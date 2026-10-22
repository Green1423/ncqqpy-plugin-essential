#!/usr/bin/env python
"""入群问候撤回探针（只读：**不会改动线上 config.json**，也不需要连 QQ）。

用法（在 bot 的工作目录跑，默认就地找 plugins/essential）：
    python probe_greeting_recall.py            # 探当前目录
    python probe_greeting_recall.py /opt/ncqq  # 探指定目录

输出三件事：
  1. 线上 essential 的 main.py / datapack.py 里 recall 相关代码长什么样（版本指纹）；
  2. 用线上 config.json 复算某个群的 _recall_delay，并干跑一次 /group_config greeting；
  3. 关键字段是否齐全（据此判断「撤回: 关」是旧代码还是配置问题）。
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

# 不传参时默认探测**仓库根**（本文件在 tests/diagnostics/ 下，要上三级），
# 而不是当前工作目录。
root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
    else Path(__file__).resolve().parent.parent.parent
plug = root / "plugins" / "essential"
print(f"探针目录: {root}")
print(f"插件目录: {plug}")

# ---- 1. 源码指纹 ----
for name in ("main.py", "datapack.py"):
    f = plug / name
    if not f.exists():
        print(f"  ❌ 缺少 {f}")
        continue
    src = f.read_text(encoding="utf-8")
    version = re.search(r'version\s*=\s*"([^"]+)"', src)
    print(f"\n--- {name} (mtime={f.stat().st_mtime:.0f}, version={version.group(1) if version else '?'}) ---")
    for i, line in enumerate(src.splitlines(), 1):
        if "recall" in line.lower() and not line.strip().startswith("#"):
            print(f"  {i:>4}| {line.rstrip()}")

# ---- 2. 干跑：直接加载线上插件模块，用线上 config.json 算一遍 ----
sys.path.insert(0, str(root))
try:
    from napcat.config import ConfigManager
except Exception as e:  # noqa: BLE001
    print(f"\n无法 import napcat（需在 bot 工作目录跑）: {e}")
    sys.exit(0)

cfg_path = root / "config.json"
data = json.loads(cfg_path.read_text(encoding="utf-8"))
# ⚠️ 探针要复算 _recall_delay，而它会顺手迁移旧键并写盘；这里用副本，绝不碰线上文件
work = root / ".probe-config.json"
work.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
groups = (data.get("plugins", {}).get("essential", {}).get("groups") or {})
print(f"\n--- config.json 里的 essential 群配置（{len(groups)} 个群）---")
for gid, conf in groups.items():
    keys = {k: conf.get(k) for k in
            ("greeting_enabled", "greeting_recall", "greeting_recall_delay") if k in conf}
    print(f"  群 {gid}: {keys}")

spec = importlib.util.spec_from_file_location("probe_essential", plug / "__init__.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["probe_essential"] = mod
spec.loader.exec_module(mod)

class _Null:
    def __getattr__(self, _):
        def _noop(*a, **k):
            return None
        return _noop

import logging
plugin = mod.EssentialPlugin(api=_Null(), event_bus=_Null(), timer=_Null(),
                             cmd_manager=_Null(), perm_manager=_Null(),
                             file_manager=_Null(), logger=logging.getLogger("probe"),
                             cfg=ConfigManager(str(work)))
print("\n--- 每个群的撤回判定（_recall_delay，0 = 不撤回）---")
for gid in groups:
    try:
        print(f"  群 {gid}: _recall_delay = {plugin._recall_delay(int(gid))}")
    except Exception as e:  # noqa: BLE001
        print(f"  群 {gid}: 判定失败 -> {type(e).__name__}: {e}")

print("\n判读：源码里若还有 `greeting_recall` 字段 / `recall = BoolField`，说明线上是旧版；"
      "新版只应有 `recall_delay = DurationField(default=None, ...)`。")
work.unlink(missing_ok=True)
