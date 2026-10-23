import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

from core.intent_recognizer import IntentCategory
from evaluation.evaluator import (
    DEFAULT_DIALOG_CASES,
    DEFAULT_INTENT_CASES,
    EVAL_SUITE_ID,
    EndToEndEvaluator,
    EvalReport,
    IntentEvaluator,
    IntentTestCase,
    LLMJudge,
    QualityScores,
)


class _IntentResult:
    def __init__(self, intent):
        self.intent = intent
        self.confidence = 1.0
        self.reasoning = "test"


class _Recognizer:
    async def recognize(self, message):
        if "VPN" in message:
            return _IntentResult(IntentCategory.NETWORK_ISSUE)
        return _IntentResult(IntentCategory.DEVICE_FAILURE)


class _JudgeResponse:
    def __init__(self, text):
        self.content = [{"type": "text", "text": text}]


class _JudgeClient:
    def __init__(self, text):
        self.text = text
        self.messages = self

    async def create(self, **kwargs):
        return _JudgeResponse(self.text)


def _judge_scores(payload):
    judge = LLMJudge(_JudgeClient(json.dumps(payload, ensure_ascii=False)), "test-model")
    return asyncio.run(judge.judge("测试问题", "测试回答"))


def _all_dialog_text():
    values = []
    for case in DEFAULT_DIALOG_CASES:
        if case.get("question"):
            values.append(case["question"])
        values.extend(case.get("turns") or [])
    return "\n".join(values)


def test_default_intent_cases_cover_itpilot_roles_and_have_no_commerce_cases():
    expected = {
        "device_failure",
        "network_issue",
        "software_install",
        "permission_request",
        "account_provisioning",
        "it_ticket",
        "human_handoff",
        "greeting",
        "feedback",
        "complaint",
    }
    labels = {case.expected_intent for case in DEFAULT_INTENT_CASES}
    text = "\n".join(case.message for case in DEFAULT_INTENT_CASES)

    assert EVAL_SUITE_ID == "itpilot-it-service-v1"
    assert expected <= labels
    assert labels <= {intent.value for intent in IntentCategory}
    for obsolete in ("退款", "订单", "物流", "配送", "发票", "扣款", "会员"):
        assert obsolete not in text


def test_default_dialog_cases_cover_single_and_multi_turn_it_scenarios():
    assert any(case.get("question") for case in DEFAULT_DIALOG_CASES)
    assert any(len(case.get("turns") or []) > 1 for case in DEFAULT_DIALOG_CASES)
    text = _all_dialog_text()

    for expected in ("打印机", "Office", "GitLab", "全公司", "VPN", "新员工"):
        assert expected in text
    for obsolete in ("退款", "订单", "物流", "配送", "发票", "扣款", "会员"):
        assert obsolete not in text


def test_judge_prompt_enforces_it_safety_and_execution_boundaries():
    prompt = LLMJudge.JUDGE_PROMPT

    for expected in (
        "企业 IT", "虚构", "密码", "短信验证码", "权限已经开通",
        "工单已经提交", "严重故障", "safety_violation", "violations",
    ):
        assert expected in prompt


def test_intent_metrics_still_calculate_accuracy_macro_f1_and_per_class():
    evaluator = IntentEvaluator(_Recognizer())
    cases = [
        IntentTestCase("VPN 报错 691", "network_issue"),
        IntentTestCase("打印机脱机", "device_failure"),
        IntentTestCase("打印机卡纸", "network_issue"),
    ]

    result = asyncio.run(evaluator.evaluate(cases))

    assert result["accuracy"] == 0.6667
    assert "macro_f1" in result
    assert set(result["per_class"]) == {"device_failure", "network_issue"}
    assert set(result["per_class"]["network_issue"]) == {"precision", "recall", "f1"}


def test_judge_invalid_json_uses_safe_fallback():
    judge = LLMJudge(_JudgeClient("not-json"), "test-model")

    scores = asyncio.run(judge.judge("VPN 报错 691", "请检查账号状态"))

    assert scores.judge_failed is True
    assert scores.overall == 0.5
    assert scores.error
    assert scores.safety_violation is False
    assert scores.violations == []


def test_judge_safety_violation_forces_accuracy_and_helpfulness_caps():
    scores = _judge_scores({
        "relevance": 0.9,
        "accuracy": 0.95,
        "completeness": 0.8,
        "helpfulness": 0.90,
        "safety_violation": True,
        "violations": ["虚构权限已经开通"],
    })

    assert scores.accuracy == 0.4
    assert scores.helpfulness == 0.4
    assert scores.safety_violation is True
    assert scores.violations == ["虚构权限已经开通"]


def test_judge_without_safety_violation_preserves_valid_scores():
    scores = _judge_scores({
        "relevance": 0.91,
        "accuracy": 0.82,
        "completeness": 0.73,
        "helpfulness": 0.84,
        "safety_violation": False,
        "violations": [],
    })

    assert (scores.relevance, scores.accuracy, scores.completeness, scores.helpfulness) == (
        0.91, 0.82, 0.73, 0.84,
    )
    assert scores.safety_violation is False
    assert scores.violations == []


def test_judge_new_safety_fields_are_backward_compatible_when_omitted():
    scores = _judge_scores({
        "relevance": 0.8,
        "accuracy": 0.7,
        "completeness": 0.6,
        "helpfulness": 0.5,
    })

    assert scores.safety_violation is False
    assert scores.violations == []
    assert scores.judge_failed is False


def test_judge_scores_are_clamped_to_zero_one_range():
    scores = _judge_scores({
        "relevance": -0.2,
        "accuracy": 1.2,
        "completeness": 2.0,
        "helpfulness": -1,
        "safety_violation": False,
    })

    assert scores.relevance == 0.0
    assert scores.accuracy == 1.0
    assert scores.completeness == 1.0
    assert scores.helpfulness == 0.0


def test_safety_details_are_written_to_dialog_result_metadata():
    class _Orchestrator:
        async def run(self, request):
            return SimpleNamespace(
                response="权限已经开通。",
                agent_type=SimpleNamespace(value="permission"),
                intent=SimpleNamespace(value="permission_request"),
            )

    class _FixedJudge:
        async def judge(self, question, response, context=None):
            return QualityScores(
                relevance=0.9,
                accuracy=0.4,
                completeness=0.8,
                helpfulness=0.4,
                safety_violation=True,
                violations=["虚构权限已经开通"],
            )

    evaluator = object.__new__(EndToEndEvaluator)
    evaluator._orchestrator = _Orchestrator()
    evaluator._judge = _FixedJudge()

    results = asyncio.run(evaluator._evaluate_dialog_case(
        {"question": "请帮我开通 GitLab 权限"},
        case_idx=0,
    ))

    assert results[0].metadata["safety_violation"] is True
    assert results[0].metadata["violations"] == ["虚构权限已经开通"]


def test_itpilot_baseline_is_saved_separately_from_legacy_baseline(tmp_path):
    legacy_path = tmp_path / "baseline.json"
    itpilot_path = tmp_path / "itpilot-baseline-v1.json"
    legacy_content = '{"legacy": true}'
    legacy_path.write_text(legacy_content, encoding="utf-8")

    evaluator = object.__new__(EndToEndEvaluator)
    evaluator._baseline_path = itpilot_path
    evaluator._baseline = None
    report = EvalReport(
        timestamp="2026-09-22T00:00:00",
        total=0,
        passed=0,
        pass_rate=0.0,
        avg_scores={},
        regressions=[],
        recommendations=[],
        results=[],
    )
    evaluator._save_baseline(report)

    assert legacy_path.read_text(encoding="utf-8") == legacy_content
    assert json.loads(itpilot_path.read_text(encoding="utf-8")) == asdict(report)


def test_regression_detection_behavior_is_unchanged():
    evaluator = object.__new__(EndToEndEvaluator)
    evaluator._history = []
    evaluator._baseline = EvalReport(
        timestamp="baseline",
        total=1,
        passed=1,
        pass_rate=1.0,
        avg_scores={"intent_accuracy": 1.0, "helpfulness": 0.9},
        regressions=[],
        recommendations=[],
        results=[],
    )

    regressions = evaluator._detect_regressions({"intent_accuracy": 0.8, "helpfulness": 0.89})

    assert any("intent_accuracy" in item for item in regressions)
    assert all("helpfulness" not in item for item in regressions)
