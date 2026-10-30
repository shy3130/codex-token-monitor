# 界面截图

以下截图由当前 **v1.6.0** 源码重新运行生成。桌面主图会展开“模型使用与费用”，直接显示每个模型的 **Token 总量、API 参考费用和平均 TPS**。页面底部保留界面版本号。

所有图均使用 `scripts/demo.py` 生成的虚构数据，不包含真实会话、用户标识或账单。图片文件名包含版本号，避免与旧截图混用。

| 文件 | 展示内容 |
| --- | --- |
| [dashboard-v1.6.0-light.png](dashboard-v1.6.0-light.png) | 浅色桌面主页，展开新版模型三列指标 |
| [dashboard-v1.6.0-dark.png](dashboard-v1.6.0-dark.png) | 同一布局的深色主题 |
| [dashboard-v1.6.0-overview-light.png](dashboard-v1.6.0-overview-light.png) | 浅色总览，模型明细默认收起 |
| [dashboard-v1.6.0-overview-dark.png](dashboard-v1.6.0-overview-dark.png) | 深色总览，模型明细默认收起 |
| [models-v1.6.0-light.png](models-v1.6.0-light.png) | 浅色模型明细特写 |
| [models-v1.6.0-dark.png](models-v1.6.0-dark.png) | 深色模型明细特写 |
| [models-v1.6.0-mobile.png](models-v1.6.0-mobile.png) | 390 像素窄屏中的模型明细 |
| [dashboard-v1.6.0-mobile.png](dashboard-v1.6.0-mobile.png) | 窄屏单列完整页面 |

另外生成带 `-en-` 的六张英文截图（桌面浅深色、模型明细浅深色、模型明细窄屏、完整窄屏），供英文 README 使用。

[manifest.json](manifest.json) 记录生成时间与每张图的语言、界面源码 SHA-256，以及各图的尺寸和 SHA-256，可核对截图是否来自当前代码。桌面视口为 1440 × 1050；窄屏视口为 390 × 844，完整页面按内容高度导出。

## 仅预览演示

在仓库根目录运行：

```sh
python3 scripts/demo.py
```

打开 [本机演示](http://127.0.0.1:8877/)，按 `Ctrl+C` 停止。可通过 `--port` 更换端口。演示只创建内存数据库，不扫描 Codex 目录，也不修改已有采集服务。数据覆盖最近 30 个自然日；数量由固定随机种子生成，时间随启动日期变化。

## 重新截图

需要 Python 3.9+、Node.js 22+ 和 Playwright Chromium。Playwright 是可选的维护工具，项目运行不依赖它。下面在临时目录安装截图依赖，避免向项目引入 Node.js 包清单：

```sh
npm install --prefix /tmp/token-monitor-screenshots --no-save --package-lock=false playwright
/tmp/token-monitor-screenshots/node_modules/.bin/playwright install chromium
NODE_PATH=/tmp/token-monitor-screenshots/node_modules node scripts/capture-screenshots.cjs
```

脚本自动选择空闲端口、启动自己的演示进程、等待数据加载、导出十四张中英文 PNG 与校验清单，结束时关闭演示进程和浏览器。截图输出到本目录，中英文首页分别引用对应语言的图片，并按阅读者的浅深色主题选择主题。

脚本核对采集服务和页脚版本，并检查桌面双列、窄屏无横向溢出、模型数值与接口一致、分页、刷新保留历史页、单价展开状态、费用明细键盘操作和浏览器脚本错误。还使用测试宿主 iframe 验证标准 MCP Apps、旧版桥接、宿主语言与浏览器语言的优先级、页面语言镜像、手动覆盖、恢复自动，以及切换后页码和明细状态保留。更新图片后还应人工查看，确认布局完整。

已有 Playwright 安装时可通过 `NODE_PATH` 指向其 `node_modules`，或用 `PLAYWRIGHT_MODULE` 指定模块路径。可通过 `PYTHON` 指定 Python 命令，通过 `SCREENSHOT_BROWSER_CHANNEL=chrome` 使用已安装的 Google Chrome；默认使用 Playwright Chromium。

[返回项目首页](../../README.md)
