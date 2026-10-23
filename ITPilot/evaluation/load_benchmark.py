"""ITPilot真实HTTP端到端并发压测，仅依赖Python标准库。"""
import argparse
import concurrent.futures
import json
import math
import pathlib
import statistics
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List


DEFAULT_DATASET = pathlib.Path(__file__).parent / "datasets" / "load-benchmark-v1.json"
DEFAULT_REPORT = pathlib.Path("data/eval/load-benchmark-v1-report.json")
REQUIRED_RESPONSE_FIELDS = {"response", "primary_agent", "latency_ms", "request_id", "conv_id"}
FAILURE_RESPONSE_MARKERS = (
    "抱歉，处理您的请求时出现问题，请稍后重试",
    "抱歉，所有 Agent 均处理失败",
    "服务暂时不可用，请稍后重试",
)


def load_dataset(path: pathlib.Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def percentile(values: Iterable[float], percent: float) -> float:
    """使用线性插值计算百分位数，与常见监控平台口径一致。"""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize_results(results: List[Dict[str, Any]], wall_time_s: float) -> Dict[str, Any]:
    successes = [item for item in results if item["success"]]
    transport_successes = [item for item in results if item.get("transport_success")]
    latencies = [item["latency_ms"] for item in successes]
    status_counts = Counter(str(item.get("status", "error")) for item in results)
    error_counts = Counter(item.get("error", "") for item in results if not item["success"])
    return {
        "total_requests": len(results),
        "successful_requests": len(successes),
        "failed_requests": len(results) - len(successes),
        "success_rate": round(len(successes) / len(results), 4) if results else 0.0,
        "http_200_rate": round(len(transport_successes) / len(results), 4) if results else 0.0,
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "mean_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
        "min_ms": round(min(latencies), 1) if latencies else 0.0,
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "wall_time_s": round(wall_time_s, 3),
        "throughput_rps": round(len(successes) / wall_time_s, 3) if wall_time_s > 0 else 0.0,
        "status_counts": dict(status_counts),
        "error_counts": dict(error_counts),
    }


def send_chat(endpoint: str, message: str, case_number: int, timeout_s: float) -> Dict[str, Any]:
    run_token = uuid.uuid4().hex[:10]
    payload = {
        "message": message,
        "user_id": f"load_user_{case_number:04d}_{run_token}",
        "conv_id": f"load_conv_{case_number:04d}_{run_token}",
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
            status = response.status
        latency_ms = (time.perf_counter() - started) * 1000
        parsed = json.loads(body)
        missing = sorted(REQUIRED_RESPONSE_FIELDS - set(parsed))
        response_text = str(parsed.get("response", "")).strip()
        failure_marker = next((marker for marker in FAILURE_RESPONSE_MARKERS if marker in response_text), "")
        transport_success = status == 200 and not missing and bool(response_text)
        valid = transport_success and not failure_marker
        return {
            "case_number": case_number,
            "success": valid,
            "transport_success": transport_success,
            "status": status,
            "latency_ms": latency_ms,
            "server_latency_ms": parsed.get("latency_ms"),
            "primary_agent": parsed.get("primary_agent"),
            "error": "" if valid else (
                f"agent_fallback:{failure_marker}" if failure_marker
                else f"invalid_response:missing={','.join(missing)}"
            ),
            "response_excerpt": response_text[:160] if not valid else "",
        }
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return {
            "case_number": case_number,
            "success": False,
            "transport_success": False,
            "status": exc.code,
            "latency_ms": latency_ms,
            "error": f"http_{exc.code}",
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return {
            "case_number": case_number,
            "success": False,
            "transport_success": False,
            "status": "error",
            "latency_ms": latency_ms,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_load_test(
    endpoint: str,
    prompts: List[Dict[str, str]],
    total_requests: int,
    concurrency: int,
    timeout_s: float,
    warmup_requests: int,
) -> Dict[str, Any]:
    if not prompts:
        raise ValueError("压测集不能为空")
    for index in range(warmup_requests):
        send_chat(endpoint, prompts[index % len(prompts)]["message"], -(index + 1), timeout_s)

    jobs = [prompts[index % len(prompts)] for index in range(total_requests)]
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = [
            executor.submit(send_chat, endpoint, job["message"], index + 1, timeout_s)
            for index, job in enumerate(jobs)
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    wall_time_s = time.perf_counter() - started
    return {
        "timestamp": datetime.now().isoformat(),
        "endpoint": endpoint,
        "concurrency": concurrency,
        "warmup_requests": warmup_requests,
        "timeout_s": timeout_s,
        "category_mix": dict(Counter(job["category"] for job in jobs)),
        "metrics": summarize_results(results, wall_time_s),
        "failures": [item for item in results if not item["success"]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ITPilot HTTP load benchmark")
    parser.add_argument("--endpoint", default="http://localhost:8080/chat")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    dataset = load_dataset(pathlib.Path(args.dataset))
    report = run_load_test(
        endpoint=args.endpoint,
        prompts=dataset["prompts"],
        total_requests=max(1, args.requests),
        concurrency=max(1, args.concurrency),
        timeout_s=max(1.0, args.timeout),
        warmup_requests=max(0, args.warmup),
    )
    report["suite_id"] = dataset.get("suite_id")
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "suite_id": report["suite_id"],
        "endpoint": report["endpoint"],
        "concurrency": report["concurrency"],
        **report["metrics"],
        "report_path": str(output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
