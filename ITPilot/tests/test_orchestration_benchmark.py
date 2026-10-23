from pathlib import Path

from agents.agent_orchestrator import AgentType
from evaluation.orchestration_benchmark import (
    DEFAULT_DATASET,
    _binary_metrics,
    _classification_metrics,
    _set_metrics,
    evaluate_orchestration_cases,
    load_dataset,
)


def test_orchestration_dataset_is_balanced_complete_and_unique():
    dataset = load_dataset(DEFAULT_DATASET)
    cases = dataset["cases"]
    scenarios = [case["scenario"] for case in cases]
    case_ids = [case["case_id"] for case in cases]
    messages = [case["message"] for case in cases]

    assert dataset["suite_id"] == "itpilot-orchestration-benchmark-v1"
    assert len(cases) == 100
    assert set(scenarios) == {"general", "technical", "permission", "escalation", "collaboration"}
    assert all(scenarios.count(scenario) == 20 for scenario in set(scenarios))
    assert len(case_ids) == len(set(case_ids))
    assert len(messages) == len(set(messages))
    assert {case["expected_primary"] for case in cases} == {agent.value for agent in AgentType}


def test_orchestration_dataset_has_twenty_true_multi_agent_cases():
    cases = load_dataset(DEFAULT_DATASET)["cases"]
    multi_agent_cases = [case for case in cases if case["expected_supporting"]]

    assert len(multi_agent_cases) == 20
    assert all(case["scenario"] == "collaboration" for case in multi_agent_cases)
    assert all(len(case["expected_supporting"]) == 1 for case in multi_agent_cases)


def test_orchestration_benchmark_returns_all_required_metrics():
    result = evaluate_orchestration_cases(load_dataset(DEFAULT_DATASET))

    assert result["case_count"] == 100
    assert 0.0 <= result["primary_agent_accuracy"] <= 1.0
    assert 0.0 <= result["primary_agent_macro_f1"] <= 1.0
    assert 0.0 <= result["team_exact_match_accuracy"] <= 1.0
    assert set(result["supporting_agent"]) == {
        "exact_match_accuracy", "micro_precision", "micro_recall", "micro_f1"
    }
    assert set(result["multi_agent_detection"]) == {
        "accuracy", "precision", "recall", "f1"
    }
    assert set(result["per_scenario"]) == {
        "general", "technical", "permission", "escalation", "collaboration"
    }


def test_supporting_and_binary_metric_math():
    support = _set_metrics(
        [{"permission"}, {"technical"}, set()],
        [{"permission"}, set(), {"technical"}],
    )
    binary = _binary_metrics([True, True, False, False], [True, False, True, False])

    assert support["exact_match_accuracy"] == 0.3333
    assert support["micro_precision"] == 0.5
    assert support["micro_recall"] == 0.5
    assert support["micro_f1"] == 0.5
    assert binary == {"accuracy": 0.5, "precision": 0.5, "recall": 0.5, "f1": 0.5}


def test_primary_classification_metric_math():
    metrics = _classification_metrics(
        ["general", "technical", "permission"],
        ["general", "permission", "permission"],
    )

    assert metrics["accuracy"] == 0.6667
    assert metrics["macro_f1"] == 0.5556
    assert metrics["per_class"]["technical"]["recall"] == 0.0
