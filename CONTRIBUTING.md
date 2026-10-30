# 参与贡献

欢迎提交问题、文档改进和代码修改。运行环境为 Python 3.9+；采集服务与 MCP 服务保持只依赖标准库。Node.js 仅用于界面脚本测试及可选截图。

## 项目结构

```text
monitor.py                  会话扫描、OTLP 日志接收、SQLite 与 HTTP 接口
plugin/server.py            stdio MCP 服务
plugin/ui/monitor.html      MCP Apps 与本地浏览器共用的界面
plugin/assets/              插件图标
plugin/plugin.json          插件元数据
plugin/.codex-plugin/       Codex 插件清单
examples/                   MCP 与 Codex 遥测配置示例
scripts/demo.py             只使用虚构数据的内存演示
scripts/capture-screenshots.cjs  实际浏览器截图与界面检查
scripts/sync-local-ui.py    同步本机界面，包含同版本的插件缓存
tests/                      MCP、界面脚本与端到端验证
test_monitor.py             采集与统计单元测试
docs/                       安装说明、截图与历史检查记录
.github/                    CI、Issue 与 Pull Request 模板
```

## 本地开发

建议用演示环境调整界面：

```sh
python3 scripts/demo.py
```

打开 [本机演示](http://127.0.0.1:8877/)。使用另一个端口可传入 `--port 8878`。修改 HTML 后刷新浏览器即可；修改 Python 后重启演示进程。

需要验证真实采集时，显式选择独立端口与数据目录，避免连接到已有运行副本。项目根目录的 `data/` 已加入忽略规则。

### 同步已有本机安装的界面

若本机已经安装该插件，修改 HTML 后运行：

```sh
python3 scripts/sync-local-ui.py
```

脚本同时检查源码安装目录、采集服务运行目录，以及 `~/.codex/plugins/cache/*/token-monitor/<当前版本>/` 中的界面。桌面客户端可能实际从版本缓存目录运行，仅更新源码安装目录不足以让面板生效。脚本先备份有变化的文件，再同步并校验所有副本；不会改动其他版本的缓存、采集代码或统计数据库。

自定义安装路径可用 `--plugin-home`、`--runtime-dir` 指定。同步后关闭并重新打开面板；面板内的刷新按钮只刷新数据，不能替换已经加载的 HTML。Python 采集逻辑有变化时，仍需另外同步代码并重启采集服务。

## 运行检查

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 python3 tests/integration_smoke.py
node --test tests/test_ui.js
```

Python 单元测试覆盖会话统计、遥测匹配、费用计算、分页与 MCP 输入校验。端到端验证会启动临时采集服务、写入虚构会话、发送模拟遥测，再经 HTTP 和 stdio MCP 读取结果。界面脚本测试验证刷新失败恢复、分页和显示行为。

CI 在 Linux 的 Python 3.9、3.13 和 macOS 的 Python 3.13 上运行 Python 检查，另用 Node.js 22 运行界面脚本测试。真实浏览器截图检查为可选本地步骤，操作见 [截图说明](docs/screenshots/README.md)。

## 提交修改

- 说明修改解决的问题、用户可见行为和实际执行的验证。
- 采集、统计或分页逻辑有变化时，补充覆盖对应行为的测试。
- 界面改动请检查宽屏、390 像素窄屏及深色主题；更新展示图时使用虚构数据。
- 保持 `monitor.py`、`plugin/server.py` 和两份插件清单的版本一致；发布版本时更新 `CHANGELOG.md`。
- 界面发布时更新插件版本，MCP 资源地址随版本变化；检查 `tools/list` 与 `resources/read` 引用同一版本资源，避免客户端继续复用旧页面。
- 不提交真实会话、数据库、访问令牌、日志或包含个人信息的截图。复现数据应使用最小虚构样本。

问题报告请提供系统、Python 版本、项目版本、运行方式和复现步骤；可通过仓库的 Issue 表单提交。
