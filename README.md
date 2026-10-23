# ITPilot 企业 IT 服务多 Agent 系统

面向企业内部设备、网络、软件、账号权限及 IT 工单问题的问答与编排系统。后端采用 Python / FastAPI，前端采用 Vue 3 / Vite，使用 Redis 与 ChromaDB 管理会话和知识。

## 主要能力

- 意图识别与实体提取，以及四类 Agent 的角色路由和主辅协作。
- Agent 按需调用共享 RAG 工具，支持查询改写、并行召回、去重与 LLM 重排。
- 角色工具白名单、动态 Skills 注入、缓存、超时、熔断与故障降级。
- 会话记忆、工具调用轨迹、运行监控与离线评测。

## 目录

```text
ITPilot/                  后端源码、Skills、测试与 Docker 配置
ITPilotFrontend/          前端源码、测试与 Docker 配置
```

本仓库不包含真实 API Key、本地数据库、虚拟环境、依赖安装目录或构建产物。评测数据集位于 `ITPilot/evaluation/datasets/`。

## 启动后端

在仓库根目录打开 PowerShell，确保 Docker Desktop 已启动：

```powershell
Set-Location .\ITPilot
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑本地 `.env`，填写自己的模型配置与 Redis 密码。模型端点须支持 Anthropic Messages 协议。随后运行：

```powershell
docker compose up -d --build
Invoke-RestMethod http://localhost:8000/health
```

接口文档：[http://localhost:8000/docs](http://localhost:8000/docs)。完整配置与接口说明见[后端 README](ITPilot/README.md)。

## 启动前端

安装 Node.js，在另一个 PowerShell 窗口中进入仓库根目录后运行：

```powershell
Set-Location .\ITPilotFrontend
npm ci
npm run build
docker compose up -d --build
```

访问 [http://localhost:5174](http://localhost:5174)。前端 Dockerfile 使用本地生成的 `dist/`，因此必须先完成 `npm run build`。开发模式与代理配置见[前端 README](ITPilotFrontend/README.md)。

## 测试与评测

后端测试位于 `ITPilot/tests/`，前端测试位于 `ITPilotFrontend/tests/`。离线评测程序和输入数据位于 `ITPilot/evaluation/`。部分测试及评测需要额外配置依赖服务或模型 API，模型调用可能产生费用。

## 功能边界

当前主要提供排障指导、申请草稿与升级交接信息。未接入真实企业后台时，不代表已执行资产查询、权限开通、软件安装或工单提交。
