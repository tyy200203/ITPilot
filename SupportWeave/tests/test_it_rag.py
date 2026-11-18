import asyncio
import json

from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager, ToolResult


class _CaptureCollection:
    def __init__(self):
        self.ids = []
        self.documents = []
        self.metadatas = []
        self.metadata = {"seeded": "true"}

    def add(self, *, ids, documents, metadatas):
        assert not set(ids) & set(self.ids)
        self.ids.extend(ids)
        self.documents.extend(documents)
        self.metadatas.extend(metadatas)

    def delete(self, *, ids):
        kept = [(item_id, doc, meta) for item_id, doc, meta in
                zip(self.ids, self.documents, self.metadatas) if item_id not in ids]
        self.ids = [item[0] for item in kept]
        self.documents = [item[1] for item in kept]
        self.metadatas = [item[2] for item in kept]

    def count(self):
        return len(self.documents)

    def get(self, *, where=None, limit=None, offset=0, include=None):
        selected = [(item_id, doc, meta) for item_id, doc, meta in
                    zip(self.ids, self.documents, self.metadatas)
                    if not where or all(meta.get(key) == value for key, value in where.items())]
        selected = selected[offset:offset + limit if limit is not None else None]
        return {"ids": [item[0] for item in selected],
                "documents": [item[1] for item in selected],
                "metadatas": [item[2] for item in selected]}

    def query(self, *, query_texts, n_results):
        matches = [(item_id, doc, meta) for item_id, doc, meta in
                   zip(self.ids, self.documents, self.metadatas)
                   if query_texts[0] in doc][:n_results]
        return {"ids": [[item[0] for item in matches]],
                "documents": [[item[1] for item in matches]],
                "metadatas": [[item[2] for item in matches]],
                "distances": [[0.1 for _ in matches]]}


class _ExistingCollection:
    metadata = {"seeded": "true"}

    def count(self):
        return 1


class _CaptureChromaClient:
    def __init__(self):
        self.requested_name = None
        self.deleted_names = []

    def heartbeat(self):
        return 1

    def get_or_create_collection(self, *, name, metadata):
        self.requested_name = name
        return _ExistingCollection()

    def delete_collection(self, name):
        self.deleted_names.append(name)


class _RewriteResponse:
    def __init__(self, payload):
        self.content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]


class _RewriteClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.messages = self

    async def create(self, **kwargs):
        if self.error is not None:
            raise self.error
        return _RewriteResponse(self.payload)


def _default_docs():
    kb = object.__new__(KnowledgeBase)
    captured = []
    kb.add_documents = lambda documents: captured.extend(documents) or len(documents)
    kb._load_default_docs()
    return captured


def test_supportweave_uses_versioned_collection_without_reusing_legacy_name():
    assert KnowledgeBase.DEFAULT_COLLECTION_NAME == "supportweave_knowledge_base_v1"
    assert KnowledgeBase.DEFAULT_COLLECTION_NAME != "knowledge_base"


def test_collection_name_is_configurable_and_legacy_collection_is_not_deleted(monkeypatch):
    client = _CaptureChromaClient()
    monkeypatch.setenv("KNOWLEDGE_COLLECTION_NAME", "supportweave_knowledge_base_test")
    monkeypatch.setattr("mcp.knowledge_base.chromadb.Settings", lambda **kwargs: object())
    monkeypatch.setattr("mcp.knowledge_base.chromadb.HttpClient", lambda **kwargs: client)

    kb = KnowledgeBase()

    assert kb.collection_name == "supportweave_knowledge_base_test"
    assert client.requested_name == "supportweave_knowledge_base_test"
    assert client.deleted_names == []


def test_default_knowledge_is_it_only():
    documents = _default_docs()
    text = "\n".join(f"{item['title']} {item['content']}" for item in documents)

    assert len(documents) == 8
    for expected in ("VPN", "DNS", "打印机", "Windows", "软件", "账号", "GitLab", "IT 工单"):
        assert expected in text
    for obsolete in ("退款", "订单", "配送", "会员", "发票", "扣款"):
        assert obsolete not in text


def test_chunk_metadata_keeps_title_index_and_total():
    kb = object.__new__(KnowledgeBase)
    kb._collection = _CaptureCollection()
    content = "。".join(["打印机脱机需要检查网络端口"] * 60)

    added = kb.add_documents([{"title": "打印机排查", "content": content}])

    assert added > 1
    assert len(kb._collection.documents) == added
    assert all(len(chunk) <= 500 for chunk in kb._collection.documents)
    assert [meta["chunk_index"] for meta in kb._collection.metadatas] == list(range(added))
    assert all(meta["title"] == "打印机排查" and meta["total_chunks"] == added
               and meta["document_id"] == KnowledgeBase.document_id("打印机排查")
               for meta in kb._collection.metadatas)


def test_document_lifecycle_keeps_only_latest_version_and_delete_removes_search_results():
    kb = object.__new__(KnowledgeBase)
    kb._collection = _CaptureCollection()
    title = "VPN 排查"
    original = "账号错误。\n请确认登录域。" * 30
    revised = "新版处理流程：先确认账号状态。" * 30

    first = kb.upsert_document(title, original)
    assert first["status"] == "created"
    assert kb.get_document(first["document_id"])["content"] == original
    assert kb.upsert_document(title, original)["status"] == "unchanged"
    assert kb._collection.count() == first["chunk_count"]

    updated = kb.upsert_document(title, revised)
    assert updated["status"] == "updated"
    assert kb._collection.count() == updated["chunk_count"]
    assert kb.get_document(first["document_id"])["content"] == revised
    assert not kb.search("请确认登录域")
    assert kb.search("新版处理流程")
    assert kb.delete_document(first["document_id"]) is True
    assert not kb.search("新版处理流程")
    assert kb.list_documents() == []
    assert kb.delete_document(first["document_id"]) is False


def test_legacy_title_chunks_can_be_replaced():
    kb = object.__new__(KnowledgeBase)
    kb._collection = _CaptureCollection()
    kb._collection.add(ids=["old-id"], documents=["旧内容"],
                       metadatas=[{"title": "VPN 排查", "chunk_index": 0}])
    result = kb.upsert_document("VPN 排查", "新内容")
    assert result["status"] == "updated"
    assert kb._collection.ids == [f'{result["document_id"]}:{result["version"]}:0']


def test_default_documents_are_not_reseeded_after_all_documents_are_deleted(monkeypatch):
    collection = _CaptureCollection()
    collection.metadata = {}

    def modify(*, metadata):
        collection.metadata = metadata

    collection.modify = modify
    client = _CaptureChromaClient()
    client.get_or_create_collection = lambda **kwargs: collection
    monkeypatch.setattr("mcp.knowledge_base.chromadb.Settings", lambda **kwargs: object())
    monkeypatch.setattr("mcp.knowledge_base.chromadb.HttpClient", lambda **kwargs: client)
    kb = KnowledgeBase()
    assert len(kb.list_documents()) == 8
    for item in kb.list_documents():
        kb.delete_document(item["document_id"])
    assert KnowledgeBase().list_documents() == []


def test_stats_proves_active_collection_and_actual_document_titles():
    kb = object.__new__(KnowledgeBase)
    kb.collection_name = "supportweave_knowledge_base_v1"
    kb._collection = _CaptureCollection()
    kb.add_documents([
        {"title": "VPN 常见错误码排查", "content": "VPN 错误 691。"},
        {"title": "GitLab 与内部系统权限申请", "content": "权限申请需要审批。"},
    ])

    assert kb.stats() == {
        "collection_name": "supportweave_knowledge_base_v1",
        "corpus_id": "supportweave-it-service-v1",
        "total_chunks": 2,
        "document_titles": ["GitLab 与内部系统权限申请", "VPN 常见错误码排查"],
    }


def test_knowledge_search_returns_chroma_chunk_id():
    class _QueryCollection:
        def query(self, *, query_texts, n_results):
            assert query_texts == ["VPN 691"]
            assert n_results == 10
            return {
                "ids": [["vpn-chunk-1"]],
                "documents": [["检查账号状态"]],
                "metadatas": [[{"title": "VPN 排查", "chunk_index": 0}]],
                "distances": [[0.2]],
            }

        def get(self, *, limit, offset, include):
            return {"ids": [], "documents": [], "metadatas": []}

    kb = object.__new__(KnowledgeBase)
    kb._collection = _QueryCollection()

    assert kb.search("VPN 691") == [{
        "chunk_id": "vpn-chunk-1",
        "title": "VPN 排查",
        "content": "检查账号状态",
        "score": 0.5,
        "chunk": 0,
        "vector_score": 0.8,
        "retrieval_sources": ["vector"],
    }]


def test_hybrid_search_recovers_exact_error_code_missed_by_vector():
    class _VectorMissCollection(_CaptureCollection):
        def query(self, *, query_texts, n_results):
            return {"ids": [["z-semantic"]], "documents": [["网络连接异常"]],
                    "metadatas": [[{"title": "网络排查", "chunk_index": 0}]],
                    "distances": [[0.1]]}

    kb = object.__new__(KnowledgeBase)
    kb._collection = _VectorMissCollection()
    kb._collection.add(ids=["z-semantic", "a-exact"],
                       documents=["网络连接异常", "VPN 错误 691：检查用户名和登录域"],
                       metadatas=[{"title": "网络排查", "chunk_index": 0},
                                  {"title": "VPN 排查", "chunk_index": 0}])

    results = kb.search("VPN 691", top_k=2)

    assert [item["chunk_id"] for item in results] == ["a-exact", "z-semantic"]
    assert results[0]["retrieval_sources"] == ["keyword"]
    assert KnowledgeBase._terms("打印机脱机 VPN 691") == ["打印", "印机", "机脱", "脱机", "vpn", "691"]


def test_hybrid_search_merges_same_chunk_from_both_channels():
    kb = object.__new__(KnowledgeBase)
    kb._collection = _CaptureCollection()
    kb._collection.add(ids=["vpn-691"], documents=["VPN 691 错误：检查账号"],
                       metadatas=[{"title": "VPN 排查", "chunk_index": 0}])

    rows = kb.search("VPN 691", top_k=5)

    assert len(rows) == 1
    assert rows[0]["chunk_id"] == "vpn-691"
    assert rows[0]["retrieval_sources"] == ["vector", "keyword"]
    assert rows[0]["score"] == 1.0


def test_rewrite_search_deduplicates_by_chunk_id_even_when_scores_differ():
    manager = MCPToolManager(api_key="test-key")
    recalled = {
        "原始问题": [
            {"chunk_id": "chunk-1", "title": "VPN", "content": "检查账号", "score": 0.9},
            {"chunk_id": "chunk-2", "title": "VPN", "content": "检查账号", "score": 0.7},
            {"title": "DNS", "content": "检查网络", "score": 0.6},
        ],
        "改写问题": [
            {"chunk_id": "chunk-1", "title": "VPN", "content": "检查账号", "score": 0.5},
            {"title": "DNS", "content": "检查网络", "score": 0.4},
        ],
    }
    rerank_input = []

    async def rewrite_query(query, n):
        assert (query, n) == ("原始问题", 3)
        return list(recalled)

    async def call(tool_name, params, context, use_cache):
        assert tool_name == "knowledge_search"
        return ToolResult(success=True, data=recalled[params["query"]], tool_name=tool_name)

    async def rerank(query, items, top_k):
        rerank_input.extend(items)
        return items[:top_k]

    manager.rewrite_query = rewrite_query
    manager.call = call
    manager._rerank = rerank

    result = asyncio.run(manager.search_with_rewrite("knowledge_search", "原始问题"))

    assert result.success is True
    assert rerank_input == recalled["原始问题"]
    assert result.data == rerank_input


def test_rewrite_query_limits_and_cleans_model_output():
    manager = MCPToolManager(api_key="test-key")
    manager._client = _RewriteClient([
        "VPN 错误 691 的原因",
        " ",
        "vpn 错误 691 的原因",
        123,
        "VPN 账号认证失败怎么排查",
        "VPN 691 需要收集哪些信息",
        "这一条不应被接收",
    ])

    result = asyncio.run(manager.rewrite_query("VPN 连接报错 691", n=3))

    assert result == [
        "VPN 连接报错 691",
        "VPN 错误 691 的原因",
        "VPN 账号认证失败怎么排查",
        "VPN 691 需要收集哪些信息",
    ]


def test_rewrite_query_falls_back_to_original_on_invalid_output():
    manager = MCPToolManager(api_key="test-key")
    manager._client = _RewriteClient({"query": "不是数组"})

    assert asyncio.run(manager.rewrite_query("打印机脱机", n=3)) == ["打印机脱机"]
