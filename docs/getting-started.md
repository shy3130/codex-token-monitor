# 安装与配置

## 1. 启动采集服务

需要 Python 3.9+，无需安装第三方 Python 包。以下命令适用于 macOS / Linux；Windows 请根据本机 Python 安装调整命令和路径，尚未验证 Windows 环境。

```sh
git clone https://github.com/shy3130/codex-token-monitor.git
cd codex-token-monitor
python3 monitor.py --port 8765
```

浏览器打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。服务在当前终端前台运行，按 `Ctrl+C` 停止。首次扫描已有会话后即可看到 Token 统计。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--port` | `8765` | 本机 HTTP、界面与 OTLP 接收端口 |
| `--codex-dir` | `~/.codex` | Codex 数据目录，扫描其 `sessions/` 与 `archived_sessions/` |
| `--data-dir` | 项目根目录下的 `data/` | 保存 `metrics.sqlite3` 统计数据库 |

如 Codex 使用了其他数据目录（例如自定义 `CODEX_HOME`），需要显式传入 `--codex-dir`。项目不会自动读取该环境变量。

自定义目录和端口：

```sh
python3 monitor.py --port 8766 \
  --codex-dir /absolute/path/to/codex-data \
  --data-dir /absolute/path/to/monitor-data
```

若只想体验界面，运行 `python3 scripts/demo.py`，打开 [演示面板](http://127.0.0.1:8877/)。该模式仅使用内存里的虚构数据。

## 2. 启用请求 TPS

Token 可从本地会话重建；请求计时依赖 Codex 后续发送的遥测，无法从历史 Token 记录补齐。

在 Codex 的用户配置文件 `~/.codex/config.toml`（自定义 `CODEX_HOME` 时为其下的 `config.toml`）中合并以下配置，再重启 Codex：

```toml
[otel]
log_user_prompt = false

[otel.exporter."otlp-http"]
endpoint = "http://127.0.0.1:8765/v1/logs"
protocol = "json"
```

**必须使用 `protocol = "json"`**，本项目不解析 OTLP Protobuf 或 gRPC。端口应与采集服务一致。若已有 `[otel]`、`exporter = "none"` 或其他 exporter 配置，请编辑现有配置，避免重复 TOML 表或同一键的字符串与子表冲突。更换导出地址后，原采集器将不再收到这些新日志。

这里配置的是 **日志 exporter**；`trace_exporter` 和 `metrics_exporter` 不能代替它。遥测配置应放在用户级配置中，项目内 `.codex/config.toml` 不接受 `otel` 设置。字段与 JSON 协议选项参见 OpenAI 官方的 [高级配置](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry) 和 [示例配置](https://learn.chatgpt.com/docs/config-file/config-sample)。独立配置文件见 [examples/codex-otel.toml](../examples/codex-otel.toml)。

保持采集服务运行，在重启后的 Codex 中完成一次模型请求。收到完整计时后，面板会更新 TPS；它表示一次请求的平均输出速度，包含首字等待与推理时间。

## 3. 接入 MCP 客户端（可选）

采集服务独立运行，MCP 服务负责读取本机 HTTP 接口。将 [examples/mcp-client.json](../examples/mcp-client.json) 合并到客户端支持的 MCP 配置中：

```json
{
  "mcpServers": {
    "codex-token-monitor": {
      "command": "python3",
      "args": ["/absolute/path/codex-token-monitor/plugin/server.py"],
      "env": {
        "TOKEN_MONITOR_BACKEND_URL": "http://127.0.0.1:8765"
      }
    }
  }
}
```

将 `args` 改为本机实际绝对路径；必要时也将 `command` 改为 Python 可执行文件的绝对路径。配置位置、字段格式以及重启方式以客户端要求为准。

`TOKEN_MONITOR_BACKEND_URL` 默认是 `http://127.0.0.1:8765`，只接受 `http` 的 `127.0.0.1` 或 `localhost` 地址。使用其他端口时，需要同时更新采集命令、遥测 endpoint 和此环境变量。

客户端可以调用 `get_token_usage`、`get_request_history`；支持 MCP Apps 时，可调用 `open_token_monitor` 展示面板。不支持面板的客户端仍可读取结构化统计数据。

`plugin/` 另含插件清单与图标，但通用 MCP 配置不等同于某个客户端的插件商店安装流程。目前未逐一验证其他产品的插件安装及面板支持。

## 常见问题

### 有 Token，TPS 一直为空

检查用户级 Codex 配置的日志 exporter 是否启用、是否为 JSON、端口是否一致，并重启 Codex。需要完成一次新的模型请求；旧的会话文件不能补出历史请求计时。

### 启动提示端口被占用

可能已有采集服务运行。先访问 [健康检查](http://127.0.0.1:8765/health) 查看版本。要独立试用另一个实例，可选择 `--port 8766 --data-dir /absolute/path/to/separate-data`，并在 MCP 配置中使用相同端口。两个采集实例应使用各自的数据目录。

### 面板没有会话记录

确认 `--codex-dir` 指向实际 Codex 数据目录，且其下存在可读的 `sessions/` 或 `archived_sessions/` JSONL 文件。会话只存在于另一台机器时，本机不会自动同步采集。

### MCP 提示无法连接本机采集服务

确认采集进程仍在运行，浏览器能打开其 `/health`，并核对 `TOKEN_MONITOR_BACKEND_URL`。stdio MCP 服务不会自动启动采集进程。

### 费用为什么与账单不同

界面按内置标准文本 Token 价格估算，不读取订阅额度或实际账单。价格快照不会自动更新，未知价格、工具费及其他收费项可能未包含。完整口径见 [项目说明](../README.md#统计说明)。

## 更新已有安装

拉取源码后，停止并重启从该源码目录运行的服务即可加载新代码。若客户端使用另一份已安装副本，需要同步该副本的运行代码和插件资源，并重启采集服务及客户端 MCP 服务。

更新前备份运行代码与统计数据库；备份 SQLite 时先停止使用它的采集服务，避免只复制主文件而遗漏尚未合并的 WAL 数据。保留原统计数据目录，不要用演示数据或空目录覆盖历史记录。

仅更新界面时，可运行 `python3 scripts/sync-local-ui.py`，同步已有本机安装与当前版本的插件缓存。客户端可能从 `~/.codex/plugins/cache/` 运行，不能只同步源码安装目录。脚本会先备份并校验各副本；完成后关闭并重新打开面板，面板内的刷新按钮只刷新统计数据。

1.5.2 起，界面资源地址包含版本号，页脚也显示版本号。新的模型明细应将 Token 总量、API 参考费用和平均 TPS 三列并排展示；若仍只有右侧金额和小字 Token，请核对插件实际安装版本，已有面板可能仍引用旧资源。发布界面改动时应同步更新插件版本和工具引用的资源地址，参见 [OpenAI UI 资源说明](https://developers.openai.com/plugins/build/chatgpt-ui)。

[返回项目首页](../README.md)
