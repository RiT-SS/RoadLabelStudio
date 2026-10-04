# 部署与使用指南

[返回项目首页](../README.md)

## 快速运行

环境：Python 3.11+，系统已安装 `ffmpeg` 和 `ffprobe`。启动网页本身不需要 GPU。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py --port 8780
```

打开 <http://127.0.0.1:8780/>。默认数据保存在 `data/`，可通过 `STUDIO_DATA_DIR` 修改。无密码的开发模式仅供本机使用。

### 内网部署

将示例 `10.0.0.10` 换成服务器实际拥有的内网 IPv4 地址：

```bash
python scripts/start_intranet.py --address 10.0.0.10 --port 8780
```

启动器生成团队密码，并在后台启动一个服务进程，同时监听本机回环地址和指定内网地址。连接该内网且能访问端口的成员可打开 `http://服务器内网地址:8780/`。

- 登录凭据：`data/内网访问凭据.txt`。
- 私有配置：`data/intranet_config.json`，权限为 `600`，不提交到 Git。
- 服务日志：`data/intranet.log`；进程信息：`data/intranet_server.json`。
- 重复执行启动命令会检查已有实例。修改配置后，需在无活动任务时重启本项目服务。
- 机器重启后需重新执行启动命令。每个数据目录只运行一个服务进程，不使用多个 Uvicorn worker。

## 模型配置

服务读取环境变量；原生启动不会自动读取 `.env`。可复制 `.env.example` 为 `.env`，编辑后在可信终端加载：

```bash
set -a
source .env
set +a
```

内网启动器还支持在私有配置的 `model_environment` 中保存模型配置；其中的值会覆盖同名环境变量。密钥由服务端持有，不返回网页。

### 外部视觉接口

- Qwen 或兼容服务：设置 `QWEN_BASE_URL`、`QWEN_MODEL`，按需设置 `QWEN_API_KEY`；保持 `QWEN_AUTO_MANAGE=0`。
- DeepSeek：设置 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` 和 `DEEPSEEK_BASE_URL`。所选模型必须支持图像输入及 Chat Completions 协议。

适配器发送真实 JPEG 帧并校验九类分数。服务未配置、请求失败或输出格式不合法时明确报错，不生成占位标注。云端接口会接收抽取的视频帧。

### 本地 Qwen 按需加载

适用于 Linux + NVIDIA GPU。准备 Qwen2.5-VL-3B-Instruct 完整权重目录，先安装适合服务器驱动的 PyTorch CUDA 版本，再安装可选依赖：

```bash
python -m pip install -r requirements-qwen.txt
```

在与网站相同的 Python 环境中配置：

```bash
export QWEN_BASE_URL=http://127.0.0.1:8790/v1
export QWEN_MODEL=Qwen2.5-VL-3B-Instruct
export QWEN_MODEL_PATH=/absolute/path/to/Qwen2.5-VL-3B-Instruct
export QWEN_AUTO_MANAGE=1
export QWEN_IDLE_SECONDS=600
export STUDIO_PRESENCE_SECONDS=90
export QWEN_GPU_IDS=auto
export QWEN_MAX_INSTANCES=4
export STUDIO_MAX_CONCURRENT_JOBS=4
export QWEN_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

然后启动网站。权重只从本地目录读取；默认位置为项目内 `models/Qwen2.5-VL-3B-Instruct`。权重不随仓库提供。

只有 Qwen 标注任务触发加载，登录网站不会加载模型。自动调度优先选择空闲显存至少 10 GiB 的单卡，否则尝试空闲显存至少 5.5 / 4.5 GiB 的两卡。此预算针对当前 3B FP16 桥接，不能作为其他模型的显存保证。也可用 `QWEN_GPU_IDS=0,3` 限定允许使用的卡。

默认最多同时执行 4 个任务、保有 4 个本地模型实例，实际并行数由可用显存决定。显存足够时，同一 GPU 可运行多个独立实例；双卡实例也参与逐卡预算检查。同一卡上的模型依次加载，加载完成后可同时推理。分配与加载前均检查显存，并在实测剩余显存之外，为每个已加载实例保留推理余量（主卡 2 GiB，第二张卡 1 GiB）；复用实例同样检查余量。空闲实例的权重已计入实际占用，不重复扣除权重预算。

显存不足的任务保持排队，每 10 秒重新尝试；加载或推理出现显存不足时保留已完成帧、释放该实例并重新排队。GPU 按扣除预留余量后的显存排序，不按算力利用率排序，也无法预留其他进程的显存。同卡并行不保证吞吐量按任务数增长。

各实例分别计时，空闲 600 秒释放；无人在线且无待处理任务时提前释放。退出登录不会中止已提交任务。关闭页面后最迟按 90 秒心跳超时确认离线。仅管理本平台启动的模型子进程，不接管其他 GPU 任务。

模型状态位于 `data/qwen_server.json`，每个实例的日志分别位于 `data/qwen-实例编号.log`。每个实例使用独立回环端口（从配置的端口开始寻找空闲端口），每个实例内部逐帧处理，多个实例可同时推理。外部 Qwen／DeepSeek 接口仍按每种服务一条请求流执行，且计入总任务上限。AI 输出应经过人工复核。

## 项目结构

```text
studio/                 API、存储、标注语义、任务队列与模型生命周期
static/                 网页界面
scripts/start_intranet.py  内网后台启动器
scripts/start_preview.py 本机后台启动器
scripts/serve_qwen.py    本地 Qwen 视觉推理桥接
scripts/import_v4.py     特定 RoadSens v4 格式的只读导入工具
tests/                  自动测试
run.py                  网页服务入口
requirements*.txt       基础、测试与可选推理依赖
Dockerfile              轻量网页镜像，不包含 CUDA 或模型权重
compose.yaml / Caddyfile 可选 HTTPS 部署模板
```

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

测试覆盖上传与真实帧解码、人工修正、原始记录保留、候选筛选、导出、登录、配置快照、任务部分失败、模型资源等待和释放。使用临时数据，不需要真实模型或 GPU。

## 导入、备份与部署边界

已有特定 v4 导出格式可使用：

```bash
python scripts/import_v4.py --source /path/to/anomaly_review_v4 --dataset-dir /path/to/dataset
```

导入复制标注并记录源文件哈希，仍引用源视频和代理视频路径。迁移时需同时迁移媒体；该脚本不是任意 JSON 标注的通用导入器。

`data/` 保存数据库、视频、缓存帧和私有配置。备份 SQLite 应使用 SQLite backup API，或停服后复制完整数据目录，避免遗漏 WAL。仓库不包含用户数据、运行日志、凭据或权重。

默认单视频最大 1 GiB、最长 30 分钟，单任务最多 10000 帧。推荐上传 MP4 视频。

`compose.yaml` 是可选公网 HTTPS 模板，需另行配置域名、密码和模型服务。内网部署使用上面的原生启动方式即可；Docker 镜像内保持 `QWEN_AUTO_MANAGE=0`，通过私有接口连接外部推理服务。
