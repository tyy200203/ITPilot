"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 混合检索：向量召回 + BM25 关键词召回，并用 RRF 合并排名
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
import math
import os
import re
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    关键词索引在本进程缓存 30 秒；文档写入和删除时立即失效。
    不需要额外调用 Anthropic Embeddings API。
    """

    # 使用带业务和版本号的新 collection，避免 Docker 持久卷中旧的电商知识
    # 与 SupportWeave 的 IT 知识混在一起。旧 collection 不会被删除，仍可回滚读取。
    DEFAULT_COLLECTION_NAME = "supportweave_knowledge_base_v1"
    COLLECTION_NAME = DEFAULT_COLLECTION_NAME
    DEFAULT_CORPUS_ID = "supportweave-it-service-v1"
    KEYWORD_CACHE_TTL_S = 30
    RRF_K = 60

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
                "description": "SupportWeave 企业 IT 服务知识库",
                "schema_version": "1",
            },
        )
        self._keyword_lock = threading.RLock()
        self._keyword_cache = None

        # 仅首次初始化时导入示例。删除最后一篇后重启也不应再次出现。
        if (self._collection.metadata or {}).get("seeded") != "true":
            if self._collection.count() == 0:
                self._load_default_docs()
            self._collection.modify(metadata={**(self._collection.metadata or {}), "seeded": "true"})

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    @staticmethod
    def document_id(title: str) -> str:
        """A title identifies one managed document, including legacy chunks."""
        return hashlib.sha256(title.strip().encode("utf-8")).hexdigest()

    def upsert_document(self, title: str, content: str) -> Dict[str, Any]:
        title = title.strip()
        content = content.strip()
        if not title or not content:
            raise ValueError("文档标题和内容不能为空")
        chunks = self._chunk_text(content, chunk_size=500)
        previous = self._collection.get(where={"title": title}, include=["documents", "metadatas"])
        old_ids = previous.get("ids") or []
        old_chunks = [item for _, item in sorted(zip(
            [meta.get("chunk_index", 0) for meta in previous.get("metadatas") or []],
            previous.get("documents") or [],
        ))]
        doc_id = self.document_id(title)
        version = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if old_ids and old_chunks == chunks:
            return {"document_id": doc_id, "title": title, "version": version,
                    "status": "unchanged", "added_chunks": 0, "chunk_count": len(chunks)}

        ids = [f"{doc_id}:{version}:{index}" for index in range(len(chunks))]
        metadata = [{"document_id": doc_id, "title": title, "version": version,
                     "chunk_index": index, "total_chunks": len(chunks)}
                    for index in range(len(chunks))]
        self._collection.add(ids=ids, documents=chunks, metadatas=metadata)
        if old_ids:
            self._collection.delete(ids=old_ids)
        self._keyword_cache = None
        status = "updated" if old_ids else "created"
        logger.info("知识库文档 %s: %s (%d chunks)", status, title, len(chunks))
        return {"document_id": doc_id, "title": title, "version": version,
                "status": status, "added_chunks": len(chunks), "chunk_count": len(chunks)}

    def upsert_documents(self, documents: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        if any(not isinstance(doc, dict) or not isinstance(doc.get("title"), str)
               or not isinstance(doc.get("content"), str) or not doc["title"].strip()
               or not doc["content"].strip() for doc in documents):
            raise ValueError("每篇文档都必须提供非空标题和内容")
        return [self.upsert_document(doc.get("title", ""), doc.get("content", "")) for doc in documents]

    async def upsert_documents_async(self, documents: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.upsert_documents, documents)

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """Compatibility wrapper; returns only chunks actually written."""
        return sum(item["added_chunks"] for item in self.upsert_documents(documents))

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def list_documents(self) -> List[Dict[str, Any]]:
        titles: Dict[str, Dict[str, Any]] = {}
        offset = 0
        while True:
            page = self._collection.get(limit=500, offset=offset, include=["metadatas"])
            ids = page.get("ids") or []
            for meta in page.get("metadatas") or []:
                title = meta.get("title", "")
                if not title:
                    continue
                item = titles.setdefault(title, {"document_id": self.document_id(title),
                                                  "title": title, "status": "active",
                                                  "version": meta.get("version", "legacy"),
                                                  "chunk_count": 0})
                item["chunk_count"] += 1
            offset += len(ids)
            if len(ids) < 500:
                break
        return sorted(titles.values(), key=lambda item: item["title"])

    async def list_documents_async(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_documents)

    def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        item = next((item for item in self.list_documents() if item["document_id"] == document_id), None)
        if item is None:
            return None
        result = self._collection.get(where={"title": item["title"]}, include=["documents", "metadatas"])
        parts = sorted(zip(result.get("metadatas") or [], result.get("documents") or []),
                       key=lambda pair: pair[0].get("chunk_index", 0))
        separator = "\n" if item["version"] == "legacy" else ""
        return {**item, "content": separator.join(text for _, text in parts)}

    async def get_document_async(self, document_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_document, document_id)

    def delete_document(self, document_id: str) -> bool:
        item = next((item for item in self.list_documents() if item["document_id"] == document_id), None)
        if item is None:
            return False
        ids = self._collection.get(where={"title": item["title"]}, include=[]).get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
            self._keyword_cache = None
        return True

    async def delete_document_async(self, document_id: str) -> bool:
        return await asyncio.to_thread(self.delete_document, document_id)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """向量和 BM25 分别召回，再按 RRF 排名合并片段 ID。"""
        if not query.strip() or top_k <= 0:
            return []
        candidate_count = max(10, top_k * 2)
        results = self._collection.query(
            query_texts=[query],
            n_results=candidate_count,
        )

        vector_rows = [
            {"chunk_id": str(chunk_id), "title": (meta or {}).get("title", ""),
             "content": doc, "chunk": (meta or {}).get("chunk_index", 0),
             "vector_score": round(1.0 - float(dist), 4)}
            for chunk_id, doc, meta, dist in zip(
                (results.get("ids") or [[]])[0],
                (results.get("documents") or [[]])[0],
                (results.get("metadatas") or [[]])[0],
                (results.get("distances") or [[]])[0],
            )
        ]
        keyword_rows = self._keyword_search(query, candidate_count)
        combined: Dict[str, Dict[str, Any]] = {}
        for source, rows in (("vector", vector_rows), ("keyword", keyword_rows)):
            for rank, row in enumerate(rows, start=1):
                chunk_id = row["chunk_id"]
                entry = combined.setdefault(chunk_id, {**row, "_rrf": 0.0, "retrieval_sources": []})
                entry["_rrf"] += 1.0 / (self.RRF_K + rank)
                entry["retrieval_sources"].append(source)
                if source == "vector":
                    entry["vector_score"] = row["vector_score"]
        ranked = sorted(combined.values(), key=lambda row: (-row["_rrf"], row["chunk_id"]))
        for row in ranked:
            row["score"] = round(row.pop("_rrf") * (self.RRF_K + 1) / 2, 4)
        return ranked[:top_k]

    @staticmethod
    def _terms(text: str) -> List[str]:
        """保留英文/数字词，中文使用二元字片段，兼容没有空格的 IT 问句。"""
        terms = []
        for match in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
            if "\u4e00" <= match[0] <= "\u9fff":
                terms.extend(match[index:index + 2] for index in range(len(match) - 1))
                if len(match) == 1:
                    terms.append(match)
            else:
                terms.append(match)
        return terms

    def _keyword_index(self) -> Dict[str, Any]:
        lock = getattr(self, "_keyword_lock", None)
        if lock is None:  # 兼容无需连接 ChromaDB 的单元测试实例
            lock = self._keyword_lock = threading.RLock()
        with lock:
            cached = getattr(self, "_keyword_cache", None)
            if cached and time.monotonic() - cached["built_at"] < self.KEYWORD_CACHE_TTL_S:
                return cached
            rows = []
            df = Counter()
            offset = 0
            while True:
                page = self._collection.get(limit=500, offset=offset,
                                            include=["documents", "metadatas"])
                ids = page.get("ids") or []
                for chunk_id, doc, meta in zip(ids, page.get("documents") or [],
                                               page.get("metadatas") or []):
                    meta = meta or {}
                    term_counts = Counter(self._terms(f'{meta.get("title", "")} {doc}'))
                    df.update(term_counts.keys())
                    rows.append({"chunk_id": str(chunk_id), "title": meta.get("title", ""),
                                 "content": doc, "chunk": meta.get("chunk_index", 0),
                                 "terms": term_counts, "length": sum(term_counts.values())})
                offset += len(ids)
                if len(ids) < 500:
                    break
            cached = {"built_at": time.monotonic(), "rows": rows, "df": df,
                      "avg_length": sum(row["length"] for row in rows) / len(rows) if rows else 0}
            self._keyword_cache = cached
            return cached

    def _keyword_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        index = self._keyword_index()
        rows = index["rows"]
        if not rows:
            return []
        terms = set(self._terms(query))
        scored = []
        for row in rows:
            score = 0.0
            matched_terms = terms.intersection(row["terms"])
            if len(terms) > 1 and len(matched_terms) < 2 and not any(term.isdigit() for term in matched_terms):
                continue
            for term in matched_terms:
                frequency = row["terms"].get(term, 0)
                if frequency:
                    idf = math.log1p((len(rows) - index["df"][term] + 0.5)
                                     / (index["df"][term] + 0.5))
                    normalizer = frequency + 1.2 * (0.25 + 0.75 * row["length"] / index["avg_length"])
                    score += idf * frequency * 2.2 / normalizer
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
        return [{key: value for key, value in row.items() if key not in ("terms", "length")}
                for _, row in scored[:limit]]

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
        titles = [item["title"] for item in self.list_documents()]
        return {
            "collection_name": self.collection_name,
            "corpus_id": self.DEFAULT_CORPUS_ID,
            "total_chunks": total_chunks,
            "document_titles": titles,
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
        """在句末附近切片，同时保留原文以便编辑时准确还原。"""
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= chunk_size:
                cut = len(remaining)
            else:
                cut = max(remaining.rfind(mark, chunk_size // 2, chunk_size) + 1
                          for mark in "。！？.!?\n")
                if cut == 0:
                    cut = chunk_size
            chunks.append(remaining[:cut])
            remaining = remaining[cut:]
        return chunks

    def _load_default_docs(self) -> None:
        """导入 SupportWeave 默认 IT 服务知识。"""
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
