"""可复现的 ITPilot 意图识别平衡基准与消融评测。"""
import argparse
import asyncio
import json
import os
import pathlib
import statistics
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from core.intent_recognizer import IntentCategory, IntentRecognizer


DEFAULT_DATASET = pathlib.Path(__file__).parent / "datasets" / "intent-benchmark-v1.json"
DEFAULT_REPORT = pathlib.Path("data/eval/intent-benchmark-v1-report.json")


def load_dataset(path: pathlib.Path) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: List[Dict[str, str]] = []
    for intent, messages in data.get("cases_by_intent", {}).items():
        for index, message in enumerate(messages):
            cases.append({
                "case_id": f"{intent}_{index + 1:02d}",
                "message": str(message),
                "expected_intent": intent,
            })
    return data, cases


def classification_metrics(
    expected: Iterable[str],
    predicted: Iterable[str],
) -> Dict[str, Any]:
    truth = list(expected)
    preds = list(predicted)
    labels = sorted(set(truth) | set(preds))
    per_class: Dict[str, Dict[str, float]] = {}
    for label in labels:
        tp = sum(p == label and g == label for p, g in zip(preds, truth))
        fp = sum(p == label and g != label for p, g in zip(preds, truth))
        fn = sum(p != label and g == label for p, g in zip(preds, truth))
        support = sum(g == label for g in truth)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
        }
    correct = sum(p == g for p, g in zip(preds, truth))
    accuracy = correct / len(truth) if truth else 0.0
    macro_f1 = statistics.mean(item["f1"] for item in per_class.values()) if per_class else 0.0
    return {
        "total": len(truth),
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "macro_f1": round(macro_f1, 4),
        "per_class": per_class,
    }


async def run_benchmark(
    recognizer: IntentRecognizer,
    cases: List[Dict[str, str]],
    concurrency: int = 5,
) -> Dict[str, Any]:
    """每个样本只调用一次LLM，同时计算融合与三组消融结果。"""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    completed = 0
    lock = asyncio.Lock()

    async def evaluate_case(case: Dict[str, str]) -> Dict[str, Any]:
        nonlocal completed
        async with semaphore:
            llm_task = asyncio.create_task(recognizer._llm_recognize(case["message"], None))
            emb_task = asyncio.create_task(recognizer._embedding_recognize(case["message"]))
            pattern = recognizer._pattern_recognize(case["message"])
            llm, embedding = await asyncio.gather(llm_task, emb_task)
            fused, fused_confidence, source_scores = recognizer._vote(llm, embedding, pattern)

            llm_confidence = float(llm.get("confidence", 0.0) or 0.0)
            llm_intent = llm.get("intent", IntentCategory.OTHER)
            if llm.get("failed") or llm_confidence < recognizer.threshold:
                llm_intent = IntentCategory.OTHER

            embedding_intent = embedding.get("intent", IntentCategory.OTHER)
            if float(embedding.get("confidence", 0.0) or 0.0) < recognizer.threshold:
                embedding_intent = IntentCategory.OTHER

            pattern_intent = pattern.get("intent", IntentCategory.OTHER)
            if float(pattern.get("confidence", 0.0) or 0.0) <= 0:
                pattern_intent = IntentCategory.OTHER

            async with lock:
                completed += 1
                if completed % 10 == 0 or completed == len(cases):
                    print(f"progress: {completed}/{len(cases)}", flush=True)

            return {
                **case,
                "predictions": {
                    "fusion": fused.value,
                    "llm_only": llm_intent.value,
                    "embedding_only": embedding_intent.value,
                    "pattern_only": pattern_intent.value,
                },
                "fused_confidence": round(float(fused_confidence), 4),
                "source_scores": source_scores,
                "llm_failed": bool(llm.get("failed")),
                "llm_reasoning": str(llm.get("reasoning", "")),
            }

    details = await asyncio.gather(*(evaluate_case(case) for case in cases))
    expected = [case["expected_intent"] for case in details]
    modes = {}
    for mode in ("fusion", "llm_only", "embedding_only", "pattern_only"):
        predictions = [case["predictions"][mode] for case in details]
        modes[mode] = classification_metrics(expected, predictions)

    fusion = modes["fusion"]
    llm_only = modes["llm_only"]
    confusion = Counter(
        (case["expected_intent"], case["predictions"]["fusion"])
        for case in details
        if case["expected_intent"] != case["predictions"]["fusion"]
    )
    return {
        "timestamp": datetime.now().isoformat(),
        "case_count": len(cases),
        "class_count": len(set(expected)),
        "concurrency": concurrency,
        "llm_failures": sum(1 for case in details if case["llm_failed"]),
        "modes": modes,
        "improvement_vs_llm_only": {
            "accuracy_points": round((fusion["accuracy"] - llm_only["accuracy"]) * 100, 2),
            "macro_f1_points": round((fusion["macro_f1"] - llm_only["macro_f1"]) * 100, 2),
        },
        "top_confusions": [
            {"expected": pair[0], "predicted": pair[1], "count": count}
            for pair, count in confusion.most_common(20)
        ],
        "cases": details,
    }


async def _main(args: argparse.Namespace) -> None:
    dataset_path = pathlib.Path(args.dataset)
    report_path = pathlib.Path(args.output)
    metadata, cases = load_dataset(dataset_path)
    recognizer = IntentRecognizer(
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
        model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
    )
    report = await run_benchmark(recognizer, cases, concurrency=args.concurrency)
    report["suite_id"] = metadata.get("suite_id")
    report["dataset"] = str(dataset_path)
    report["model"] = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "suite_id": report["suite_id"],
        "case_count": report["case_count"],
        "class_count": report["class_count"],
        "llm_failures": report["llm_failures"],
        "modes": {
            name: {
                "accuracy": metrics["accuracy"],
                "macro_f1": metrics["macro_f1"],
            }
            for name, metrics in report["modes"].items()
        },
        "improvement_vs_llm_only": report["improvement_vs_llm_only"],
        "top_confusions": report["top_confusions"][:10],
        "report_path": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ITPilot intent benchmark")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
