"""essential 插件 —— 群管理命令 + 入群问候。

配置（config.json → plugins.essential）
---------------------------------------
    groups.<群号>.greeting          该群的入群欢迎语（**按群存**，命令写入的也是这里）
    groups.<群号>.greeting_enabled  该群是否开启入群问候
    groups.<群号>.greeting_recall_delay
                                    欢迎语发出后多少**秒**撤回：**0 = 不撤回**（默认），
                                    正数 = 启用并设为该时长
    greeting / greeting_recall_delay
                                    （可选）同名的全局配置，仅作历史兼容兜底：
                                    群内没配时才用它，建议直接用命令按群设置

命令
----
    占位符统一写作 `<名称>`（必填）与 `[名称]`（可选），尖括号/方括号本身不用输入。

    /group_config greeting [欢迎语] [enabled] [recall_delay]
                                    设置欢迎语、开关问候、设置撤回延迟（群管理员）
    /ban /kick /unban /groupinfo    群管理（见 /help）
"""
import asyncio

from napcat.plugin import PluginBase
from napcat.message import MessageChain
from napcat.models import GroupIncreaseEvent, MessageEvent
from .datapack import GreetingSchema, BanSchema, KickSchema, GroupInfoSchema

# 已废弃的布尔开关（老配置/老命令里可能有）：读取时一次性迁移到 greeting_recall_delay
_LEGACY_RECALL_KEY = "greeting_recall"
_FALSE_VALUES = ("false", "0", "no", "off", "n", "否", "关")


def _recall_delay_seconds(value) -> int:
    """把配置里的撤回延迟归一成 >= 0 的整数秒（非法值按 0 = 不撤回处理）。"""
    try:
        seconds = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, seconds)


class EssentialPlugin(PluginBase):
    name = "essential"
    version = "1.2.0"
    description = "Essential plugin for Group Chat"
    author = "Green"

    # 待撤回的欢迎语任务：群号 → Task（同一群只留最新一条，插件禁用时统一取消）。
    # 用类属性 + 惰性建表（`_recall_tasks`）而不是重写 __init__：基类构造签名会随
    # 框架演进，复写一份等于把参数列表复制过来，早晚漂移。
    _recall: dict | None = None

    @property
    def _recall_tasks(self) -> dict:
        if self._recall is None:
            self._recall = {}
        return self._recall

    def config_template(self) -> dict:
        """完整配置模板（框架在加载时写进 `config.json` → `plugins.essential`）。

        `groups` 是**按群**配置的容器（`groups.<群号>.greeting` 等，由
        `/group_config greeting` 写入），这里只放一个空容器，让运维看得到结构；
        `greeting` / `greeting_recall_delay` 是历史兼容的全局兜底（群内没配时才用）。
        """
        return {
            # 按群配置：groups.<群号>.{greeting, greeting_enabled, greeting_recall_delay}
            "groups": {},
            # 历史兼容的全局兜底，建议直接用 /group_config greeting 按群设置
            "greeting": "",
            "greeting_recall_delay": 0,     # 0 = 不撤回
        }

    async def on_enable(self):
        # permission/scope 只是子命令的默认值：带子命令的命令是命令组，本体不设权限
        @self.register_command("group_config", permission="admin", scope="group",
                               description="群管理")
        async def cmd_group_config(params, event):
            # 用法行与占位符图例都交给框架生成（手写会与 schema 漂移，
            # 且图例会与 render_help 里那行重复）。
            handler = self.cmds.list_commands().get("group_config")
            if handler is None:                     # 理论上不可达
                return "📖 群管理：子命令清单不可用（详见日志）"
            return "📖 " + handler.help_text

        @self.on_event(GroupIncreaseEvent, hid="essential.greeting_event")
        async def greeting(event: GroupIncreaseEvent):
            """入群问候。

            ⚠️ 欢迎语必须读**按群**配置（与 greeting_enabled 同一作用域）：
            此前读的是全局 `plugins.essential.greeting`，而命令只往 `groups.<群号>.greeting` 写，
            于是群里明明配了欢迎语、这里却取到 None，每次有人入群都抛
            `TypeError: can only concatenate str (not "NoneType") to str`，问候一直发不出去。

            撤回延迟同样按群读（`_recall_delay` 与命令写入时同一口径）：
            **0 = 不撤回**，正数 = 发出后多少秒撤回。
            """
            cfg = self.config.group(event.group_id)
            if not cfg.get("greeting_enabled", False):
                return
            text = (cfg.get("greeting") or "").strip()
            if not text:
                # 历史兼容：只认旧的全局 greeting（并提示迁移）
                legacy = str(self.config.get("greeting") or "").strip()
                if legacy:
                    self.logger.warning(
                        "[essential] 群 %s 未按群配置欢迎语，暂用全局 greeting（建议改用 "
                        "/group_config greeting <内容> 按群设置）", event.group_id)
                    text = legacy
            if not text:
                self.logger.warning(
                    "[essential] 群 %s 开启了入群问候但没有欢迎语，已跳过"
                    "（用 /group_config greeting <内容> 设置）", event.group_id)
                return

            delay = self._recall_delay(event.group_id)
            # 有人入群先取消上一条同群的待撤回：否则任务表里会攒下过期任务，
            # 它们到点后照样撤回（新欢迎语会莫名消失），且再也无法取消。
            self._cancel_recall(event.group_id)

            result = await self.api.send_group_msg(
                str(event.group_id),
                MessageChain().at(event.user_id).text(" " + text))

            message_id = getattr(result, "message_id", None)
            if message_id is None and isinstance(result, dict):
                message_id = result.get("message_id")
            if delay > 0 and message_id:
                task = asyncio.create_task(
                    self._recall_later(event.group_id, message_id, delay))
                self._recall_tasks[event.group_id] = task
                task.add_done_callback(
                    lambda t, gid=event.group_id: self._forget_recall(gid, t))

        @self.register_command("group_config.greeting", permission="admin", scope="group",
                               schema=GreetingSchema, description="欢迎语设置")
        async def greeting_command(params, event: MessageEvent):
            """设置/开关入群问候。

            不带内容时**保留**现有欢迎语（旧行为会用 schema 默认值把长欢迎语覆盖成「欢迎!」），
            只切换开关；内容为空则明确报错而不是把欢迎语清空。

            撤回只有一个参数：`recall_delay`（秒）—— **不带 = 保持现值**（所以改欢迎语不会
            把手写的延迟重置掉），**0 = 取消撤回**，正数 = 启用并设为该时长。
            键写进**群作用域**，与处理器读取时的作用域完全一致。
            """
            cfg = self.config.group(event.group_id)
            content = (params.content or "").strip()
            if content:
                cfg.set("greeting", content)
            current = (cfg.get("greeting") or "").strip()

            if params.recall_delay is not None:      # None = 调用者没提延迟，保持现值
                cfg.set("greeting_recall_delay", int(params.recall_delay))
                cfg.delete(_LEGACY_RECALL_KEY)       # 老开关已被延迟取代，顺手清掉
            delay = self._recall_delay(event.group_id)
            recall_text = f"撤回: {delay} 秒" if delay > 0 else "撤回: 关"

            if not params.enabled:
                cfg.set("greeting_enabled", False)
                return MessageChain().at(event.user_id).text(
                    f" ⏸ 已关闭入群问候（{recall_text}）")
            if not current:
                return MessageChain().at(event.user_id).text(
                    " ⚠️ 请先设置欢迎语：/group_config greeting <内容>")
            cfg.set("greeting_enabled", True)
            preview = current if len(current) <= 20 else current[:20] + "…"
            return MessageChain().at(event.user_id).text(
                f" ✅ 已开启入群问候（{recall_text}）：{preview}")

        @self.register_command("ban", schema=BanSchema, permission="admin",
                               scope="group",
                               description="禁言成员")
        async def cmd_ban(params, event: MessageEvent):
            await self.api.set_group_ban(
                group_id=str(event.group_id),
                user_id=str(params.user_id), duration=params.duration)
            r = f"，原因: {params.reason}" if params.reason else ""
            return f"已禁言 {params.user_id} {params.duration}秒{r}"

        @self.register_command("kick", schema=KickSchema, permission="admin",
                               scope="group",
                               description="踢出成员")
        async def cmd_kick(params, event: MessageEvent):
            await self.api.set_group_kick(
                group_id=str(event.group_id),
                user_id=str(params.user_id), reject_add_request=params.reject)
            r = " (已拒绝再次加群)" if params.reject else ""
            return f"已踢出 {params.user_id}{r}"

        @self.register_command("unban", schema=BanSchema, permission="admin",
                               scope="group",
                               description="解禁成员")
        async def cmd_unban(params, event: MessageEvent):
            await self.api.set_group_ban(
                group_id=str(event.group_id),
                user_id=str(params.user_id), duration=0)
            return f"已解禁 {params.user_id}"

        @self.register_command("groupinfo", schema=GroupInfoSchema,
                               scope="group", description="查看群信息")
        async def cmd_group_info(params, event: MessageEvent):
            if not event.group_id:
                return "此命令只能在群内使用"
            uid = str(params.user_id) if params.user_id else str(event.user_id)
            info = await self.api.get_group_info(str(event.group_id))
            member = await self.api.get_group_member_info(str(event.group_id), uid)
            role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(
                member.role, str(member.role or "成员"))
            return MessageChain().text(
                f"群: {info.group_name}({info.group_id})\n"
                f"成员: {info.member_count}/{info.max_member_count}\n"
                f"{member.nickname} | 名片: {member.card or '无'} | 身份: {role_cn}")

        # 参数写错时的回执由**框架**统一负责（`handle()` 把 SchemaError 变成
        # 「⚠️ 用法不正确：… + 该命令的帮助页」），插件不再自己注册 on_schema_error
        # 回调 —— 那个钩子只有一个槽位，两个插件都注册时后者覆盖前者。

    # ---- 入群欢迎语撤回 ----

    def _recall_delay(self, group_id: int) -> int:
        """返回该群的撤回延迟秒数（**0 = 不撤回**）；群内配置优先、全局兜底。

        键名沿用 `greeting_recall_delay`。已废弃的布尔开关 `greeting_recall`
        只在迁移窗口里被读一次：显式 false（= 永不撤回）折算成 0（= 不撤回），
        行为与升级前一致；读到时顺手删键，避免这个死键一直挡着全局兜底。
        """
        cfg = self.config.group(group_id)

        legacy = cfg.get(_LEGACY_RECALL_KEY)
        if legacy is not None:
            cfg.delete(_LEGACY_RECALL_KEY)      # 一次性迁移，别让旧键长期覆盖全局兜底
            if str(legacy).strip().lower() in _FALSE_VALUES:
                self.logger.info(
                    "[essential] 群 %s 的 greeting_recall=false 已迁移为 "
                    "greeting_recall_delay=0（不撤回）", group_id)
                return 0

        delay = cfg.get("greeting_recall_delay")
        if delay is None:
            delay = self.config.get("greeting_recall_delay")
        return _recall_delay_seconds(delay)

    def _cancel_recall(self, group_id: int):
        """取消该群尚未执行的待撤回任务（不存在或已跑完都算成功）。"""
        task = self._recall_tasks.pop(group_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _forget_recall(self, group_id: int, task: asyncio.Task):
        """任务结束后从表里摘掉；只摘自己这条，避免误删后来替换上的新任务。"""
        if self._recall_tasks.get(group_id) is task:
            self._recall_tasks.pop(group_id, None)

    async def _recall_later(self, group_id: int, message_id: int, delay: int):
        """延迟 `delay` 秒后撤回消息；失败只记日志，不影响问候本身。"""
        try:
            await asyncio.sleep(delay)
            await self.api.delete_msg(int(message_id))
            self.logger.info("[essential] 已撤回群 %s 的入群欢迎语（message_id=%s，延迟 %ss）",
                             group_id, message_id, delay)
        except asyncio.CancelledError:
            self.logger.debug("[essential] 群 %s 的欢迎语撤回已取消（message_id=%s）",
                              group_id, message_id)
        except Exception as e:  # noqa: BLE001 - 撤回是尽力而为
            self.logger.warning("[essential] 撤回群 %s 的入群欢迎语失败（message_id=%s）：%s",
                                group_id, message_id, e)

    async def on_disable(self):
        """禁用/热重载时取消所有待撤回任务（否则到点会撤回新实例发的消息）。"""
        for gid in list(self._recall or {}):     # 没建过表就不必建
            self._cancel_recall(gid)
        await super().on_disable()
