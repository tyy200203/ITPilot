import json
from pathlib import Path

from evaluation.rag_benchmark import (
    _summary_markdown,
    load_dataset,
    retrieval_metrics,
    validate_dataset,
)


DATASET_PATH = Path(__file__).parents[1] / "evaluation" / "datasets" / "rag-benchmark-v1.json"


def test_rag_benchmark_dataset_has_expected_size_and_valid_gold_chunks():
    metadata, chunks, cases = load_dataset(DATASET_PATH)

    validate_dataset(metadata, chunks, cases)
    assert len(metadata["documents"]) == 30
    assert len(chunks) == 60
    assert len(cases) == 100
    assert sum(len(case["relevant_chunk_ids"]) > 1 for case in cases) == 10
    assert len({case["query"] for case in cases}) == 100


def test_rag_benchmark_json_contains_no_result_leakage():
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    raw = json.dumps(payload, ensure_ascii=False)

    assert "retrieved_chunk_ids" not in raw
    assert "recall_at_5" not in raw
    assert "mrr_at_5" not in raw


def test_retrieval_metrics_calculate_recall_mrr_and_hit_at_five():
    cases = [
        {
            "case_id": "one",
            "relevant_chunk_ids": ["a"],
            "retrieved_chunk_ids": ["x", "a", "z"],
        },
        {
            "case_id": "two",
            "relevant_chunk_ids": ["b", "c"],
            "retrieved_chunk_ids": ["b", "x", "y"],
        },
        {
            "case_id": "three",
            "relevant_chunk_ids": ["d"],
            "retrieved_chunk_ids": ["x", "y", "z"],
        },
    ]

    assert retrieval_metrics(cases, top_k=5) == {
        "case_count": 3,
        "recall_at_5": 0.5,
        "mrr_at_5": 0.5,
        "hit_at_5": 0.6667,
    }


def test_summary_does_not_present_blocked_optimized_run_as_a_metric():
    report = {
        "suite_id": "itpilot-rag-benchmark-v1",
        "document_count": 30,
        "chunk_count": 60,
        "case_count": 100,
        "backend": "local:test",
        "baseline": {
            "metrics": {
                "recall_at_5": 0.59,
                "mrr_at_5": 0.4343,
                "hit_at_5": 0.62,
                "avg_latency_ms": 50.0,
                "p95_latency_ms": 60.0,
            }
        },
        "optimized_blocked": {
            "status": "blocked",
            "reason": "模型额度不足",
        },
    }

    summary = _summary_markdown(report, top_k=5)

    assert "优化链路状态：**未完成**" in summary
    assert "模型额度不足" in summary
    assert "| 改写+并行召回+重排 |" not in summary
