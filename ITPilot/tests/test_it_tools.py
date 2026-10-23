import pytest

from agents.agent_orchestrator import PermissionAgent, Request
from agents.tools import escalation_tools, general_tools, permission_tools, technical_tools
from core.intent_recognizer import IntentCategory, UrgencyLevel


class FakeClient:
    class Messages:
        async def create(self, **kwargs):
            raise AssertionError("工具单元测试不应调用 LLM")

    messages = Messages()


def make_request(**kwargs) -> Request:
    values = {
        "message": "工号 E10086 的 Windows 11 笔记本连接 VPN 提示错误码 691",
        "user_id": "employee-1",
        "conv_id": "it-tools-1",
        "intent": IntentCategory.NETWORK_ISSUE,
        "intent_group": "technical",
        "urgency": UrgencyLevel.HIGH,
        "entities": {
            "employee_id": ["E10086"],
            "asset_id": ["PC-2025-019"],
            "device_type": ["笔记本"],
            "os_version": ["Windows 11"],
            "software_name": ["VPN 客户端"],
            "error_code": ["691"],
        },
    }
    values.update(kwargs)
    return Request(**values)


@pytest.mark.parametrize(
    ("intent", "expected_field"),
    [
        (IntentCategory.DEVICE_FAILURE, "资产编号"),
        (IntentCategory.NETWORK_ISSUE, "影响范围"),
        (IntentCategory.SOFTWARE_INSTALL, "软件名称"),
        (IntentCategory.PERMISSION_REQUEST, "业务理由"),
        (IntentCategory.ACCOUNT_PROVISIONING, "员工信息"),
        (IntentCategory.IT_TICKET, "联系方式"),
    ],
)
def test_general_required_fields_are_it_specific(intent, expected_field):
    spec = general_tools()["suggest_required_fields"]
    result = spec.handler(make_request(intent=intent), {})

    assert expected_field in result["required_fields"]


@pytest.mark.parametrize(
    ("code", "expected_meaning"),
    [
        ("691", "VPN 身份验证失败"),
        ("651", "网络连接或拨号适配器异常"),
        ("720", "VPN 协议或 TCP/IP 配置异常"),
        ("0x80070005", "Windows 拒绝访问"),
    ],
)
def test_lookup_error_code_supports_common_it_codes(code, expected_meaning):
    spec = technical_tools()["lookup_error_code"]
    result = spec.handler(make_request(), {"error_code": code})

    assert result["meaning"] == expected_meaning
    assert result["server_log_checked"] is False


def test_device_context_uses_only_user_provided_entities():
    spec = technical_tools()["check_device_context"]
    result = spec.handler(make_request(), {})

    assert result["ready_for_diagnosis"] is True
    assert result["asset_system_checked"] is False
    assert result["missing_fields"] == []


def test_diagnostic_plan_contains_it_environment_and_entities():
    spec = technical_tools()["build_diagnostic_plan"]
    result = spec.handler(make_request(), {
        "environment": "Windows 11 / VPN 客户端",
        "reproduced": True,
    })

    assert result["asset_ids"] == ["PC-2025-019"]
    assert result["os_versions"] == ["Windows 11"]
    assert result["software_names"] == ["VPN 客户端"]
    assert result["error_codes"] == ["691"]
    assert "用最小请求复现并记录 request_id" in result["diagnostic_steps"]


def test_permission_field_check_reports_only_missing_information():
    req = make_request(
        message="工号 E10086 需要 GitLab 权限",
        intent=IntentCategory.PERMISSION_REQUEST,
        intent_group="account",
        entities={"employee_id": ["E10086"], "software_name": ["GitLab"]},
    )
    spec = permission_tools()["check_permission_fields"]
    result = spec.handler(req, {
        "permission_type": "项目开发权限",
        "business_reason": "参与 ITPilot 项目开发",
    })

    assert result["target_system"] == "GitLab"
    assert result["missing_fields"] == ["duration"]
    assert result["directory_checked"] is False
    assert result["approval_required"] is True


def test_permission_request_is_a_draft_and_never_claims_grant():
    req = make_request(
        intent=IntentCategory.PERMISSION_REQUEST,
        entities={"employee_id": ["E10086"], "software_name": ["GitLab"]},
    )
    spec = permission_tools()["build_permission_request"]
    result = spec.handler(req, {
        "target_system": "GitLab",
        "permission_type": "项目开发权限",
        "business_reason": "参与 ITPilot 项目开发",
        "duration": "项目周期内",
    })

    assert result["status"] == "draft"
    assert result["missing_fields"] == []
    assert result["actually_submitted"] is False
    assert result["actually_granted"] is False


def test_it_ticket_contains_context_but_is_not_created():
    req = make_request(intent=IntentCategory.IT_TICKET, intent_group="escalation")
    spec = escalation_tools()["build_it_ticket"]
    result = spec.handler(req, {"reason": "VPN 故障需要二线处理", "impact_scope": "个人"})

    assert result["ticket_status"] == "draft"
    assert result["actually_created"] is False
    assert result["employee_ids"] == ["E10086"]
    assert result["asset_ids"] == ["PC-2025-019"]
    assert result["error_codes"] == ["691"]
    assert result["sensitive_data_required"] is False


def test_permission_tool_schema_rejects_unknown_fields():
    agent = PermissionAgent(FakeClient(), "test-model")
    spec = agent.get_tools()["build_permission_request"]

    with pytest.raises(ValueError, match="不允许的工具参数"):
        agent._validate_tool_input(spec, {
            "target_system": "GitLab",
            "password": "must-not-be-accepted",
        })


def test_active_factories_do_not_expose_legacy_billing_tools():
    active_names = set().union(
        general_tools(),
        technical_tools(),
        permission_tools(),
        escalation_tools(),
    )

    assert "check_billing_fields" not in active_names
    assert "compare_amounts" not in active_names
