"""ITPilot RAG离线基准：对比ChromaDB单路检索与查询改写/并行召回/LLM重排。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import statistics
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence, Tuple


DEFAULT_DATASET = pathlib.Path(__file__).parent / "datasets" / "rag-benchmark-v1.json"
DEFAULT_REPORT = pathlib.Path("data/eval/rag-benchmark-v1-report.json")
DEFAULT_SUMMARY = pathlib.Path("data/eval/rag-benchmark-v1-summary.md")
DEFAULT_COLLECTION = "itpilot_rag_benchmark_v1"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_dataset(path: pathlib.Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """加载并展开数据集，返回元数据、固定chunk和100条查询。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    chunks: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    case_no = 0
    for document in payload.get("documents", []):
        for chunk in document.get("chunks", []):
            record = {
                "chunk_id": str(chunk["chunk_id"]),
                "doc_id": str(document["doc_id"]),
                "title": str(document["title"]),
                "category": str(document["category"]),
                "text": str(chunk["text"]),
            }
            chunks.append(record)
            for query in chunk.get("queries", []):
                case_no += 1
                cases.append({
                    "case_id": f"rag_{case_no:03d}",
                    "query": str(query),
                    "category": record["category"],
                    "relevant_chunk_ids": [record["chunk_id"]],
                })
    for multi in payload.get("multi_cases", []):
        cases.append({
            "case_id": str(multi["case_id"]),
            "query": str(multi["query"]),
            "category": "multi",
            "relevant_chunk_ids": [str(item) for item in multi["relevant_chunk_ids"]],
        })
    return payload, chunks, cases


def retrieval_metrics(cases: Sequence[Dict[str, Any]], top_k: int = 5) -> Dict[str, Any]:
    """基于每条case的retrieved_chunk_ids计算宏平均Recall、MRR和Hit。"""
    recalls: List[float] = []
    reciprocal_ranks: List[float] = []
    hits: List[float] = []
    for case in cases:
        relevant = set(case.get("relevant_chunk_ids") or [])
        retrieved = list(case.get("retrieved_chunk_ids") or [])[:top_k]
        if not relevant:
            raise ValueError(f"case {case.get('case_id')} 缺少相关chunk标注")
        matched = relevant.intersection(retrieved)
        recalls.append(len(matched) / len(relevant))
        hits.append(1.0 if matched else 0.0)
        first_rank = next(
            (rank for rank, chunk_id in enumerate(retrieved, start=1) if chunk_id in relevant),
            None,
        )
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
    return {
        "case_count": len(cases),
        f"recall_at_{top_k}": round(statistics.mean(recalls), 4) if recalls else 0.0,
        f"mrr_at_{top_k}": round(statistics.mean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
        f"hit_at_{top_k}": round(statistics.mean(hits), 4) if hits else 0.0,
    }


def validate_dataset(metadata: Dict[str, Any], chunks: Sequence[Dict[str, Any]], cases: Sequence[Dict[str, Any]]) -> None:
    chunk_ids = [item["chunk_id"] for item in chunks]
    case_ids = [item["case_id"] for item in cases]
    queries = [item["query"] for item in cases]
    known = set(chunk_ids)
    if metadata.get("suite_id") != "itpilot-rag-benchmark-v1":
        raise ValueError("suite_id不正确")
    if len(metadata.get("documents", [])) != 30 or len(chunks) != 60 or len(cases) != 100:
        raise ValueError("数据集必须包含30篇文档、60个chunk和100条查询")
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("chunk_id存在重复")
    if len(case_ids) != len(set(case_ids)) or len(queries) != len(set(queries)):
        raise ValueError("case_id或query存在重复")
    for case in cases:
        missing = set(case["relevant_chunk_ids"]) - known
        if missing:
            raise ValueError(f"case {case['case_id']}引用未知chunk: {sorted(missing)}")


def _query_collection(collection: Any, query: str, top_k: int) -> List[Dict[str, Any]]:
    result = collection.query(query_texts=[query], n_results=top_k)
    rows: List[Dict[str, Any]] = []
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    for chunk_id, content, metadata, distance in zip(ids, docs, metas, distances):
        rows.append({
            "chunk_id": str(chunk_id),
            "title": str((metadata or {}).get("title", "")),
            "content": str(content),
            "score": round(1.0 - float(distance), 6),
        })
    return rows


def _connect_collection(args: argparse.Namespace, chunks: Sequence[Dict[str, Any]]) -> Tuple[Any, str]:
    """使用独立集合；仅重建固定的benchmark集合，不触碰业务知识库。"""
    try:
        import chromadb
    except ImportError as ex:
        raise RuntimeError("缺少chromadb依赖，请在项目Docker环境或安装requirements后运行") from ex

    settings = chromadb.Settings(anonymized_telemetry=False)
    backend = args.backend
    client = None
    backend_used = ""
    if backend in ("auto", "http"):
        try:
            client = chromadb.HttpClient(host=args.chroma_host, port=args.chroma_port, settings=settings)
            client.heartbeat()
            backend_used = f"http://{args.chroma_host}:{args.chroma_port}"
        except Exception:
            if backend == "http":
                raise
    if client is None:
        # Chroma 0.5.x默认把ONNX模型写入用户目录。评测环境改为项目内专用缓存，
        # 避免依赖用户目录写权限，也不会影响业务知识库。
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

        ONNXMiniLM_L6_V2.DOWNLOAD_PATH = (
            pathlib.Path(args.chroma_path).parent
            / "chroma-model-cache"
            / ONNXMiniLM_L6_V2.MODEL_NAME
        )
        client = chromadb.PersistentClient(path=args.chroma_path, settings=settings)
        backend_used = f"local:{args.chroma_path}"

    if not args.collection.startswith("itpilot_rag_benchmark_"):
        raise ValueError("benchmark集合名必须以itpilot_rag_benchmark_开头")
    try:
        client.delete_collection(args.collection)
    except Exception:
        pass
    collection = client.get_or_create_collection(
        name=args.collection,
        metadata={"description": "ITPilot RAG benchmark only", "suite_id": "itpilot-rag-benchmark-v1"},
    )
    collection.add(
        ids=[item["chunk_id"] for item in chunks],
        documents=[item["text"] for item in chunks],
        metadatas=[{
            "doc_id": item["doc_id"],
            "title": item["title"],
            "category": item["category"],
        } for item in chunks],
    )
    return collection, backend_used


async def _evaluate_baseline(collection: Any, cases: Sequence[Dict[str, Any]], top_k: int) -> Dict[str, Any]:
    details: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for index, case in enumerate(cases, start=1):
        started = time.perf_counter()
        rows = await asyncio.to_thread(_query_collection, collection, case["query"], top_k)
        latency_ms = (time.perf_counter() - started) * 1000
        latencies.append(latency_ms)
        details.append({
            **case,
            "retrieved_chunk_ids": [item["chunk_id"] for item in rows],
            "retrieved": rows,
            "latency_ms": round(latency_ms, 2),
        })
        if index % 10 == 0:
            print(f"baseline progress: {index}/{len(cases)}", flush=True)
    metrics = retrieval_metrics(details, top_k)
    metrics["avg_latency_ms"] = round(statistics.mean(latencies), 2)
    metrics["p95_latency_ms"] = round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2)
    return {"metrics": metrics, "cases": details}


async def _evaluate_optimized(
    collection: Any,
    cases: Sequence[Dict[str, Any]],
    top_k: int,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    try:
        from mcp.tool_manager import MCPToolManager, Tool
    except ImportError as ex:
        raise RuntimeError("缺少anthropic等项目依赖，无法运行优化检索") from ex

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY为空，无法运行查询改写和LLM重排")
    manager = MCPToolManager(
        api_key=api_key,
        base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
        model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
    )

    # 生产实现会在查询改写失败时静默退回原查询。该行为适合在线服务，
    # 但离线评测若继续运行，会把降级结果误标成“改写+重排”成绩。
    # 因此先直接探测模型是否可用；失败时由run()记录为阻塞，不产出伪优化指标。
    try:
        await manager._client.messages.create(
            model=manager._model,
            max_tokens=8,
            temperature=0,
            messages=[{"role": "user", "content": "仅回复OK"}],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as ex:
        message = str(ex).replace("\n", " ").strip()
        if "Free quota exhausted" in message:
            message = "模型接口返回403：免费额度已耗尽，请充值或关闭仅使用免费额度模式"
        raise RuntimeError(f"优化链路模型预检失败：{message[:500]}") from ex

    async def handler(params: Dict[str, Any], context: Any) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(
            _query_collection,
            collection,
            str(params.get("query", "")),
            int(params.get("top_k", top_k)),
        )

    manager.register(Tool(
        name="knowledge_search",
        description="ITPilot RAG benchmark retrieval",
        handler=handler,
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
            "required": ["query"],
        },
        cache_ttl=0,
        timeout_s=args.timeout,
        supports_rerank=True,
    ))

    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    completed = 0
    progress_lock = asyncio.Lock()

    async def evaluate_case(case: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal completed
        async with semaphore:
            started = time.perf_counter()
            result = await manager.search_with_rewrite("knowledge_search", case["query"], top_k=top_k)
            latency_ms = (time.perf_counter() - started) * 1000
            rows = result.data if result.success and isinstance(result.data, list) else []
            detail = {
                **case,
                "retrieved_chunk_ids": [str(item.get("chunk_id", "")) for item in rows],
                "retrieved": rows,
                "latency_ms": round(latency_ms, 2),
                "success": bool(result.success),
                "error": result.error,
            }
            async with progress_lock:
                completed += 1
                if completed % 10 == 0 or completed == len(cases):
                    print(f"optimized progress: {completed}/{len(cases)}", flush=True)
            return detail

    details = await asyncio.gather(*(evaluate_case(case) for case in cases))
    latencies = [float(case["latency_ms"]) for case in details]
    failures = sum(not case["success"] for case in details)
    metrics = retrieval_metrics(details, top_k)
    metrics["avg_latency_ms"] = round(statistics.mean(latencies), 2)
    metrics["p95_latency_ms"] = round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2)
    metrics["failed_cases"] = failures
    return {"metrics": metrics, "cases": details}


def _load_dotenv(path: pathlib.Path = pathlib.Path(".env")) -> None:
    """仅补充当前进程缺失的环境变量，避免脚本依赖python-dotenv。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _summary_markdown(report: Dict[str, Any], top_k: int) -> str:
    baseline = report["baseline"]["metrics"]
    lines = [
        "# ITPilot RAG检索基准评测报告",
        "",
        f"- 套件：`{report['suite_id']}`",
        f"- 文档：{report['document_count']}篇",
        f"- Chunk：{report['chunk_count']}个",
        f"- 独立查询：{report['case_count']}条",
        f"- 后端：`{report['backend']}`",
        "",
        "| 模式 | Recall@5 | MRR@5 | Hit@5 | 平均延迟 | P95延迟 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        f"| 基础向量检索 | {baseline[f'recall_at_{top_k}']:.2%} | {baseline[f'mrr_at_{top_k}']:.2%} | {baseline[f'hit_at_{top_k}']:.2%} | {baseline['avg_latency_ms']:.2f}ms | {baseline['p95_latency_ms']:.2f}ms |",
    ]
    if "optimized" in report:
        optimized = report["optimized"]["metrics"]
        lines.append(
            f"| 改写+并行召回+重排 | {optimized[f'recall_at_{top_k}']:.2%} | {optimized[f'mrr_at_{top_k}']:.2%} | {optimized[f'hit_at_{top_k}']:.2%} | {optimized['avg_latency_ms']:.2f}ms | {optimized['p95_latency_ms']:.2f}ms |"
        )
        lines.extend([
            "",
            f"- Recall@5变化：{(optimized[f'recall_at_{top_k}'] - baseline[f'recall_at_{top_k}']) * 100:+.2f}个百分点",
            f"- Recall@5相对提升：{((optimized[f'recall_at_{top_k}'] / baseline[f'recall_at_{top_k}']) - 1) * 100:+.2f}%" if baseline[f'recall_at_{top_k}'] else "- Recall@5相对提升：基线为0，无法计算",
            f"- MRR@5变化：{(optimized[f'mrr_at_{top_k}'] - baseline[f'mrr_at_{top_k}']) * 100:+.2f}个百分点",
            f"- MRR@5相对提升：{((optimized[f'mrr_at_{top_k}'] / baseline[f'mrr_at_{top_k}']) - 1) * 100:+.2f}%" if baseline[f'mrr_at_{top_k}'] else "- MRR@5相对提升：基线为0，无法计算",
            f"- 优化链路失败：{optimized.get('failed_cases', 0)}条",
        ])
    elif "optimized_blocked" in report:
        blocked = report["optimized_blocked"]
        lines.extend([
            "",
            f"- 优化链路状态：**未完成**（{blocked['reason']}）",
            "- 说明：未把查询改写失败后的原查询降级结果冒充为优化检索成绩。",
        ])
    lines.extend([
        "",
        "## 口径",
        "",
        "Recall@5为每条查询召回的相关chunk比例的宏平均；MRR@5为首个相关chunk倒数排名的宏平均。",
        "所有模式使用相同知识库、相同查询和相同Top-5，测试集未参与检索逻辑调参。",
        "",
    ])
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    _load_dotenv()
    metadata, chunks, cases = load_dataset(pathlib.Path(args.dataset))
    validate_dataset(metadata, chunks, cases)
    collection, backend_used = _connect_collection(args, chunks)
    top_k = int(metadata.get("top_k", args.top_k) or args.top_k)
    baseline = await _evaluate_baseline(collection, cases, top_k)
    report: Dict[str, Any] = {
        "suite_id": metadata["suite_id"],
        "timestamp": datetime.now().isoformat(),
        "dataset": str(args.dataset),
        "backend": backend_used,
        "collection": args.collection,
        "document_count": len(metadata["documents"]),
        "chunk_count": len(chunks),
        "case_count": len(cases),
        "top_k": top_k,
        "baseline": baseline,
    }
    if args.mode == "both":
        try:
            report["optimized"] = await _evaluate_optimized(collection, cases, top_k, args)
        except RuntimeError as ex:
            report["optimized_blocked"] = {
                "status": "blocked",
                "reason": str(ex),
            }

    output = pathlib.Path(args.output)
    summary = pathlib.Path(args.summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary.write_text(_summary_markdown(report, top_k), encoding="utf-8")
    print(_summary_markdown(report, top_k), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ITPilot RAG retrieval benchmark")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--mode", choices=("baseline", "both"), default="both")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--backend", choices=("auto", "http", "local"), default="auto")
    parser.add_argument("--chroma-host", default="localhost")
    parser.add_argument("--chroma-port", type=int, default=8001)
    parser.add_argument("--chroma-path", default="data/chroma-rag-benchmark")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
