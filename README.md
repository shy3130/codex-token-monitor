# Codex Token Monitor

本地 Token 用量和模型请求速度监测项目。它读取本机 Codex 的会话记录与遥测，按今日、最近 7 日和最近 30 日展示 Token、TPS、模型、思考强度与 API 参考费用。采集服务、MCP 接口和界面都在此项目中，使用 Python 3 标准库运行，无需安装第三方 Python 包。

## 现在如何使用

当前版本为 **1.5.0**，包含 [更新记录](CHANGELOG.md) 中列出的 TPS 和刷新修复。项目源码与已安装的运行副本分开管理：修改或拉取源码后，需要更新运行副本并重启采集服务才会生效。更新前应备份运行代码与数据库，保留原来的统计数据目录。

采集服务默认使用 `127.0.0.1:8765`。支持本地 stdio MCP 服务的客户端可以参考 [examples/mcp-client.json](examples/mcp-client.json)，让项目里的 MCP 服务读取采集结果。**先将示例中的 `/absolute/path/codex-token-monitor/plugin/server.py` 改为本机实际绝对路径**；不同客户端的配置位置和格式也可能不同。

客户端支持 MCP Apps 时可以尝试通过 `open_token_monitor` 打开图形面板；不支持图形面板的 MCP 客户端可以调用 `get_token_usage` 读取用量数据。不同客户端的界面支持有所不同，目前仅验证了通用 stdio MCP 交互，没有逐一验证其他产品。

宽屏界面采用左右布局，窄屏自动切换为单列。调用记录每页可选 10、20、50 条，可持续翻页查看所选范围内的全部已采集计时记录；浏览历史页时，自动刷新保留当前页。强度徽标只显示 HIGH、XHIGH 等设置值。HTTP `/api/requests` 与 MCP `get_request_history` 均支持游标分页。

## 独立运行采集服务

在终端运行：

```sh
cd codex-token-monitor
python3 monitor.py --port 8766
```

这里使用 `8766`，以避开可能已运行的 `8765` 服务。服务默认扫描 `~/.codex` 下面的会话记录，并把项目自身的统计数据写入 `data/metrics.sqlite3`；也可以通过 `--codex-dir` 和 `--data-dir` 指定其他目录。需要使用本项目服务的 MCP 客户端，将配置中的 `TOKEN_MONITOR_BACKEND_URL` 改为 `http://127.0.0.1:8766`。该服务地址只接受本机回环地址。

独立服务也提供可选的本地调试界面 `http://127.0.0.1:8766/`；使用 MCP 客户端不需要打开网页。

Token 统计可以从本地会话重建。TPS 还需要 Codex 将 OTLP 日志发送给这个服务。如果 Codex 的遥测仍指向原来的 `8765` 服务，因此单独启动 `8766` 后不会自动获得新的 TPS。若以后要切换到独立服务，需在 Codex 配置中将现有 `[otel]` 导出地址改为 `http://127.0.0.1:8766/v1/logs` 并重启 Codex；切换后原 `8765` 服务将不再收到这些新遥测。项目不会自动修改此配置。

## 支持范围与统计口径

- 采集源目前是 **本机 Codex 会话和 Codex 遥测**。其他支持 MCP 的客户端可以查看这些数据，但不会自动统计它们自身的调用；普通 ChatGPT 网页或移动端的会话也不会自动纳入。要扩展采集范围，需要针对相应产品增加适配方式。
- 三个时间范围按北京时间自然日计算并包含今天。输入 Token 已包含缓存读取，输出已包含推理 Token。
- TPS 为一次已完成模型请求的输出 Token 数除以请求耗时，包含首字等待和推理时间，不包含请求之间的工具执行时间；它不是逐 Token 的实时速度。
- 金额按各模型 API 标准文本价格分别计算后合计，是参考估算，不是实际订阅扣费或 API 账单。价格快照日期为 **2026-10-29**，不会自动更新；缺少价格的模型单独标记并不计入金额，工具费用等也未计入。
- 采集服务只监听本机回环地址；SQLite 保存数量、模型、思考强度及会话和请求标识等统计信息，不有意保存提示词或模型回复。项目副本不包含原有数据库、凭据或会话记录。

价格来源：[OpenAI API 定价](https://developers.openai.com/api/docs/pricing)、[Z.ai 定价](https://docs.z.ai/guides/overview/pricing)。

## 文件与验证

```text
monitor.py                 本地采集服务
plugin/server.py           标准 stdio MCP 服务
plugin/ui/monitor.html     MCP Apps 面板与可选本地界面
examples/mcp-client.json   需要填写本机路径的 MCP 配置示例
tests/                     MCP 与端到端验证
```

运行测试：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 python3 tests/integration_smoke.py
```

端到端测试使用临时端口、临时数据库和虚构会话数据，不修改现有服务或历史记录。

如果安装了 Node.js，还可以验证界面在服务出错或无响应时的超时与重试行为（项目运行本身不需要 Node.js）：

```sh
node --test tests/test_ui.js
```

已发现问题、修复方式与本次验证范围见 [缺陷检查记录](docs/bug-audit-2026-10-30.md)。
