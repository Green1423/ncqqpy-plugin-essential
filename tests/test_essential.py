"""essential 插件验收：群管理命令 + 按群入群问候（含定时撤回与老配置迁移）。

覆盖：

1. 配置模板契约（首次构造即把 `plugins.essential` 全部配置项写进 config.json）；
2. 命令登记与归属（5 条命令 + `greeting` 子命令，全部打上插件标签）；
3. 权限与作用域（群管理员可用 / 普通成员按「未知命令」隐藏 / 私聊不可用）；
4. 入群问候：按群配置生效、未开启不发、没有欢迎语不发、全局兜底带迁移提示；
5. 定时撤回：0 = 不撤回、正数 = 建任务、同一群只留最新一条；
6. 老布尔开关 `greeting_recall=false` 的一次性迁移；
7. `/group_config greeting` 的写入口径与回执；
8. 卸载回收命令、不动配置。

两种仓库布局都能跑（框架仓库 `plugins/essential/` 或独立插件仓库根），见下方 shim。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)   # 新 clone 里没有它（.gitignore 排除）

# ---- 插件包位置：两种仓库布局都要能跑 --------------------------------------
# 框架仓库布局 = <repo>/plugins/essential/（直接可用）；
# 独立插件仓库布局 = 仓库根就是包目录，而且 clone 出来的目录名未必等于插件名。
# 后者搭一个「包农场」<tmp>/pkgfarm/plugins/essential -> 插件包目录，让
# `import plugins.essential.*` 与 `plugin_dir=<农场>/plugins` 两种口径都成立。
PLUGIN_NAME = "essential"
PLUGIN_DIR = ROOT / "plugins" / PLUGIN_NAME
PLUGIN_ROOT_DIR = ROOT / "plugins"
if not (PLUGIN_DIR / "__init__.py").exists() and (ROOT / "__init__.py").exists():
    import tempfile
    PLUGIN_ROOT_DIR = (Path(tempfile.mkdtemp(prefix=f"pkgfarm-{PLUGIN_NAME}-"))
                       / "plugins")
    PLUGIN_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    if not (PLUGIN_ROOT_DIR / PLUGIN_NAME).exists():
        (PLUGIN_ROOT_DIR / PLUGIN_NAME).symlink_to(ROOT, target_is_directory=True)
    sys.path.insert(0, str(PLUGIN_ROOT_DIR.parent))
    PLUGIN_DIR = PLUGIN_ROOT_DIR / PLUGIN_NAME
PLUGIN_PKG = f"plugins.{PLUGIN_NAME}"

from napcat import (                                                   # noqa: E402
    CommandManager, ConfigManager, EventBus, FileManager, GroupIncreaseEvent,
    GroupBinding, MessageChain, MessageEvent, PermissionManager, PluginManager,
    TimerManager,
)

TMP = ROOT / "tests" / ".test-tmp" / "essential"
SUPERUSER = 1
ADMIN = 20001
MEMBER = 20002
GROUP = 1000000001
OTHER_GROUP = 100000002

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


class FakeApi:
    def __init__(self):
        self.sent: list[tuple[str, list]] = []
        self.bans: list[tuple] = []
        self.kicks: list[tuple] = []
        self.deleted: list[int] = []
        self._next_id = 1000

    async def send_group_msg(self, group_id, message):
        self.sent.append((str(group_id), message.build()))
        self._next_id += 1
        return {"message_id": self._next_id}

    async def delete_msg(self, message_id):
        self.deleted.append(int(message_id))

    async def set_group_ban(self, group_id, user_id, duration):
        self.bans.append((str(group_id), str(user_id), duration))

    async def set_group_kick(self, group_id, user_id, reject_add_request=False):
        self.kicks.append((str(group_id), str(user_id), reject_add_request))

    async def get_group_info(self, group_id):
        class _G:
            group_name, group_id, member_count, max_member_count = "测试群", GROUP, 42, 200
        return _G()

    async def get_group_member_info(self, group_id, user_id):
        class _M:
            nickname, card, role = "小明", "小明的名片", "admin"
        return _M()


def text_of(chain: list) -> str:
    return "".join(seg.get("data", {}).get("text", "") for seg in chain
                   if seg.get("type") == "text")


def at_of(chain: list) -> list[str]:
    return [str(seg["data"].get("qq")) for seg in chain if seg.get("type") == "at"]


async def build(cfg_path: Path):
    cfg = ConfigManager(str(cfg_path))
    api = FakeApi()
    bus, timer = EventBus(), TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[SUPERUSER])
    perm.set_group_role(GROUP, ADMIN, "admin")
    perm.set_group_role(OTHER_GROUP, ADMIN, "admin")
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    mgr = PluginManager(str(PLUGIN_ROOT_DIR), api=api, event_bus=bus, timer=timer,
                        cmd_manager=cmds, perm_manager=perm,
                        file_manager=FileManager(api=api), logger=_logger(), cfg=cfg,
                        groups=GroupBinding(bound={GROUP}, all_groups=False))
    cmds._plugins_ref = mgr
    await mgr.load(PLUGIN_NAME)
    await mgr.enable(PLUGIN_NAME)
    return mgr, api, bus, timer, cmds, cfg


def _logger():
    import logging
    logger = logging.getLogger("test.essential")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    return logger


def group_msg(text: str, *, user_id: int = ADMIN, group_id: int = GROUP) -> MessageEvent:
    return MessageEvent(message_type="group", group_id=group_id, user_id=user_id,
                        message=text)


async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    started = time.time()

    # ---------- 1. 配置模板 ----------
    print("\n[1] 配置模板：首次构造即写入全部配置项")
    cfg_path = TMP / "config.json"
    mgr, api, bus, timer, cmds, cfg = await build(cfg_path)
    section = (json.loads(cfg_path.read_text(encoding="utf-8")).get("plugins") or {}).get(
        PLUGIN_NAME) or {}
    check("模板三项都已写进 config.json",
          {"groups", "greeting", "greeting_recall_delay"} <= set(section),
          json.dumps(section, ensure_ascii=False))

    # ---------- 2. 命令登记 ----------
    print("\n[2] 命令登记与插件归属")
    registered = cmds.list_commands()
    for name in ("group_config", "ban", "kick", "unban", "groupinfo"):
        check(f"/{name} 已注册且归属 {PLUGIN_NAME}",
              name in registered and registered[name].plugin == PLUGIN_NAME,
              f"{name in registered} / {getattr(registered.get(name), 'plugin', None)}")
    greeting_sub = registered["group_config"].subcommands.get("greeting")
    check("group_config.greeting 子命令已注册",
          greeting_sub is not None and greeting_sub.plugin == PLUGIN_NAME)

    # ---------- 3. 权限与作用域 ----------
    print("\n[3] 权限与作用域")
    check("群管理员可用 /ban",
          (await cmds.handle(group_msg("/ban 20002 600"))) is not None)
    api.bans.clear()
    r = await cmds.handle(group_msg("/ban 20002 600", user_id=MEMBER))
    check("普通成员按「未知命令」隐藏", isinstance(r, tuple) and r[0] == "_not_found",
          repr(r))
    r = await cmds.handle(MessageEvent(message_type="private", user_id=ADMIN,
                                       message="/ban 20002 600"))
    check("私聊里 groupinfo/ban 这类群命令不可用",
          isinstance(r, tuple) and r[0] == "_not_found", repr(r))

    # ---------- 4. 群管理命令真的调了 API ----------
    print("\n[4] 群管理命令 → API 参数")
    api.bans.clear()
    await cmds.handle(group_msg("/ban 20002 600 刷屏"))
    check("禁言参数正确", api.bans and api.bans[-1][2] == 600, str(api.bans))
    api.bans.clear()
    await cmds.handle(group_msg("/unban 20002"))
    check("解禁走 duration=0", api.bans and api.bans[-1][2] == 0, str(api.bans))
    api.kicks.clear()
    await cmds.handle(group_msg("/kick 20002 --reject=true"))
    check("踢人带 reject_add_request", api.kicks and api.kicks[-1][2] is True,
          str(api.kicks))
    r = await cmds.handle(group_msg("/groupinfo"))
    check("群信息回执含群名与成员数",
          r is not None and "测试群" in text_of(r.build()) and "42" in text_of(r.build()),
          repr(r))
    r = await cmds.handle(group_msg("/ban"))
    check("缺参数 → 框架回用法不正确", isinstance(r, str) and "用法不正确" in r,
          repr(r))

    # ---------- 5. 入群问候 ----------
    print("\n[5] 入群问候（按群配置）")
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting", "欢迎加入信息同步群！")
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_enabled", True)
    api.sent.clear()
    await bus.dispatch(GroupIncreaseEvent(group_id=GROUP, user_id=MEMBER))
    check("有人在群里入群 → 发欢迎语", len(api.sent) == 1, str(api.sent))
    if api.sent:
        check("欢迎语 @ 了新成员", at_of(api.sent[-1][1]) == [str(MEMBER)],
              str(api.sent[-1][1]))
        check("欢迎语内容来自按群配置",
              "欢迎加入信息同步群" in text_of(api.sent[-1][1]), text_of(api.sent[-1][1]))

    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{OTHER_GROUP}.greeting", "别的群")
    api.sent.clear()
    await bus.dispatch(GroupIncreaseEvent(group_id=OTHER_GROUP, user_id=MEMBER))
    check("没开开关的群不发（默认关）", not api.sent, str(api.sent))

    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{OTHER_GROUP}.greeting_enabled", True)
    cfg.set(f"plugins.{PLUGIN_NAME}.greeting", "全局兜底欢迎语")
    api.sent.clear()
    await bus.dispatch(GroupIncreaseEvent(group_id=OTHER_GROUP, user_id=MEMBER))
    check("群内没配欢迎语时用全局兜底",
          bool(api.sent) and "别的群" in text_of(api.sent[-1][1]),
          text_of(api.sent[-1][1]) if api.sent else "（没发）")

    # 群内欢迎语被清空 → 退到全局兜底（历史兼容行为）；全局也没有 → 不发
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{OTHER_GROUP}.greeting", "")
    api.sent.clear()
    await bus.dispatch(GroupIncreaseEvent(group_id=OTHER_GROUP, user_id=MEMBER))
    check("群内欢迎语为空 → 退到全局兜底",
          bool(api.sent) and "全局兜底欢迎语" in text_of(api.sent[-1][1]),
          text_of(api.sent[-1][1]) if api.sent else "（没发）")
    cfg.delete(f"plugins.{PLUGIN_NAME}.greeting")
    api.sent.clear()
    await bus.dispatch(GroupIncreaseEvent(group_id=OTHER_GROUP, user_id=MEMBER))
    check("群内与全局都没有欢迎语 → 不发", not api.sent, str(api.sent))

    # ---------- 6. 撤回延迟 ----------
    print("\n[6] 定时撤回")
    plugin = mgr.get(PLUGIN_NAME)
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_recall_delay", 0)
    api.sent.clear()
    await bus.dispatch(GroupIncreaseEvent(group_id=GROUP, user_id=MEMBER))
    check("延迟 0 = 不建撤回任务", GROUP not in plugin._recall_tasks,
          str(list(plugin._recall_tasks)))
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_recall_delay", 5)
    api.sent.clear()
    await bus.dispatch(GroupIncreaseEvent(group_id=GROUP, user_id=MEMBER))
    check("正数延迟 → 建了撤回任务", GROUP in plugin._recall_tasks,
          str(list(plugin._recall_tasks)))
    first = plugin._recall_tasks.get(GROUP)
    await bus.dispatch(GroupIncreaseEvent(group_id=GROUP, user_id=MEMBER))
    check("同一群只留最新一条待撤回任务",
          plugin._recall_tasks.get(GROUP) is not first, "任务没有被替换")
    plugin._cancel_recall(GROUP)
    check("取消后任务表清空", GROUP not in plugin._recall_tasks)

    # ---------- 7. 老布尔开关迁移 ----------
    print("\n[7] 老配置迁移：greeting_recall=false")
    cfg.set(f"plugins.{PLUGIN_NAME}.greeting_recall_delay", 30)
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{OTHER_GROUP}.greeting_recall", False)
    check("读到老键时按「不撤回」处理", plugin._recall_delay(OTHER_GROUP) == 0)
    check("老键被顺手删掉",
          not cfg.has(f"plugins.{PLUGIN_NAME}.groups.{OTHER_GROUP}.greeting_recall"))
    check("删除后回落到全局兜底（30）", plugin._recall_delay(OTHER_GROUP) == 30)

    # ---------- 8. /group_config greeting 写入口径 ----------
    print("\n[8] /group_config greeting 写入按群作用域")
    r = await cmds.handle(group_msg("/group_config greeting 新欢迎语 --enabled=true"))
    check("回执说明已开启",
          r is not None and "已开启入群问候" in text_of(r.build()),
          text_of(r.build()) if r is not None else repr(r))
    check("欢迎语写进 groups.<群号>.greeting",
          cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting") == "新欢迎语",
          str(cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting")))
    check("开关写进 groups.<群号>.greeting_enabled",
          cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_enabled") is True)
    r = await cmds.handle(group_msg("/group_config greeting --recall_delay=0"))
    check("不带内容时保留欢迎语（不被默认值覆盖）",
          cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting") == "新欢迎语",
          str(cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting")))
    check("显式 recall_delay=0 → 关撤回",
          plugin._recall_delay(GROUP) == 0, str(plugin._recall_delay(GROUP)))

    # ---------- 10. 处理器直调与命令写法（从 bupt_news 套件迁回本插件）----------
    # 这些用例原来住在 test_bupt_news.py 的 [5.6] 里 —— 测的是 essential 的行为，
    # 却要求跑 bupt_news 套件时 essential 也在场（跨仓库耦合）。挪回本插件。
    print("\n[10] 处理器直调 + 命令写法 + 撤回任务生命周期")
    handler = [h for et, h in plugin._handlers if et is GroupIncreaseEvent][0]
    cfg.set(f"plugins.{PLUGIN_NAME}.greeting", "")
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting", "")
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_enabled", True)
    err = ""
    try:
        await handler(GroupIncreaseEvent(group_id=GROUP, user_id=MEMBER))
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    check("开启问候但没有任何欢迎语：直调处理器不抛异常（历史 TypeError 回归）",
          not err, err)

    # 位置写法：[content] [enabled] [recall_delay]
    await cmds.handle(group_msg("/group_config greeting 新内容 false"))
    check("位置写法 `false` 可关闭问候",
          cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_enabled") is False,
          str(cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_enabled")))
    r = await cmds.handle(group_msg("/group_config greeting 新内容 true"))
    check("位置写法 `true` 重新开启并回显预览",
          cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_enabled") is True
          and "已开启" in text_of(r.build()), text_of(r.build()))
    await cmds.handle(group_msg("/group_config greeting 'We\\'re BUPT!'"))
    check("StrField 可接收带转义单引号的文本",
          cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting") == "We're BUPT!",
          repr(cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting")))

    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{OTHER_GROUP}.greeting", "")
    cfg.set(f"plugins.{PLUGIN_NAME}.greeting", "")
    r = await cmds.handle(group_msg("/group_config greeting", group_id=OTHER_GROUP))
    check("没有欢迎语时开启被拒绝并给出下一步",
          "请先设置欢迎语" in text_of(r.build()), text_of(r.build()))

    # 撤回真的会执行（短延迟）+ 禁用时清空待撤回任务
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting", "撤回测试欢迎语")
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_enabled", True)
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_recall_delay", 1)
    api.deleted.clear()
    await handler(GroupIncreaseEvent(group_id=GROUP, user_id=MEMBER))
    check("正数延迟排好了待撤回任务", GROUP in plugin._recall_tasks,
          str(list(plugin._recall_tasks)))
    await asyncio.sleep(1.3)
    check("到点后确实撤回了那条欢迎语", bool(api.deleted), str(api.deleted))
    check("撤回后任务表清空（不留悬挂任务）", GROUP not in plugin._recall_tasks,
          str(list(plugin._recall_tasks)))

    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_recall_delay", 300)
    await handler(GroupIncreaseEvent(group_id=GROUP, user_id=MEMBER))
    check("禁用前确实有待撤回任务", GROUP in plugin._recall_tasks)
    await plugin.on_disable()
    check("插件禁用后待撤回任务被清空", not plugin._recall_tasks,
          str(plugin._recall_tasks))
    await mgr.enable(PLUGIN_NAME)          # 恢复启用，后续小节照常
    cfg.set(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting_recall_delay", 0)

    # ---------- 9. 卸载回收 ----------
    print("\n[9] 卸载回收命令、配置留档")
    before_greeting = cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting")
    await mgr.unload(PLUGIN_NAME)
    check("unload 后命令被回收",
          not [c for c in cmds.list_commands() if c.startswith(("ban", "kick", "unban",
                                                               "group_config",
                                                               "groupinfo"))],
          str(sorted(cmds.list_commands())))
    check("配置没有被卸载流程改动",
          cfg.get(f"plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting") == before_greeting,
          f"{cfg.get(f'plugins.{PLUGIN_NAME}.groups.{GROUP}.greeting')!r} "
          f"!= {before_greeting!r}")
    timer.stop()

    print(f"\n{'=' * 50}\n通过 {PASSED} / 失败 {FAILED}（{time.time() - started:.1f}s）")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
