import asyncio

from agents.agent_orchestrator import (
    AgentProfile,
    AgentResponse,
    AgentType,
    AgentOrchestrator,
    EscalationAgent,
    GeneralAgent,
    PermissionAgent,
    Request,
    ResponseComposer,
    RoutingDecision,
    TechnicalAgent,
    _preferred_env_name,
    build_shared_rag_tools,
)
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel


def test_itpilot_environment_name_takes_precedence_over_legacy(monkeypatch):
    monkeypatch.setenv("ECHOMIND_COMPOSER_MAX_TOKENS", "900")
    monkeypatch.setenv("ITPILOT_COMPOSER_MAX_TOKENS", "1200")

    assert _preferred_env_name(
        "ITPILOT_COMPOSER_MAX_TOKENS", "ECHOMIND_COMPOSER_MAX_TOKENS"
    ) == "ITPILOT_COMPOSER_MAX_TOKENS"

    monkeypatch.delenv("ITPILOT_COMPOSER_MAX_TOKENS")
    assert _preferred_env_name(
        "ITPILOT_COMPOSER_MAX_TOKENS", "ECHOMIND_COMPOSER_MAX_TOKENS"
    ) == "ECHOMIND_COMPOSER_MAX_TOKENS"


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


def make_request(**kwargs):
    values = {
        "message": "VPN 客户端报错 691，同时还没有内网访问权限",
        "user_id": "u1",
        "conv_id": "c1",
        "intent": IntentCategory.NETWORK_ISSUE,
        "intent_group": "technical",
        "urgency": UrgencyLevel.HIGH,
        "intent_confidence": 0.92,
        "entities": {"error_code": ["691"], "employee_id": ["E10086"]},
    }
    values.update(kwargs)
    return Request(**values)


def test_agent_profiles_have_distinct_contracts_and_generation_config():
    assert isinstance(GeneralAgent.profile, AgentProfile)
    assert GeneralAgent.profile.role != TechnicalAgent.profile.role
    assert TechnicalAgent.profile.workflow != PermissionAgent.profile.workflow
    assert TechnicalAgent.profile.temperature < GeneralAgent.profile.temperature
    assert "search_knowledge_base" in GeneralAgent.profile.tool_scope
    assert "lookup_error_code" in TechnicalAgent.profile.tool_scope
    assert "check_permission_fields" in PermissionAgent.profile.tool_scope
    assert "build_permission_request" in PermissionAgent.profile.tool_scope


def test_agent_type_model_contains_exactly_four_it_roles():
    assert [agent_type.value for agent_type in AgentType] == [
        "general",
        "technical",
        "permission",
        "escalation",
    ]


def test_service_overview_does_not_trigger_escalation_from_boundary_text():
    agent = GeneralAgent(FakeClient(), "test-model")

    assert agent._needs_escalation("涉及实际授权时由 IT 人员继续处理。") is False
    assert agent._needs_escalation("这个问题无法处理，建议转人工。") is True


def test_llm_failure_prefers_query_pattern_over_ngram_similarity():
    recognizer = IntentRecognizer(api_key="test-key")
    pattern = recognizer._pattern_recognize("你们支持哪些服务？")
    intent, _, _ = recognizer._vote(
        {"intent": IntentCategory.OTHER, "confidence": 0.0, "failed": True},
        {"intent": IntentCategory.ESCALATION, "confidence": 0.9},
        pattern,
    )

    assert intent == IntentCategory.QUERY


def test_domain_agents_build_different_role_packets():
    req = make_request()
    general_packet = GeneralAgent(FakeClient(), "test-model")._build_role_packet(req)
    technical_packet = TechnicalAgent(FakeClient(), "test-model")._build_role_packet(req)
    permission_packet = PermissionAgent(FakeClient(), "test-model")._build_role_packet(req)

    assert "triage_targets" in general_packet
    assert "diagnostic_fields" in technical_packet
    assert "permission_request_fields" in permission_packet
    assert general_packet != technical_packet != permission_packet


def test_escalation_agent_is_a_real_non_llm_handoff_node():
    client = FakeClient()
    agent = EscalationAgent(client, "test-model")

    result = asyncio.run(agent.handle(make_request(
        intent=IntentCategory.HUMAN_HANDOFF,
        urgency=UrgencyLevel.CRITICAL,
    )))

    assert result.success is True
    assert result.escalate is True
    assert "IT 工单升级" in result.content
    assert "尚未向真实 ITSM 系统提交" in result.content
    assert result.tools_used == ["build_it_ticket"]
    assert result.tool_traces[0]["tool_name"] == "build_it_ticket"
    assert result.tool_traces[0]["input"]["impact_scope"] == "未提供"
    assert client.calls == []


def test_composer_fallback_preserves_primary_and_supporting_results():
    composer = ResponseComposer(FakeClient(error=RuntimeError("provider down")), "test-model")
    req = make_request()
    responses = [
        AgentResponse(AgentType.TECHNICAL, "先检查 VPN 客户端配置和错误码。", True),
        AgentResponse(AgentType.PERMISSION, "请提供工号和所需访问的内网系统。", True),
    ]

    content = asyncio.run(composer.compose(req, responses))

    assert content.startswith("先检查 VPN 客户端配置和错误码。")
    assert "补充说明" in content
    assert "工号" in content


def test_routing_decision_can_target_escalation_pool():
    # Keep this assertion close to the public data contract used by the API.
    decision = RoutingDecision(
        primary_agent=AgentType.ESCALATION,
        reason="critical request",
        confidence=1.0,
    )
    assert decision.agent_types == [AgentType.ESCALATION]
    assert not decision.multi_agent


def test_composite_it_request_routes_permission_as_supporting_agent():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.GENERAL: [object()],
        AgentType.TECHNICAL: [object()],
        AgentType.PERMISSION: [object()],
    }

    decision = orchestrator._route_decision(make_request())

    assert decision.primary_agent is AgentType.TECHNICAL
    assert decision.supporting_agents == [AgentType.PERMISSION]
    assert decision.multi_agent is True


def test_composite_device_and_permission_wording_keeps_both_agents():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.GENERAL: [object()],
        AgentType.TECHNICAL: [object()],
        AgentType.PERMISSION: [object()],
    }
    request = make_request(
        message="电脑蓝屏，同时需要 GitLab 权限",
        intent=IntentCategory.DEVICE_FAILURE,
        entities={"device_type": ["电脑"]},
    )

    decision = orchestrator._route_decision(request)

    assert decision.primary_agent is AgentType.TECHNICAL
    assert decision.supporting_agents == [AgentType.PERMISSION]


def test_it_intents_route_to_the_four_agent_model():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.GENERAL: [object()],
        AgentType.TECHNICAL: [object()],
        AgentType.PERMISSION: [object()],
        AgentType.ESCALATION: [object()],
    }
    cases = [
        (IntentCategory.DEVICE_FAILURE, "打印机一直显示脱机", AgentType.TECHNICAL),
        (IntentCategory.NETWORK_ISSUE, "VPN连接提示691", AgentType.TECHNICAL),
        (IntentCategory.SOFTWARE_INSTALL, "申请安装Office", AgentType.TECHNICAL),
        (IntentCategory.PERMISSION_REQUEST, "帮我开通GitLab权限", AgentType.PERMISSION),
        (IntentCategory.ACCOUNT_PROVISIONING, "新员工没有企业邮箱账号", AgentType.PERMISSION),
        (IntentCategory.IT_TICKET, "帮我创建IT工单", AgentType.ESCALATION),
    ]

    for intent, message, expected_agent in cases:
        decision = orchestrator._route_decision(make_request(
            message=message,
            intent=intent,
            intent_group=IntentRecognizer._intent_group(intent),
            entities={},
        ))
        assert decision.primary_agent is expected_agent


def test_agent_tool_scopes_are_real_and_isolated():
    general_tools = set(GeneralAgent(FakeClient(), "test-model").get_tools())
    technical_tools = set(TechnicalAgent(FakeClient(), "test-model").get_tools())
    permission_tools = set(PermissionAgent(FakeClient(), "test-model").get_tools())
    escalation_tools = set(EscalationAgent(FakeClient(), "test-model").get_tools())

    assert general_tools == {"inspect_request_context", "suggest_required_fields"}
    assert technical_tools == {"lookup_error_code", "build_diagnostic_plan", "check_device_context"}
    assert permission_tools == {"check_permission_fields", "build_permission_request"}
    assert escalation_tools == {"build_it_ticket"}
    assert not general_tools & technical_tools
    assert not technical_tools & permission_tools
    assert "check_permission_fields" not in technical_tools
    assert "lookup_error_code" not in permission_tools
    assert "build_it_ticket" not in general_tools | technical_tools | permission_tools


def test_shared_rag_tool_is_available_to_all_agents():
    class RagManager:
        async def search_with_rewrite(self, tool_name, query, top_k=5):
            return type(
                "Result",
                (),
                {"success": True, "data": [{"title": "VPN 使用指南", "content": "先确认客户端版本"}], "reranked": True},
            )()

    shared = build_shared_rag_tools(RagManager())

    general = GeneralAgent(FakeClient(), "test-model")
    technical = TechnicalAgent(FakeClient(), "test-model")
    permission = PermissionAgent(FakeClient(), "test-model")
    escalation = EscalationAgent(FakeClient(), "test-model")

    for agent in (general, technical, permission, escalation):
        agent.set_shared_tools(shared)
        tools = agent.get_tools()
        assert "search_knowledge_base" in tools


def test_tool_input_validation_rejects_unknown_fields():
    agent = TechnicalAgent(FakeClient(), "test-model")
    spec = agent.get_tools()["lookup_error_code"]

    try:
        agent._validate_tool_input(spec, {"error_code": "401", "secret": "nope"})
    except ValueError as exc:
        assert "不允许的工具参数" in str(exc)
    else:
        raise AssertionError("unknown tool fields should be rejected")


def test_tool_use_round_trip_executes_only_whitelisted_tool():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_1"
        name = "lookup_error_code"
        input = {"error_code": "401"}

    class TextBlock:
        type = "text"
        text = "已根据 401 错误码给出排查建议。"

    class ToolClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    client = ToolClient()
    agent = TechnicalAgent(client, "test-model")
    response = asyncio.run(agent.handle(make_request()))

    assert response.success is True
    assert response.tools_used == ["lookup_error_code"]
    assert response.tool_traces[0]["tool_name"] == "lookup_error_code"
    assert response.tool_traces[0]["input"] == {"error_code": "401"}
    assert len(client.calls) == 2
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "lookup_error_code",
        "build_diagnostic_plan",
        "check_device_context",
    }
    assert "tool_result" in str(client.calls[1]["messages"])
