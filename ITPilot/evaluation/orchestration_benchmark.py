"""ITPilot 多 Agent 编排基准评测。"""
import argparse
import json
import pathlib
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel


DEFAULT_DATASET = pathlib.Path(__file__).parent / "datasets" / "orchestration-benchmark-v1.json"
DEFAULT_INTENT_REPORT = pathlib.Path("data/eval/intent-benchmark-v1-report.json")
DEFAULT_REPORT = pathlib.Path("data/eval/orchestration-benchmark-v1-report.json")


def load_dataset(path: pathlib.Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_routing_orchestrator() -> AgentOrchestrator:
    """构造只执行确定性路由逻辑的编排器，不调用任何Agent或外部模型。"""
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {agent_type: [object()] for agent_type in AgentType}
    return orchestrator


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _round(value: float) -> float:
    return round(value, 4)


def _set_metrics(expected: Iterable[Set[str]], predicted: Iterable[Set[str]]) -> Dict[str, float]:
    expected_sets = list(expected)
    predicted_sets = list(predicted)
    true_positive = sum(len(gold & pred) for gold, pred in zip(expected_sets, predicted_sets))
    false_positive = sum(len(pred - gold) for gold, pred in zip(expected_sets, predicted_sets))
    false_negative = sum(len(gold - pred) for gold, pred in zip(expected_sets, predicted_sets))
    precision = _safe_ratio(true_positive, true_positive + false_positive)
    recall = _safe_ratio(true_positive, true_positive + false_negative)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    exact = sum(gold == pred for gold, pred in zip(expected_sets, predicted_sets))
    return {
        "exact_match_accuracy": _round(_safe_ratio(exact, len(expected_sets))),
        "micro_precision": _round(precision),
        "micro_recall": _round(recall),
        "micro_f1": _round(f1),
    }


def _binary_metrics(expected: List[bool], predicted: List[bool]) -> Dict[str, float]:
    tp = sum(gold and pred for gold, pred in zip(expected, predicted))
    fp = sum(not gold and pred for gold, pred in zip(expected, predicted))
    fn = sum(gold and not pred for gold, pred in zip(expected, predicted))
    tn = sum(not gold and not pred for gold, pred in zip(expected, predicted))
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    return {
        "accuracy": _round(_safe_ratio(tp + tn, len(expected))),
        "precision": _round(precision),
        "recall": _round(recall),
        "f1": _round(f1),
    }


def _classification_metrics(expected: List[str], predicted: List[str]) -> Dict[str, Any]:
    labels = sorted(set(expected) | set(predicted))
    per_class: Dict[str, Dict[str, float]] = {}
    f1_values: List[float] = []
    for label in labels:
        tp = sum(gold == label and pred == label for gold, pred in zip(expected, predicted))
        fp = sum(gold != label and pred == label for gold, pred in zip(expected, predicted))
        fn = sum(gold == label and pred != label for gold, pred in zip(expected, predicted))
        support = sum(gold == label for gold in expected)
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        f1 = _safe_ratio(2 * precision * recall, precision + recall)
        f1_values.append(f1)
        per_class[label] = {
            "precision": _round(precision),
            "recall": _round(recall),
            "f1": _round(f1),
            "support": support,
        }
    return {
        "accuracy": _round(_safe_ratio(
            sum(gold == pred for gold, pred in zip(expected, predicted)), len(expected)
        )),
        "macro_f1": _round(_safe_ratio(sum(f1_values), len(f1_values))),
        "per_class": per_class,
    }


def _intent_to_agent(intent: IntentCategory) -> AgentType:
    return AgentOrchestrator._INTENT_ROUTING.get(intent, AgentType.GENERAL)


def evaluate_orchestration_cases(dataset: Dict[str, Any]) -> Dict[str, Any]:
    orchestrator = build_routing_orchestrator()
    details: List[Dict[str, Any]] = []
    for case in dataset["cases"]:
        intent = IntentCategory(case["intent"])
        urgency = UrgencyLevel[case.get("urgency", "medium").upper()]
        request = Request(
            message=case["message"],
            user_id="orchestration_benchmark",
            conv_id=case["case_id"],
            intent=intent,
            intent_group=IntentRecognizer._intent_group(intent),
            urgency=urgency,
            intent_confidence=float(case.get("intent_confidence", 1.0)),
            entities=case.get("entities", {}),
        )
        decision = orchestrator._route_decision(request)
        expected_supporting = set(case.get("expected_supporting", []))
        predicted_supporting = {agent.value for agent in decision.supporting_agents}
        expected_primary = case["expected_primary"]
        predicted_primary = decision.primary_agent.value
        details.append({
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "message": case["message"],
            "intent": case["intent"],
            "expected_primary": expected_primary,
            "predicted_primary": predicted_primary,
            "expected_supporting": sorted(expected_supporting),
            "predicted_supporting": sorted(predicted_supporting),
            "primary_correct": expected_primary == predicted_primary,
            "team_exact": (
                expected_primary == predicted_primary
                and expected_supporting == predicted_supporting
            ),
            "routing_confidence": decision.confidence,
            "routing_reason": decision.reason,
        })

    expected_support = [set(item["expected_supporting"]) for item in details]
    predicted_support = [set(item["predicted_supporting"]) for item in details]
    expected_multi = [bool(item["expected_supporting"]) for item in details]
    predicted_multi = [bool(item["predicted_supporting"]) for item in details]
    per_scenario: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in details:
        grouped[item["scenario"]].append(item)
    for scenario, items in sorted(grouped.items()):
        per_scenario[scenario] = {
            "total": len(items),
            "primary_accuracy": _round(_safe_ratio(
                sum(item["primary_correct"] for item in items), len(items)
            )),
            "team_exact_match_accuracy": _round(_safe_ratio(
                sum(item["team_exact"] for item in items), len(items)
            )),
        }

    primary_correct = sum(item["primary_correct"] for item in details)
    team_exact = sum(item["team_exact"] for item in details)
    primary_metrics = _classification_metrics(
        [item["expected_primary"] for item in details],
        [item["predicted_primary"] for item in details],
    )
    return {
        "case_count": len(details),
        "scenario_counts": dict(sorted(Counter(item["scenario"] for item in details).items())),
        "primary_agent_accuracy": _round(_safe_ratio(primary_correct, len(details))),
        "primary_agent_macro_f1": primary_metrics["macro_f1"],
        "primary_agent_per_class": primary_metrics["per_class"],
        "team_exact_match_accuracy": _round(_safe_ratio(team_exact, len(details))),
        "supporting_agent": _set_metrics(expected_support, predicted_support),
        "multi_agent_detection": _binary_metrics(expected_multi, predicted_multi),
        "escalation_recall": _round(_safe_ratio(
            sum(
                item["predicted_primary"] == AgentType.ESCALATION.value
                for item in details
                if item["expected_primary"] == AgentType.ESCALATION.value
            ),
            sum(item["expected_primary"] == AgentType.ESCALATION.value for item in details),
        )),
        "per_scenario": per_scenario,
        "failed_cases": [item for item in details if not item["team_exact"]],
        "cases": details,
    }


def evaluate_end_to_end_primary(intent_report_path: pathlib.Path) -> Optional[Dict[str, Any]]:
    """复用意图基准的已保存输出，评估意图识别到主Agent路由的累计效果。"""
    if not intent_report_path.exists():
        return None
    source = json.loads(intent_report_path.read_text(encoding="utf-8"))
    orchestrator = build_routing_orchestrator()
    details: List[Dict[str, Any]] = []
    for item in source.get("cases", []):
        expected_intent = IntentCategory(item["expected_intent"])
        predicted_intent = IntentCategory(item["predictions"]["fusion"])
        expected_primary = _intent_to_agent(expected_intent)
        request = Request(
            message=item["message"],
            user_id="end_to_end_benchmark",
            conv_id=item["case_id"],
            intent=predicted_intent,
            intent_group=IntentRecognizer._intent_group(predicted_intent),
            urgency=UrgencyLevel.MEDIUM,
            intent_confidence=float(item.get("fused_confidence", 1.0)),
        )
        decision = orchestrator._route_decision(request)
        details.append({
            "case_id": item["case_id"],
            "expected_intent": expected_intent.value,
            "predicted_intent": predicted_intent.value,
            "expected_primary": expected_primary.value,
            "predicted_primary": decision.primary_agent.value,
            "correct": expected_primary == decision.primary_agent,
        })
    correct = sum(item["correct"] for item in details)
    primary_metrics = _classification_metrics(
        [item["expected_primary"] for item in details],
        [item["predicted_primary"] for item in details],
    )
    return {
        "source_suite_id": source.get("suite_id"),
        "case_count": len(details),
        "primary_agent_accuracy": _round(_safe_ratio(correct, len(details))),
        "primary_agent_macro_f1": primary_metrics["macro_f1"],
        "primary_agent_per_class": primary_metrics["per_class"],
        "failed_cases": [item for item in details if not item["correct"]],
    }


def run_benchmark(
    dataset_path: pathlib.Path = DEFAULT_DATASET,
    intent_report_path: pathlib.Path = DEFAULT_INTENT_REPORT,
) -> Dict[str, Any]:
    dataset = load_dataset(dataset_path)
    return {
        "timestamp": datetime.now().isoformat(),
        "suite_id": dataset.get("suite_id"),
        "dataset": str(dataset_path),
        "routing_with_gold_signals": evaluate_orchestration_cases(dataset),
        "end_to_end_primary_routing": evaluate_end_to_end_primary(intent_report_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ITPilot orchestration benchmark")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--intent-report", default=str(DEFAULT_INTENT_REPORT))
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    args = parser.parse_args()
    report = run_benchmark(pathlib.Path(args.dataset), pathlib.Path(args.intent_report))
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    routing = report["routing_with_gold_signals"]
    print(json.dumps({
        "suite_id": report["suite_id"],
        "case_count": routing["case_count"],
        "primary_agent_accuracy": routing["primary_agent_accuracy"],
        "primary_agent_macro_f1": routing["primary_agent_macro_f1"],
        "team_exact_match_accuracy": routing["team_exact_match_accuracy"],
        "supporting_agent": routing["supporting_agent"],
        "multi_agent_detection": routing["multi_agent_detection"],
        "escalation_recall": routing["escalation_recall"],
        "per_scenario": routing["per_scenario"],
        "end_to_end_primary_routing": report["end_to_end_primary_routing"],
        "report_path": str(output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
