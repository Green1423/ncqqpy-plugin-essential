# essential —— 群管理基础命令 + 入群问候

禁言/踢人/解禁/群信息等群管理命令，以及按群配置的入群欢迎语（含定时撤回）。

这是 [ncqqpy](https://github.com/<you>/ncqqpy) 的插件，**独立仓库**：
框架按 `plugin_dir`（默认 `./plugins`）扫描子目录加载插件，所以把它 clone 到
框架的 `plugins/` 下即可。

## 安装

```bash
cd /path/to/ncqqpy
git clone https://github.com/<you>/ncqqpy-plugin-essential plugins/essential
# 重新加载（QQ 内，超级用户 · 仅私聊）
#   /reload essential
```

要求：框架 [ncqqpy](https://github.com/<you>/ncqqpy)（`napcat` 包需可导入）。

## 配置

插件被**构造**时（`on_load()` 之前）会把自己的**全部**配置项写进
`config.json` 的 `plugins.essential` 段（只补缺键、不覆盖已有值、幂等），所以打开
文件就能看到所有开关和默认值。模板如下：

```json
{
  "groups": {},
  "greeting": "",
  "greeting_recall_delay": 0
}
```

环境变量（优先级高于文件）：

| 变量 | 说明 |
|------|------|
| `（无）` | 全部配置在 `config.json → plugins.essential`，按群配置由 `/group_config greeting` 写入 |


## 测试

```bash
# 独立仓库里跑：框架需可导入（PYTHONPATH 指向框架仓库，或 pip install -e）
PYTHONPATH=/path/to/ncqqpy python -B tests/test_essential.py
```

套件：`tests/test_essential.py`。测试会在 `tests/.test-tmp/` 下建临时目录，不碰真实数据。

## 注意

- `tests/diagnostics/probe_greeting_recall.py` 是只读探针。

## License

MIT，见 [LICENSE](LICENSE)。
