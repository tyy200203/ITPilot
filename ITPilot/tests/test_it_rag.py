import asyncio
import json

from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager


class _CaptureCollection:
    def __init__(self):
        self.ids = []
        self.documents = []
        self.metadatas = []

    def add(self, *, ids, documents, metadatas):
        self.ids.extend(ids)
        self.documents.extend(documents)
        self.metadatas.extend(metadatas)

    def count(self):
        return len(self.documents)

    def get(self, *, limit, include):
        return {"metadatas": self.metadatas[:limit]}


class _ExistingCollection:
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


def test_itpilot_uses_versioned_collection_without_reusing_legacy_name():
    assert KnowledgeBase.DEFAULT_COLLECTION_NAME == "itpilot_knowledge_base_v1"
    assert KnowledgeBase.DEFAULT_COLLECTION_NAME != "knowledge_base"


def test_collection_name_is_configurable_and_legacy_collection_is_not_deleted(monkeypatch):
    client = _CaptureChromaClient()
    monkeypatch.setenv("KNOWLEDGE_COLLECTION_NAME", "itpilot_knowledge_base_test")
    monkeypatch.setattr("mcp.knowledge_base.chromadb.Settings", lambda **kwargs: object())
    monkeypatch.setattr("mcp.knowledge_base.chromadb.HttpClient", lambda **kwargs: client)

    kb = KnowledgeBase()

    assert kb.collection_name == "itpilot_knowledge_base_test"
    assert client.requested_name == "itpilot_knowledge_base_test"
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
    assert kb._collection.metadatas == [
        {"title": "打印机排查", "chunk_index": index, "total_chunks": added}
        for index in range(added)
    ]


def test_stats_proves_active_collection_and_actual_document_titles():
    kb = object.__new__(KnowledgeBase)
    kb.collection_name = "itpilot_knowledge_base_v1"
    kb._collection = _CaptureCollection()
    kb.add_documents([
        {"title": "VPN 常见错误码排查", "content": "VPN 错误 691。"},
        {"title": "GitLab 与内部系统权限申请", "content": "权限申请需要审批。"},
    ])

    assert kb.stats() == {
        "collection_name": "itpilot_knowledge_base_v1",
        "corpus_id": "itpilot-it-service-v1",
        "total_chunks": 2,
        "document_titles": ["GitLab 与内部系统权限申请", "VPN 常见错误码排查"],
    }


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
