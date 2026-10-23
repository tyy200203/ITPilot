import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel, _TEMPLATES


def recognizer() -> IntentRecognizer:
    return object.__new__(IntentRecognizer)


@pytest.mark.parametrize("with_history", [False, True])
def test_llm_request_includes_few_shot_examples_and_current_message(with_history):
    rec = recognizer()
    create = AsyncMock(return_value=SimpleNamespace(content=[{
        "type": "text",
        "text": '{"intent":"network_issue","confidence":0.9,"reasoning":"VPN 故障"}',
    }]))
    rec.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    rec.model = "test-model"
    message = "我的 VPN 连接仍然提示 691"
    history = [
        {"role": "user", "content": "这是不应携带的最早消息"},
        {"role": "assistant", "content": "请提供错误码"},
        {"role": "user", "content": "错误码是 691"},
        {"role": "assistant", "content": "请确认网络是否正常"},
    ] if with_history else None

    result = asyncio.run(rec._llm_recognize(message, history))

    create.assert_awaited_once()
    prompt = create.call_args.kwargs["messages"][0]["content"]
    assert "意图分类示例：" in prompt
    for category, templates in _TEMPLATES.items():
        assert f'消息: "{templates[0]}" → 意图: {category.value}' in prompt
    assert f'用户消息: "{message}"' in prompt
    assert "可选意图:" in prompt
    if history:
        assert history[0]["content"] not in prompt
        for item in history[-3:]:
            assert item["content"] in prompt
    else:
        assert "最近对话:" not in prompt
    assert result["intent"] == IntentCategory.NETWORK_ISSUE


def test_it_intent_patterns_are_recognized():
    cases = [
        ("打印机一直显示脱机", IntentCategory.DEVICE_FAILURE),
        ("VPN连接提示691", IntentCategory.NETWORK_ISSUE),
        ("申请安装Office", IntentCategory.SOFTWARE_INSTALL),
        ("帮我开通GitLab权限", IntentCategory.PERMISSION_REQUEST),
        ("GitLab权限怎么申请", IntentCategory.PERMISSION_REQUEST),
        ("新员工没有企业邮箱账号", IntentCategory.ACCOUNT_PROVISIONING),
        ("帮我创建IT工单", IntentCategory.IT_TICKET),
    ]

    rec = recognizer()
    for message, expected_intent in cases:
        result = rec._pattern_recognize(message)
        assert result["intent"] == expected_intent
        assert result["confidence"] >= 0.5


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("打印机一直显示脱机", IntentCategory.DEVICE_FAILURE),
        ("VPN连接提示691", IntentCategory.NETWORK_ISSUE),
        ("申请安装Office", IntentCategory.SOFTWARE_INSTALL),
        ("GitLab权限怎么申请", IntentCategory.PERMISSION_REQUEST),
        ("帮我开通GitLab权限", IntentCategory.PERMISSION_REQUEST),
        ("新员工没有企业邮箱账号", IntentCategory.ACCOUNT_PROVISIONING),
        ("帮我创建IT工单", IntentCategory.IT_TICKET),
    ],
)
def test_public_recognize_returns_it_intent(monkeypatch, message, expected_intent):
    """通过公开入口验证三路结果会被融合成最终 IT 意图。"""
    rec = recognizer()
    rec._cache = {}
    rec.cache_hits = 0
    rec.cache_misses = 0
    rec._embedding_enabled = True
    rec.threshold = 0.5

    async def fake_llm(_message, _history):
        return {"intent": expected_intent, "confidence": 0.9, "reasoning": "测试语义命中"}

    async def fake_embedding(_message):
        return {"intent": expected_intent, "confidence": 0.8}

    monkeypatch.setattr(rec, "_llm_recognize", fake_llm)
    monkeypatch.setattr(rec, "_embedding_recognize", fake_embedding)

    result = asyncio.run(rec.recognize(message))

    assert result.intent == expected_intent
    assert result.confidence >= 0.5
    assert result.intent_group == rec._intent_group(expected_intent)


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("你好", IntentCategory.GREETING),
        ("服务太差了", IntentCategory.COMPLAINT),
        ("请帮我转人工", IntentCategory.HUMAN_HANDOFF),
    ],
)
def test_existing_intents_still_work(message, expected_intent):
    """IT 改造不能破坏原项目已有的基础意图。"""
    result = recognizer()._pattern_recognize(message)

    assert result["intent"] == expected_intent


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("申请安装Office", IntentCategory.SOFTWARE_INSTALL),
        ("新员工没有企业邮箱账号", IntentCategory.ACCOUNT_PROVISIONING),
        ("VPN报错691，需要帮忙处理", IntentCategory.NETWORK_ISSUE),
        ("帮我创建IT工单", IntentCategory.IT_TICKET),
    ],
)
def test_specific_it_intent_wins_over_generic_words(message, expected_intent):
    """“申请、账号、报错、帮我”等通用词不能覆盖更具体的 IT 意图。"""
    result = recognizer()._pattern_recognize(message)

    assert result["intent"] == expected_intent


def test_llm_failure_uses_it_pattern_instead_of_wrong_embedding():
    """LLM 不可用时，明确的 IT 关键词应优先于错误的向量候选。"""
    rec = recognizer()
    pattern = rec._pattern_recognize("VPN连接提示691")

    intent, confidence, source_scores = rec._vote(
        {"intent": IntentCategory.OTHER, "confidence": 0.0, "failed": True},
        {"intent": IntentCategory.GREETING, "confidence": 0.95},
        pattern,
    )

    assert intent == IntentCategory.NETWORK_ISSUE
    assert confidence == pattern["confidence"]
    assert source_scores["embedding"] == 0.95


def test_it_intents_map_to_existing_agent_groups():
    assert IntentRecognizer._intent_group(IntentCategory.DEVICE_FAILURE) == IntentCategory.TECHNICAL.value
    assert IntentRecognizer._intent_group(IntentCategory.NETWORK_ISSUE) == IntentCategory.TECHNICAL.value
    assert IntentRecognizer._intent_group(IntentCategory.SOFTWARE_INSTALL) == IntentCategory.TECHNICAL.value
    assert IntentRecognizer._intent_group(IntentCategory.PERMISSION_REQUEST) == IntentCategory.ACCOUNT.value
    assert IntentRecognizer._intent_group(IntentCategory.ACCOUNT_PROVISIONING) == IntentCategory.ACCOUNT.value
    assert IntentRecognizer._intent_group(IntentCategory.IT_TICKET) == IntentCategory.ESCALATION.value


def test_it_entities_are_extracted():
    rec = recognizer()
    message = (
        "工号 E10086，资产编号 PC-2025-019，Windows 11 笔记本的 "
        "VPN 客户端提示错误码 691"
    )

    entities = rec._extract_entities(message)

    assert entities["employee_id"] == ["E10086"]
    assert entities["asset_id"] == ["PC-2025-019"]
    assert "Windows 11" in entities["os_version"]
    assert "笔记本" in entities["device_type"]
    assert "VPN 客户端" in entities["software_name"]
    assert "691" in entities["error_code"]


def test_commerce_intents_and_entities_are_not_part_of_itpilot():
    obsolete_intents = {"billing", "order_status", "logistics", "refund", "invoice", "payment_issue"}
    assert obsolete_intents.isdisjoint(intent.value for intent in IntentCategory)

    entities = recognizer()._extract_entities("订单号 A12345，退款金额 99 元")
    assert "order_id" not in entities
    assert "product" not in entities
    assert "amount" not in entities


def test_it_urgency_keywords_are_recognized():
    rec = recognizer()

    assert rec._urgency("公司全员 VPN 大面积断网，业务中断", IntentCategory.NETWORK_ISSUE) == UrgencyLevel.CRITICAL
    assert rec._urgency("账号登录失败，导致我无法办公", IntentCategory.ACCOUNT_PROVISIONING) == UrgencyLevel.HIGH
