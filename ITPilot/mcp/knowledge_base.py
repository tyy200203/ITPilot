"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
import os
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    # 使用带业务和版本号的新 collection，避免 Docker 持久卷中旧的电商知识
    # 与 ITPilot 的 IT 知识混在一起。旧 collection 不会被删除，仍可回滚读取。
    DEFAULT_COLLECTION_NAME = "itpilot_knowledge_base_v1"
    COLLECTION_NAME = DEFAULT_COLLECTION_NAME
    DEFAULT_CORPUS_ID = "itpilot-it-service-v1"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        collection_name: Optional[str] = None,
    ):
        self.collection_name = (
            collection_name
            or os.getenv("KNOWLEDGE_COLLECTION_NAME")
            or self.DEFAULT_COLLECTION_NAME
        )
        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 使用服务端时不传 embedding_function，让服务端处理
        # 本地模式时也不传，使用 ChromaDB 默认的（会触发模型下载）
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={
                "description": "ITPilot 企业 IT 服务知识库",
                "schema_version": "1",
            },
        )

        # 如果知识库为空，导入默认文档
        if self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({"title": title, "chunk_index": i, "total_chunks": len(chunks)})

        if ids:
            # ChromaDB 会自动生成 Embedding
            self._collection.add(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库导入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        ChromaDB 内部自动将 query 转为向量，与存储的文档向量做余弦相似度匹配。
        """
        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    def stats(self) -> Dict[str, Any]:
        """返回当前实际 collection 及其中的文档标题，便于核验部署版本。"""
        total_chunks = self._collection.count()
        titles = set()
        if total_chunks:
            result = self._collection.get(
                limit=min(total_chunks, 1000),
                include=["metadatas"],
            )
            for metadata in result.get("metadatas") or []:
                if isinstance(metadata, dict) and metadata.get("title"):
                    titles.add(str(metadata["title"]))
        return {
            "collection_name": self.collection_name,
            "corpus_id": self.DEFAULT_CORPUS_ID,
            "total_chunks": total_chunks,
            "document_titles": sorted(titles),
        }

    async def stats_async(self) -> Dict[str, Any]:
        """异步读取知识库部署核验信息。"""
        return await asyncio.to_thread(self.stats)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """导入 ITPilot 默认 IT 服务知识。"""
        default_docs = [
            {
                "title": "VPN 常见错误码排查",
                "content": (
                    "VPN 错误 691 通常表示用户名、密码或域信息验证失败。"
                    "先确认账号未锁定、密码未过期并检查登录域，再重新输入凭据。"
                    "错误 651 通常与本地网络适配器、拨号组件或驱动异常有关，可重启适配器并检查驱动。"
                    "错误 720 常见于 VPN 协议栈或虚拟网卡异常，可重建 VPN 配置并检查系统网络组件。"
                    "排查时应记录操作系统版本、VPN 客户端版本、错误码、发生时间和网络环境。"
                    "禁止要求员工提供密码、短信验证码或完整密钥。"
                ),
            },
            {
                "title": "办公网络与 DNS 排查",
                "content": (
                    "办公网络异常应先确认影响范围，是单台设备、单个区域还是全公司。"
                    "依次检查网线或 Wi-Fi 状态、IP 地址、默认网关、DNS 配置和代理设置。"
                    "能访问 IP 但无法访问域名时优先检查 DNS；完全无法联网时检查网卡、地址获取和交换网络。"
                    "内网系统不可用但公网正常时，应核验 VPN、内网路由和目标服务状态。"
                    "多人同时故障或核心业务中断时，应提高紧急度并创建 IT 工单。"
                ),
            },
            {
                "title": "打印机脱机处理",
                "content": (
                    "打印机显示脱机时，先确认电源、网线或 USB 连接以及设备面板是否有报错。"
                    "网络打印机需检查设备 IP 是否可达，并确认电脑使用的打印端口与当前 IP 一致。"
                    "清理卡住的打印任务，重新启动打印后台处理服务后再打印测试页。"
                    "同一打印机影响多人时记录资产编号、位置和故障时间并创建工单。"
                ),
            },
            {
                "title": "Windows 蓝屏与无法启动排查",
                "content": (
                    "Windows 蓝屏应记录停止代码、发生时间、近期驱动或软件变更，避免反复强制关机。"
                    "可先移除新接入的外设并尝试进入安全模式，再检查驱动、系统更新和磁盘健康。"
                    "设备无法启动时确认电源、指示灯和启动提示，不应指导用户自行拆机。"
                    "涉及磁盘异响、数据丢失风险或多次启动失败时立即停止高风险操作并升级处理。"
                ),
            },
            {
                "title": "办公软件安装与升级规范",
                "content": (
                    "Office、VPN 客户端和其他办公软件应从公司软件中心或批准的软件源安装。"
                    "处理申请时收集员工工号、设备资产编号、操作系统版本、软件名称和业务用途。"
                    "需要许可证、管理员权限或不在软件白名单中的软件，必须经过审批。"
                    "不得提供绕过授权、关闭安全软件或使用未知安装包的方案。"
                ),
            },
            {
                "title": "员工账号开通与登录异常",
                "content": (
                    "新员工账号开通需核验员工工号、部门、直属负责人、入职日期和目标系统。"
                    "登录错误 401 表示认证未通过，应检查账号状态、密码是否过期、登录域和客户端时间。"
                    "错误 403 表示身份已识别但权限不足，应转入权限申请流程，不应反复重置密码。"
                    "账号解锁、重置和多因素认证变更必须通过身份核验，禁止索取密码或验证码。"
                ),
            },
            {
                "title": "GitLab 与内部系统权限申请",
                "content": (
                    "权限申请需提供员工工号、目标系统、所需角色、业务理由、使用期限和审批人。"
                    "系统遵循最小权限原则，优先授予完成工作所需的最低权限。"
                    "管理员权限、跨部门数据权限和生产环境权限必须升级审批。"
                    "服务台只能生成申请草稿或工单，不得宣称权限已经开通。"
                ),
            },
            {
                "title": "IT 工单优先级与升级规则",
                "content": (
                    "工单应记录问题摘要、员工工号、联系方式、资产编号、影响范围、错误码和已执行步骤。"
                    "单人一般问题通常为低或中优先级；多人受影响、关键岗位中断通常为高优先级。"
                    "全公司中断、核心系统不可用、数据泄露或疑似安全事件属于严重问题，应立即升级。"
                    "自动化助手只能生成工单草稿；只有后端工单系统确认成功后才能告知用户已创建。"
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇文档")
