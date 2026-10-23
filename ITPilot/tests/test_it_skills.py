from pathlib import Path

import pytest

from agents.agent_orchestrator import (
    EscalationAgent,
    GeneralAgent,
    PermissionAgent,
    Request,
    TechnicalAgent,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel
from core.skill_loader import SkillManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"


def loaded_manager() -> SkillManager:
    manager = SkillManager(str(SKILLS_DIR))
    manager.load()
    return manager


class FakeClient:
    pass


def test_exactly_four_it_skills_load_without_errors():
    manager = loaded_manager()

    assert manager.errors == []
    assert {skill.name for skill in manager.skills} == {
        "IT服务台分诊规范",
        "IT技术支持规范",
        "账号权限申请规范",
        "IT工单升级规范",
    }
    assert {skill.agents[0] for skill in manager.skills} == {
        "general",
        "technical",
        "permission",
        "escalation",
    }


@pytest.mark.parametrize(
    ("message", "agent_type", "expected_name"),
    [
        ("你好，我需要IT支持", "general", "IT服务台分诊规范"),
        ("打印机一直显示脱机", "technical", "IT技术支持规范"),
        ("帮我申请GitLab权限", "permission", "账号权限申请规范"),
        ("帮我创建IT工单", "escalation", "IT工单升级规范"),
    ],
)
def test_each_agent_receives_its_matching_skill(message, agent_type, expected_name):
    prompt = loaded_manager().prompt_for(message, agent_type)

    assert "以下是当前请求可用的 ITPilot Skills" in prompt
    assert f"### {expected_name}" in prompt


def test_skill_agent_boundaries_prevent_cross_role_injection():
    manager = loaded_manager()

    assert "账号权限申请规范" in manager.prompt_for("帮我申请GitLab权限", "permission")
    assert "账号权限申请规范" not in manager.prompt_for("帮我申请GitLab权限", "technical")
    assert "IT技术支持规范" in manager.prompt_for("打印机一直显示脱机", "technical")
    assert "IT技术支持规范" not in manager.prompt_for("打印机一直显示脱机", "permission")


@pytest.mark.parametrize(
    ("agent_class", "message", "intent", "expected_name"),
    [
        (GeneralAgent, "你好，我需要IT支持", IntentCategory.QUERY, "IT服务台分诊规范"),
        (TechnicalAgent, "打印机一直显示脱机", IntentCategory.DEVICE_FAILURE, "IT技术支持规范"),
        (PermissionAgent, "帮我申请GitLab权限", IntentCategory.PERMISSION_REQUEST, "账号权限申请规范"),
        (EscalationAgent, "帮我创建IT工单", IntentCategory.IT_TICKET, "IT工单升级规范"),
    ],
)
def test_matching_skill_is_available_in_agent_system_prompt(agent_class, message, intent, expected_name):
    manager = loaded_manager()
    agent = agent_class(FakeClient(), "test-model", skill_manager=manager)
    request = Request(
        message=message,
        user_id="employee-1",
        conv_id="skill-test",
        intent=intent,
        intent_group="test",
        urgency=UrgencyLevel.LOW,
    )

    prompt = agent._build_system_prompt(request)

    assert "[动态 Skills]" in prompt
    assert expected_name in prompt


def test_active_skills_do_not_contain_legacy_ecommerce_rules():
    manager = loaded_manager()
    combined = "\n".join(
        f"{skill.name}\n{skill.description}\n{skill.content}"
        for skill in manager.skills
    )

    for forbidden in ("退款", "账单", "订单", "发票", "扣款", "会员"):
        assert forbidden not in combined


def test_disabled_skill_is_not_injected(tmp_path):
    skill_dir = tmp_path / "disabled_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: 禁用测试规范
keywords: 打印机
agents: technical
enabled: false
---

# 禁用测试规范

这段内容不应被注入。
""",
        encoding="utf-8",
    )
    manager = SkillManager(str(tmp_path))
    manager.load()

    assert manager.skills[0].enabled is False
    assert manager.prompt_for("打印机故障", "technical") == ""


def test_reload_replaces_previous_skill_content(tmp_path):
    skill_dir = tmp_path / "reload_skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        """---
name: 热加载测试规范
keywords: VPN
agents: technical
enabled: true
---

# 热加载测试规范

第一版规则。
""",
        encoding="utf-8",
    )
    manager = SkillManager(str(tmp_path))
    manager.load()
    assert "第一版规则" in manager.prompt_for("VPN故障", "technical")

    skill_path.write_text(
        """---
name: 热加载测试规范
keywords: VPN
agents: technical
enabled: true
---

# 热加载测试规范

第二版规则。
""",
        encoding="utf-8",
    )
    manager.reload()

    prompt = manager.prompt_for("VPN故障", "technical")
    assert "第二版规则" in prompt
    assert "第一版规则" not in prompt
