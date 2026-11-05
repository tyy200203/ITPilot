# ITPilot 企业 IT 服务 Agent 系统

ITPilot 面向企业内部设备、网络、软件、账号权限和 IT 工单问题，提供多 Agent 协作的问答与处理指引。后端基于 FastAPI，使用 Redis 保存当前会话，使用 ChromaDB 保存知识库和长期记忆。

## 主要能力

- 结合 LLM、相似度和关键词规则识别 IT 意图，提取错误码、设备和账号等实体。
- 根据意图、实体和运行状态选择主 Agent；复合问题可由主辅 Agent 并发处理并合并结果。
- 按角色白名单提供工具。知识库检索是共享工具，由 Agent 根据问题和上下文决定是否调用，不在 API 层按意图强制预检索。
- 从 `skills/` 加载业务规范，约束排障、权限申请和升级交接流程。
- 提供监控摘要、工具调用轨迹和意图／对话质量评测接口。

| Agent | 职责 |
| --- | --- |
| GeneralAgent | IT 服务接待、通用咨询与需求澄清 |
| TechnicalAgent | 设备、网络和软件问题排查 |
| PermissionAgent | 账号开通、权限申请与审批边界 |
| EscalationAgent | 严重故障升级、IT 工单信息整理与交接 |

系统不会仅凭模型回复宣称已查询后台、安装软件、开通权限或提交工单；这些操作只有在接入并实际调用相应系统后才能确认。

## 运行环境与配置

- Docker Desktop（含 Docker Compose）
- 可调用 Anthropic Messages 协议的模型 API Key

首次使用时，在本目录的 PowerShell 中创建本地配置；已有 `.env` 时不覆盖：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，至少填写 `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL`、`REDIS_PASSWORD`、`ITPILOT_USER_TOKENS` 和 `ITPILOT_ADMIN_TOKEN`。用户令牌配置为 JSON 对象，例如 `{"u1001":"<独立随机令牌>"}`；管理员使用另一枚随机令牌。令牌须为不少于 24 字符的字母、数字、`_` 或 `-`，启动时会拒绝缺失、占位或重复的令牌。使用第三方兼容服务时，还需填写该服务的 **Anthropic Messages 兼容地址** `ANTHROPIC_BASE_URL`；OpenAI Chat Completions 地址不能直接填入此变量。不要提交 `.env` 或把密钥展示在截图中。

可在 PowerShell 中运行下面的命令两次，分别生成用户和管理员令牌，再填入本地 `.env`：

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
([BitConverter]::ToString($bytes) -replace '-', '').ToLower()
$rng.Dispose()
```

## 访问控制

业务 API 使用 `Authorization: Bearer <令牌>`；`/health`、供 Prometheus 抓取的 `/metrics` 以及交互式 API 文档仍可公开访问。`ITPILOT_USER_TOKENS` 中的用户令牌可用于对话、会话归档、检索、知识统计和本人请求的工具 Trace；请求中的 `user_id` 必须与令牌绑定的用户一致，省略时由服务端确定。管理员令牌用于知识导入、Skills 查看与重载、运行监控、最近 Trace 列表及评测。管理员令牌不能代替用户令牌进行对话。

前端“连接配置”中分别填写用户令牌与管理员令牌。令牌只保存在当前浏览器标签的 `sessionStorage`，不会写进前端的长期设置；关闭标签后需重新填写。多人使用或跨网络部署时应配置 HTTPS，并按实际前端地址设置 `ITPILOT_CORS_ORIGINS`。不要将管理员令牌提供给普通用户。

## 启动与验收

在后端目录运行：

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail 50 itpilot
Invoke-RestMethod http://localhost:8000/health
```

`/health` 应返回 `service: ITPilot`，并包含版本、构建编号、知识库标识及 `general_0`、`technical_0`、`permission_0`、`escalation_0` 四个 Agent。首次构建需要下载 Python 依赖和 ChromaDB 使用的模型文件，耗时取决于网络环境。

| 入口 | 地址 |
| --- | --- |
| API 与健康检查 | `http://localhost:8000/health` |
| Swagger API 文档 | `http://localhost:8000/docs` |
| 后端 Nginx 代理 | `http://localhost:8080` |
| Prometheus | `http://localhost:9090` |

Redis、ChromaDB 和 Prometheus 的宿主机端口仅绑定 `127.0.0.1`，分别为 `6379`、`8001`、`9090`；它们不是对外网页入口。Redis 密码必须在 `.env` 中显式设置。前端在相邻的 `ITPilotFrontend` 目录中单独构建和启动，页面地址为 `http://localhost:5174`；参见[前端部署说明](../ITPilotFrontend/README.md)。

修改后端代码后，可运行 `./rebuild-itpilot.ps1` 重新构建应用容器并检查运行版本。该脚本不负责构建前端。停止服务使用 `docker compose down`；不要在仍需保留 Redis、ChromaDB 数据时添加 `-v`。

## 核心链路与接口

`POST /chat` 的处理顺序为：读取会话记忆 → 识别意图与实体 → 路由主辅 Agent → Agent 按需调用工具或知识库 → 合成回复 → 写回会话记忆。RAG 不是每次请求都强制执行。

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 服务版本、构建与 Agent 状态 |
| `POST /chat` | IT 问答与多 Agent 编排（用户令牌） |
| `POST /conversation/archive` | 归档当前会话 |
| `POST /search` | 独立验证查询改写、并行召回与重排链路 |
| `POST /knowledge/add`、`POST /knowledge/upload` | 导入文本或文件（管理员令牌） |
| `GET /knowledge/stats` | 知识库集合与文档统计 |
| `GET /skills`、`POST /skills/reload` | 查看或重载业务规范 |
| `GET /trace/tool/{request_id}` | 查询单次请求的工具轨迹 |
| `GET /monitor`、`GET /metrics` | 运行摘要与 Prometheus 指标入口 |
| `POST /eval/run` | 运行意图及对话质量评测（管理员令牌）；调用模型可能产生费用 |

知识库上传支持 `.txt`、`.md` 和包含 `title`、`content` 的 JSON 文档数组；单个上传文件限制为 10 MB。运行中的知识库默认会导入 IT 服务示例文档，已有数据保存在 ChromaDB 中，不会因重建应用容器自动清空。

## 代码结构

```text
api/main.py                    FastAPI 入口和接口定义
agents/agent_orchestrator.py   Agent 路由、执行与结果合并
agents/tools.py                角色工具定义及白名单
core/intent_recognizer.py      意图识别与实体提取
core/skill_loader.py           Skills 加载与匹配
mcp/tool_manager.py            工具超时、缓存、熔断和检索编排
mcp/knowledge_base.py          ChromaDB 知识库
memory/conversation_memory.py  Redis 与 ChromaDB 记忆
monitor/performance_monitor.py 在线统计与路由反馈
evaluation/evaluator.py        意图与对话质量评测
skills/                        IT 服务处理规范
tests/                         自动化回归测试
```
