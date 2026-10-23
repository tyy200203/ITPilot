import asyncio
from datetime import datetime

from memory.conversation_memory import MemoryManager, Message, MsgRole


class _FakeRedis:
    def __init__(self):
        self.deleted = []

    async def delete(self, key):
        self.deleted.append(key)


def test_short_conversation_is_archived_before_working_memory_is_cleared():
    memory = object.__new__(MemoryManager)
    memory._redis = _FakeRedis()
    messages = [
        Message(MsgRole.USER, "VPN 报错 691", datetime.now()),
        Message(MsgRole.ASSISTANT, "请确认账号状态", datetime.now()),
    ]
    calls = {"profile": 0, "episodic": []}

    async def get_working_memory(user_id, conv_id):
        return messages

    async def update_profile(user_id, conv_id):
        calls["profile"] += 1

    async def summarize(items):
        return "user: VPN 报错 691", "用户反馈 VPN 错误 691"

    async def store(user_id, conv_id, text, summary):
        calls["episodic"].append((user_id, conv_id, text, summary))

    memory._get_working_memory = get_working_memory
    memory.update_profile = update_profile
    memory._summarize_messages = summarize
    memory._store_episodic = store

    result = asyncio.run(memory.archive_conversation("u1", "c1"))

    assert result == {"archived": True, "message_count": 2, "conv_id": "c1"}
    assert calls["profile"] == 1
    assert calls["episodic"] == [("u1", "c1", "user: VPN 报错 691", "用户反馈 VPN 错误 691")]
    assert memory._redis.deleted == ["wm:u1:c1", "summary:u1:c1"]


def test_empty_conversation_archive_is_idempotent():
    memory = object.__new__(MemoryManager)
    memory._redis = _FakeRedis()

    async def get_working_memory(user_id, conv_id):
        return []

    memory._get_working_memory = get_working_memory

    result = asyncio.run(memory.archive_conversation("u1", "c1"))

    assert result == {"archived": False, "message_count": 0, "conv_id": "c1"}
    assert memory._redis.deleted == []
