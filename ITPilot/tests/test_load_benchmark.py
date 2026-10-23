import json

from evaluation.load_benchmark import DEFAULT_DATASET, load_dataset, percentile, send_chat, summarize_results


def test_load_dataset_has_expected_mix_and_unique_prompts():
    dataset = load_dataset(DEFAULT_DATASET)
    prompts = dataset["prompts"]
    categories = [item["category"] for item in prompts]
    messages = [item["message"] for item in prompts]

    assert dataset["suite_id"] == "itpilot-load-benchmark-v1"
    assert len(prompts) == 20
    assert categories.count("technical") == 8
    assert categories.count("permission") == 5
    assert categories.count("general") == 4
    assert categories.count("collaboration") == 3
    assert len(messages) == len(set(messages))


def test_percentile_uses_linear_interpolation():
    assert percentile([100, 200, 300, 400], 50) == 250
    assert percentile([100, 200, 300, 400], 95) == 385
    assert percentile([], 95) == 0


def test_summary_calculates_success_latency_and_throughput():
    results = [
        {"success": True, "transport_success": True, "status": 200, "latency_ms": 100, "error": ""},
        {"success": True, "transport_success": True, "status": 200, "latency_ms": 300, "error": ""},
        {"success": False, "transport_success": False, "status": 504, "latency_ms": 500, "error": "http_504"},
        {"success": True, "transport_success": True, "status": 200, "latency_ms": 200, "error": ""},
    ]

    metrics = summarize_results(results, wall_time_s=2.0)

    assert metrics["success_rate"] == 0.75
    assert metrics["http_200_rate"] == 0.75
    assert metrics["p50_ms"] == 200.0
    assert metrics["p95_ms"] == 290.0
    assert metrics["throughput_rps"] == 1.5
    assert metrics["status_counts"] == {"200": 3, "504": 1}
    assert metrics["error_counts"] == {"http_504": 1}


def test_http_200_agent_fallback_is_counted_as_failure(monkeypatch):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "response": "抱歉，处理您的请求时出现问题，请稍后重试。",
                "primary_agent": "general",
                "latency_ms": 20,
                "request_id": "r1",
                "conv_id": "c1",
            }, ensure_ascii=False).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = send_chat("http://test/chat", "测试", 1, 1.0)

    assert result["transport_success"] is True
    assert result["success"] is False
    assert result["error"].startswith("agent_fallback:")
