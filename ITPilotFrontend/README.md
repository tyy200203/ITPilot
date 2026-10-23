# ITPilot 前端工作台

基于 Vue 3 和 Vite 的企业 IT 服务工作台，对接相邻后端目录中的 Python/FastAPI 服务。页面提供 IT 问答、路由与工具轨迹、知识库管理、运行状态及评测结果展示。当前前端只支持 Python 后端，不提供 Java 后端切换。

## 本地开发

先按[后端 README](../ITPilot/README.md)启动后端，确认 `http://localhost:8000/health` 返回 `ITPilot`，再在本目录运行：

```powershell
npm ci
npm run dev
```

访问 `http://localhost:5173`。开发服务器将 `/api/python` 代理到 `http://localhost:8000`；如后端地址不同，可在启动前设置 `VITE_PYTHON_API_URL`。

## Docker 演示部署

后端与前端各有一份 `docker-compose.yml`，需要分别在对应目录执行。先在后端目录启动并验收：

```powershell
Set-Location ..\ITPilot
docker compose up -d --build
Invoke-RestMethod http://localhost:8000/health
```

然后回到前端目录构建静态文件并启动网页容器：

```powershell
Set-Location ..\ITPilotFrontend
npm ci
npm run build
docker compose up -d --build
docker compose ps
```

访问 `http://localhost:5174`。前端 Nginx 将 `/api/python/` 请求转发到宿主机的 `8000` 端口，因此后端必须先可用。前端的 `Dockerfile` 复制已有的 `dist/`，修改前端源码后应重新运行 `npm run build`，再重建前端容器。

## 演示检查

1. 页面顶部显示 ITPilot，服务状态与后端 `/health` 一致。
2. 对话页发送 IT 问题，检查回复、意图和主辅 Agent；复杂请求可查看工具轨迹。
3. 知识库页查看统计，检索已有 IT 文档，并可上传 `.txt`、`.md` 或 JSON 文件。
4. 评测页可查看结果；执行 `POST /eval/run` 会调用模型，可能耗时并产生费用。

页面地址 `5174`、后端直连地址 `8000` 和后端 Nginx 代理地址 `8080` 是三个不同入口。当前 Docker 配置面向本机演示；跨主机部署时需要重新配置前端到后端的代理地址。
