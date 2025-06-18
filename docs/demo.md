# 界面演示与截图复现

[返回项目首页](../README.md)

功能演示的六张 PNG 为实际网页截图。视频画面由 `scripts/demo.py` 绘制，置信度、复核记录与任务状态均为固定演示数据；它们不来自用户上传、真实道路数据或模型推理。

首页展示四张真实道路裁切图片及一张真实样例复核截图，位于 `docs/images/real/`，来源与标注流程见 [真实样例说明](real-examples.md)。本演示脚本不会生成或覆盖这些真实样例。

运行演示：

```bash
python -m pip install -r requirements-dev.txt
python scripts/demo.py --data-dir /tmp/roadlabel-demo --port 18780
```

目录必须为空或不存在，脚本拒绝写入已有数据目录。脚本只监听本机，关闭后台执行器并清除继承的模型和登录配置，不会调用模型或占用 GPU。演示结束后在该终端按 Ctrl+C 关闭。

在另一个终端中安装 Playwright 并截图：

```bash
npm install --no-save --package-lock=false playwright@1.57.0
npx playwright install chromium
node scripts/render_docs.cjs
```

截图写入 `docs/images/`。脚本先检查页面仅包含三个预设演示视频，随后记录视频库、复核、筛选、任务配置、Skills 和任务队列。它不提交任务，也不改动真实工作室。

可通过 `PLAYWRIGHT_PACKAGE` 指定已安装的 Playwright 包，通过 `CHROMIUM_PATH` 指定 Chromium 可执行文件。首页流程图 `workflow.svg` 是项目内的矢量示意图，系统架构与模型生命周期图使用 GitHub 原生 Mermaid 渲染。
