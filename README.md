# MDE-LAB｜多模态数字员工平台（IT 服务台方向）

一个**简化但完整**的多模态数字员工演示/教学项目：以企业 IT 服务台为业务场景，
用真实技术栈实现「多模态感知 → 多模态 RAG 知识库 → CrewAI 多 Agent 编排 →
MCP 工单系统 → 人机协同转人工 → 三角色前端」的端到端闭环。

需求与设计全文见 [docs/多模态数字员工平台.md](./docs/多模态数字员工平台.md)。

---

## 一、核心能力

| 模块 | 说明 |
|---|---|
| 多模态感知 | ASR 语音转写、OCR / VLM 截图理解、情绪识别、TTS 语音合成 |
| 多模态 RAG | **文本优先**：文本原文 / 表格 Markdown / 图片配图描述（入库期由 VLM 生成）统一进**单一文本向量空间**（Qwen3-VL-Embedding-8B），单集合一次召回覆盖全模态，再用 Reranker 精排 top_n=3 |
| CrewAI 多 Agent | 一线客服 Agent → 工单处理 Agent → 质检 Agent，`Process.sequential` 编排。由 `CREW_ENABLED` 开关控制：`false`（默认）走中台直连 LLM **流式生成**（逐字输出、首字快）；`true` 走 CrewAI 编排（Agent 自主调用检索/截图/工单工具，回复同样**逐字流式**，`Crew(stream=True)`）。见 `core/ai/crew_agents.py` 模块说明与 §4.2 |
| MCP 工单系统 | **唯一外部系统**，以独立 MCP Server（stdio）暴露 8 个工具：建单 / 查单 / 查用户工单列表 / 查全部工单 / 删除工单 / 改状态 / 重置密码 / 申请软件权限 |
| 人机协同 | 6.3 四类转人工触发规则、坐席工作台、转回 AI 并传递备注 |
| 三角色前端 | Vue 3 SPA + Vue Router `meta.roles` 路由守卫，用户端 / 坐席端 / 管理后台 |

## 二、技术栈

- **后端**：FastAPI + Uvicorn（端口 8000）、CrewAI、MCP（官方 SDK + FastMCP）、APScheduler
- **向量库**：ChromaDB（嵌入式 PersistentClient，**单进程独占**）
- **关系库**：SQLite（`./data/app.db`）；工单库 SQLite（本地默认 `mcp_servers/it_service/tickets.db`，容器内由 `TICKETS_DB_PATH` 映射到 `./data/tickets.db`）
- **模型**：LLM / 嵌入 / 重排 / VLM / ASR / OCR 走 OpenAI 兼容协议（「能力四元组」环境变量配置）；TTS 默认本地 edge-tts（免费、零部署），OCR / ASR 也可切本地引擎
- **前端**：Vue 3 + Vite + Vue Router + Pinia + Element Plus + ECharts（端口 3000）

## 三、快速开始

### 3.1 一键启动（推荐）

完成一次性依赖安装后，可用 `scripts/` 下的脚本拉起服务：

```powershell
# Windows
.\scripts\start.ps1                  # 新窗口启动中台(8000) + 前端(3000)
.\scripts\start.ps1 -Target backend  # 只启动中台（当前窗口前台运行）
.\scripts\stop.ps1                   # 停止 8000 / 3000
```

```bash
# Linux / macOS
./scripts/start.sh                   # 同时启动中台 + 前端
./scripts/start.sh backend           # 只启动中台
./scripts/stop.sh                    # 停止 8000 / 3000
```

脚本会自动检查 `.env`（缺失时从 `.env.example` 复制并告警）、检测 `.venv`、
并在首次运行时安装前端依赖。

### 3.2 手动启动（本地开发）

```bash
# 1. 安装依赖（Python 3.11+）
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS
pip install -r requirements.txt

# 前端依赖
cd frontend && npm install && cd ..

# 2. 配置环境变量
copy .env.example .env            # Windows
# cp .env.example .env            # Linux / macOS
# 编辑 .env，填入 SILICONFLOW_API_KEY（或按能力分别填 *_API_KEY；各能力 _API_KEY 留空即回退）

# 3. 启动中台（端口 8000）
python -m core.main

# 4. 另开终端启动前端（端口 3000，代理 /api 与 /ws 到 8000）
cd frontend && npm run dev
```

### 3.3 Docker Compose

```bash
copy .env.example .env            # 填入 API Key

# 容器部署文件统一在 deploy/ 下（core 与 frontend 两个镜像 + 编排）
docker compose -f deploy/docker-compose.yml up -d

# 演示账号已在启动时由 lifespan 自动差量补种（无需手动调用）；
# 知识库登录 admin 后在「知识库管理」页一键播种（或 admin 鉴权后 POST /api/admin/knowledge/seed）
```

- 用户端：http://localhost:3000/login
- 接口文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/health

### 3.4 演示账号

| 账号 | 密码 | 角色 |
|---|---|---|
| `user01` / `user02` | `123456` | 用户（普通员工） |
| `agent01` | `123456` | 坐席（人工客服） |
| `admin` | `123456` | 管理员 |

## 四、目录结构

```text
.
├── core/                     # 中台（MCP Host）
│   ├── main.py               # 入口：配置自检 → 建库 → MCP 常驻连接 → 定时任务 → 路由
│   ├── config.py             # pydantic-settings 统一加载 .env + 启动自检
│   ├── api/                  # 路由：auth / chat / agent / admin / ws 五组
│   ├── infra/                # 基础设施：db / auth / middleware / media / ws_manager /
│   │                         #   scheduler / async_bridge / mcp_client / mcp_bridge / init_data
│   ├── ai/                   # 智能编排：llm / crew_agents / crew_tools
│   ├── rag/                  # 多模态 RAG：embedding / vector_store / multimodal / multimodal_rag / parsers
│   └── service/              # 业务编排：session_router / session_flow / turn_context / handoff / analytics / seed_knowledge
├── mcp_servers/it_service/   # 工单 MCP Server（唯一外部系统）
├── eval/                     # 全栈评测（ASR / TTS / VLM / 检索四套件，admin「评测」页调用）
├── data/                     # 知识库文档、ChromaDB、媒体、业务库（数据卷）
├── tests/                    # pytest：MCP / RAG / 转人工 / 多模态 ASR / 会话媒体 / 评测指标 / 容器配置
├── scripts/                  # 本地启动 / 停止脚本（start.ps1|start.sh|stop.ps1|stop.sh）
├── frontend/                 # Vue 3 SPA
├── deploy/                   # 容器部署：Dockerfile.core / Dockerfile.frontend / docker-compose.yml
└── requirements.txt
```

## 五、API 概览

| 分组 | 端点 | 说明 |
|---|---|---|
| 认证 | `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`、`POST /api/auth/init` | 登录 / 登出 / 当前用户 / 初始化账号（admin，启动时已自动补种） |
| 用户端 | `GET/POST /api/conversations`、`GET /api/conversations/{id}/messages`、`DELETE /api/conversations/{id}`、`POST /api/conversations/{id}/close`、`GET /api/tickets`、`POST /api/media/upload`、`GET /api/media/{asset_id}` | 会话列表 / 建会话 / 消息 / 删除会话 / 结束会话 / 我的工单 / 上传与读取媒体 |
| 坐席端 | `GET /api/agent/queue`、`GET /api/agent/sessions`、`GET /api/agent/session/{id}`、`GET /api/agent/session/{id}/assist`、`POST /api/agent/session/{id}/join`、`/reply`、`/return_to_ai`、`/end_service`、`GET/POST /api/agent/status` | 待接入队列 / 服务中会话 / 会话详情 / AI 摘要与推荐（独立接口）/ 接入 / 回复 / 转回 AI / 结束服务 / 在线状态 |
| 管理端 | `GET /api/admin/dashboard`、`/logs*`、`/tools*`、`/knowledge*`（upload / rebuild / test_search / seed / stats / chunks）、`/tickets*`、`/users*`、`/eval*`（suites / run / runs / baseline） | 数据看板 / 对话日志 / 工具调用日志 / 知识库管理 / 工单管理 / 用户管理 / 全栈评测 |
| WebSocket | `/ws/user`、`/ws/agent`、`/ws/admin` | token 走 query 参数；角色分组广播 |

## 六、三角色联动演示流程

用三个浏览器窗口分别登录三个角色：

```text
[用户] 张三登录 → “VPN 连不上”
        ↓
[管理] 数据看板实时对话量 +1
        ↓
[用户] AI 回复 → 继续追问 → 情绪激动（触发转人工）
        ↓
[坐席] 待接入队列出现新会话（含 AI 摘要与转接原因）→ 点击接入
        ↓
[用户] 提示“人工客服已接入” → 坐席回复 → 用户收到
        ↓
[管理] 对话日志看到完整记录 / 工具调用日志看到 MCP 调用
        ↓
[坐席] “转回 AI”并填写备注 → 会话交还
        ↓
[用户] 继续提问 → AI 基于坐席备注回复
```

多模态链路：上传一张报错截图 → 命中知识库图像/表格块 → **生成时以图片配图描述文本参与**
（文本优先，见文档 7.7）→ 回复带图（原图作附件）+ 末尾附「参考来源」。

## 七、测试

```bash
python -m pytest -q
```

| 用例 | 覆盖范围 |
|---|---|
| `tests/test_mcp_server.py` | 工单 MCP Server 的 6 个工具（建单 / 查单 / 查用户工单列表 / 改状态 / 重置密码 / 申请权限；Server 共 8 个，`list_tickets` / `delete_ticket` 暂未覆盖） |
| `tests/test_rag.py` | 表格转 Markdown、图片降采样、文档解析、向量增删查、结果装配（附件 / 引用）；**素材驱动**：表格型/图文混合/扫描型 PDF、Word、单图各自落库模态；text_only 过滤、工具**保留最优命中**（回归） |
| `tests/test_handoff.py` | 四类转人工触发规则、会话超时关闭 |
| `tests/test_multimodal_asr.py` | ASR 语音转写边界（空音频、API 失败、空文本、正常返回去空白） |
| `tests/test_session_media.py` | 会话媒体落库、转发坐席携带媒体、ASR 失败路由 |
| `tests/test_eval_metrics.py` | 评测指标（CER / WER / Recall / MRR / nDCG / 字段准确率 / 关键点命中 等） |
| `tests/test_dockerfile.py` | 容器配置（core 拷贝 eval、compose 引用、前端 context 路径、不打包 tests、.dockerignore 嵌套 db） |

## 八、配置说明

模型配置以「能力四元组」为主：`{能力}_PROVIDER / _BASE_URL / _MODEL / _API_KEY`。
具备**完整四元组**的是 LLM / EMBEDDING / ASR / OCR（均走 OpenAI 兼容协议）；
**VLM** 为三元组（无 `_PROVIDER`），**RERANKER** 用 `RERANKER_ENABLED` 开关控制启停，
**TTS** 仅 `TTS_PROVIDER` / `TTS_VOICE`（默认本地 `edge`）。
各能力 `_API_KEY` 留空时回退 `SILICONFLOW_API_KEY`，**换供应商只改 `.env`，代码零改动**。
可运行的最小配置见 [.env.example](./.env.example)；完整配置项与调优项见文档第十七章。

关键约束：

- `EMBEDDING_DIM=1024`，**换嵌入模型后必须重建索引**（管理端「知识库管理」→ 重建索引）。
- `RETRIEVAL_MIN_SCORE`（检索准入门槛）**当前未接入检索流程**，配置值留 `0` 即不生效、仅作预留；低置信度场景由**精排分驱动的转人工护栏**（`HANDOFF_MIN_CONFIDENCE`）兜底。如日后启用，须作用于精排分并按具体模型实测分布校准。
- `CREW_ENABLED` 决定会话回复走哪条路：`false`（默认）中台直连 LLM 逐字流式；`true` 走 CrewAI 多 Agent 编排（Agent 自主调工具，回复同样逐字流式）。两条路径共用同一套流式事件，前端无需切换。
- ChromaDB 嵌入式为**单进程独占**，`core` 必须单实例运行，不可开多 worker。
- `LLM_MODEL` 为**纯文本生成**（命中图片的配图描述、截图的预分析文字均以文本注入），无需视觉能力；**视觉能力（截图理解 / 配图描述）由 `VLM_MODEL` 承担**，未配置 VLM 时配图描述自动跳过、回退页正文。

## 九、非功能边界

| 类别 | 约束 |
|---|---|
| 并发规模 | ≤ 10 并发会话、≤ 3 并发坐席（单实例） |
| 响应时延 | 首条 AI 回复 P95 ≤ 3s；坐席消息投递 ≤ 1s |
| 数据保留 | 会话与消息 90 天；语音/图片原文件 30 天 |
| 隐私合规 | 全部为合成演示数据；图片**仅本地存储但会发送至视觉理解（VLM）API**（配图描述 / 截图分析），严禁使用真实敏感截图 |
| 成本控制 | 单会话 token 上限 20k；召回 top_k=10、精排 top_n=3；图片入库/描述前按 `EMBEDDING_MAX_IMAGE_SIDE`（像素）与 `EMBEDDING_MAX_IMAGE_BYTES`（字节，约 32KB）双重降采样 |
| 浏览器 | Chrome / Edge 最新版 |
