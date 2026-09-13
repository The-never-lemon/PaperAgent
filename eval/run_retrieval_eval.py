"""检索评估 L1：对论文检索服务进行组件级评估。

这个脚本执行以下步骤：
1. 加载 24 条真实评估用例
2. 对每条用例调用 PaperSearchService.async_search 进行真实检索
3. 计算去重、字段完整性等确定性指标
4. 使用 LLM 裁判评估论文相关性，计算 nDCG、Precision 等指标
5. 调用 async_related 进行引文扩展和相关性评估
6. 输出 retrieval.json 和人读版 retrieval.md

CLI 使用：
  python eval/run_retrieval_eval.py
  python eval/run_retrieval_eval.py --skip-judge --only r01,r02
  python eval/run_retrieval_eval.py --replay reports/20260910_154514/raw
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.llm.config import SystemConfig
from src.paper_retrieval.service import PaperSearchService
from src.paper_retrieval.models import SearchResponse
from eval import config as eval_config
from eval.lib.judge import build_judge_deps, run_relevance_judge, estimate_cost_cny
from eval.lib.event_stats import percentile
from eval.lib.workspace_reader import (
    check_duplicates,
    check_field_completeness,
    normalize_doi,
    normalize_arxiv_id,
    normalize_title,
)

logger = logging.getLogger(__name__)


# ==================== 数据模型 ====================

@dataclass(slots=True)
class CaseMetrics:
    """单条用例的评估指标。"""
    case_id: str
    domain: str
    difficulty: str
    status: str  # "ok" 或 "infra_error"
    latency_s: float
    judge_status: str | None  # "ok", "judge_failed", "skipped", None
    metrics: dict  # 本条全部指标


@dataclass(slots=True)
class AggregateMetrics:
    """全量聚合指标。"""
    ndcg_at_10: float | None
    precision_at_10: float | None
    golden_recall: float | None
    duplicate_violations: int
    field_completeness: dict
    sources_ok_rate: float
    errors_empty_rate: float
    rerank_monotonicity_gap: float | None
    multi_source_topk_rate: float | None
    expand_success_rate: float | None
    expand_relevance_avg: float | None
    latency_s: dict  # {"p50": ..., "p95": ...}
    # 中文注释：被评为"裁判饱和"的用例数——这些用例里所有论文拿到的分数完全相同，
    # nDCG 必然等于 1。它们已经从裁判类指标的均值里排除，只在这里计数留痕。
    judge_saturated_cases: int = 0


# ==================== 指标计算函数 ====================

def calculate_ndcg_at_k(scores: list[float], k: int = 10) -> float:
    """计算 nDCG@k 指标。

    分数应该是 0~4 之间的相关性得分（0 无关，1 顺带提及，2 相邻领域，3 直接研究某侧面，4 核心工作）。
    使用标准公式：DCG = sum(relevance_i / log2(i+1))，其中 i 是 1-based 位置。
    理想增益(ideal DCG)用分数降序排列计算。

    Args:
        scores: 论文相关性分数列表
        k: 取前 k 个计算

    Returns:
        nDCG@k 值（0~1）
    """
    scores = scores[:k]
    if not scores:
        return 0.0

    # 计算实际 DCG（使用 log2 对数折扣，i 是 0-based，位置 p = i+1）
    dcg = 0.0
    for i, score in enumerate(scores):
        dcg += score / math.log2(i + 2)

    # 计算理想 DCG（分数降序，使用同样的 log2 折扣）
    ideal_scores = sorted(scores, reverse=True)
    ideal_dcg = 0.0
    for i, score in enumerate(ideal_scores):
        ideal_dcg += score / math.log2(i + 2)

    if ideal_dcg == 0:
        return 0.0

    return dcg / ideal_dcg


def calculate_precision_at_k(scores: list[float], k: int = 10, threshold: float = 2.0) -> float:
    """计算 Precision@k 指标（相关性≥threshold 的占比）。

    Args:
        scores: 论文相关性分数列表（0~4）
        k: 取前 k 个计算
        threshold: 判定为相关的最小分数。默认 2.0（"属于同一大领域及以上"）——
            在旧的 0~2 分制里，同样口径对应的是 1.0

    Returns:
        P@k（0~1）
    """
    scores = scores[:k]
    if not scores:
        return 0.0

    relevant = sum(1 for s in scores if s >= threshold)
    return relevant / len(scores)


def calculate_rerank_monotonicity(scores: list[float]) -> float:
    """计算重排单调性缺失（gap）。

    单调性缺失 = top-5 平均分 - bottom-5 平均分。值越大越好。
    如果论文少于 10 篇，用实际数量的上下各 50%。

    Args:
        scores: 相关性分数列表

    Returns:
        单调性缺失值（正表示有序度好）
    """
    if not scores:
        return 0.0

    n = len(scores)
    if n < 2:
        return 0.0

    # 分成上下两部分
    top_count = max(1, n // 2)
    bottom_count = n - top_count

    top_avg = sum(scores[:top_count]) / top_count if top_count > 0 else 0.0
    bottom_avg = sum(scores[-bottom_count:]) / bottom_count if bottom_count > 0 else 0.0

    return top_avg - bottom_avg


def calculate_multi_source_topk_rate(papers: list[dict], k: int = 10) -> float | None:
    """计算多源命中率@k。

    论文的 metadata["sources"] 列表中源数≥2 的论文落在 top-k 的占比。

    Args:
        papers: PaperDocument.to_dict() 论文列表

    Returns:
        多源命中率（0~1）或 None（无法计算）
    """
    topk = papers[:k]
    if not topk:
        return None

    multi_source_count = 0
    for paper in topk:
        sources = paper.get("metadata", {}).get("sources", [])
        if isinstance(sources, list) and len(sources) >= 2:
            multi_source_count += 1

    return multi_source_count / len(topk) if topk else None


# ==================== 异步搜索与指标计算 ====================

async def run_case(
    case: dict,
    deps: Any,
    skip_judge: bool = False,
    replay_raw_dir: Path | None = None,
    out_raw_dir: Path | None = None,
    rejudge: bool = False,
) -> tuple[CaseMetrics, dict | None, dict | None]:
    """执行一条评估用例。

    Args:
        case: retrieval_cases.json 中的单条用例
        deps: JudgeDeps 对象（若需要裁判）
        skip_judge: 是否跳过 LLM 裁判
        replay_raw_dir: 若非空，从此目录读历史 search_response.json 而不调 API
        out_raw_dir: 输出目录中的 raw/ 子目录
        rejudge: 重放模式下是否仍然跑裁判。默认 False（重放 = 零成本）；
            想验证裁判提示词改动的效果时传 True，就不必重新做一次真实检索

    Returns:
        (CaseMetrics, judge_output, expand_response)
        其中 judge_output 和 expand_response 可能为 None
    """
    case_id = case["case_id"]
    start_time = time.time()

    # 创建输出目录
    if out_raw_dir is None:
        out_raw_dir = Path(eval_config.REPORTS_DIR) / (
            datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ) / "raw"

    raw_dir = out_raw_dir / case_id
    raw_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 第一步：获取 SearchResponse
        if replay_raw_dir:
            # 重放模式：从磁盘读
            search_response_path = replay_raw_dir / case_id / "search_response.json"
            if not search_response_path.exists():
                logger.warning(f"用例 {case_id} 的历史搜索结果不存在：{search_response_path}")
                return (
                    CaseMetrics(
                        case_id=case_id,
                        domain=case["domain"],
                        difficulty=case["difficulty"],
                        status="infra_error",
                        latency_s=0,
                        judge_status="skipped",
                        metrics={},
                    ),
                    None,
                    None,
                )

            with open(search_response_path, "r", encoding="utf-8") as f:
                search_dict = json.load(f)
        else:
            # 实时模式：调用检索服务
            service = PaperSearchService()
            search_response = await service.async_search(
                topic=case.get("topic", ""),
                concept_groups=case.get("concept_groups", []),
                limit=case.get("limit", 10),
                year_from=case.get("year_from"),
                year_to=case.get("year_to"),
            )
            search_dict = search_response.to_dict()

            # 落盘 search_response
            with open(raw_dir / "search_response.json", "w", encoding="utf-8") as f:
                json.dump(search_dict, f, ensure_ascii=False, indent=2)

        latency = time.time() - start_time

        # 检查是否全源报错
        errors = search_dict.get("errors", {})
        sources_used = search_dict.get("sources_used", [])
        if errors and len(errors) == len(sources_used) and not search_dict.get("papers"):
            logger.info(f"用例 {case_id} 全源报错，标记为 infra_error")
            return (
                CaseMetrics(
                    case_id=case_id,
                    domain=case["domain"],
                    difficulty=case["difficulty"],
                    status="infra_error",
                    latency_s=latency,
                    judge_status="skipped",
                    metrics={"error": "所有检索源均报错"},
                ),
                None,
                None,
            )

        # 第二步：计算确定性指标
        papers = search_dict.get("papers", [])
        metrics: dict[str, Any] = {}

        # 去重检查
        duplicates = check_duplicates(papers)
        metrics["duplicate_violations"] = len(duplicates)

        # 字段完整性
        completeness = check_field_completeness(papers)
        metrics["field_completeness"] = completeness

        # 源数检查
        min_sources = case.get("min_sources_expected", 1)
        sources_ok = len(sources_used) >= min_sources
        metrics["sources_ok"] = sources_ok

        # 错误检查
        errors_ok = not errors
        metrics["errors_empty"] = errors_ok

        # 多源 top-k
        multi_source_rate = calculate_multi_source_topk_rate(papers, k=10)
        metrics["multi_source_topk_rate"] = multi_source_rate

        # Golden 召回（前 20 篇）
        golden_papers = case.get("golden_papers", [])
        golden_recall = calculate_golden_recall(papers[:20], golden_papers)
        metrics["golden_recall"] = golden_recall

        judge_status = None
        judge_output = None

        # 第三步：LLM 裁判（可选）
        # 中文注释：重放模式默认不判分（保持零成本）；只有显式传了 rejudge 才重新判一次。
        if not skip_judge and (not replay_raw_dir or rejudge) and papers:
            # 只对前 10 篇裁判
            papers_to_judge = papers[:10]
            judge_result = await run_relevance_judge(
                deps,
                case_id=case_id,
                topic=case.get("topic", ""),
                concept_groups=case.get("concept_groups", []),
                papers=papers_to_judge,
            )

            if judge_result["status"] == "ok":
                judge_output = judge_result
                judge_status = "ok"

                # 从裁判分数计算排序指标
                scores = [s.get("score", 0) for s in judge_result.get("scores", [])]
                # 中文注释：如果这些论文拿到的分数**完全相同**，那么"实际顺序"和"理想顺序"
                # 必然一致，nDCG 恒等于 1。这不是"排序好"，而是"裁判根本没给出区分度"。
                # 标出来，聚合时把它排除，免得满分假绿灯继续通过阈值。
                metrics["judge_saturated"] = len(set(scores)) < 2
                metrics["judge_score_span"] = (max(scores) - min(scores)) if scores else 0
                metrics["ndcg_at_10"] = calculate_ndcg_at_k(scores, k=10)
                metrics["precision_at_10"] = calculate_precision_at_k(scores, k=10, threshold=2.0)
                metrics["rerank_monotonicity_gap"] = calculate_rerank_monotonicity(scores)

                # 落盘 judge_output
                with open(raw_dir / "judge_output.json", "w", encoding="utf-8") as f:
                    json.dump(judge_output, f, ensure_ascii=False, indent=2)
            else:
                judge_status = "judge_failed"
                metrics["judge_error"] = judge_result.get("reason", "未知错误")
        else:
            judge_status = "skipped"

        # 第四步：引文扩展（可选）
        expand_response = None
        expand_seed_index = case.get("expand_seed_index", 0)
        if expand_seed_index < len(papers) and not replay_raw_dir:
            seed_paper = papers[expand_seed_index]
            seed_id = seed_paper.get("paperId") or seed_paper.get("id")

            if seed_id:
                try:
                    service = PaperSearchService()
                    expand_resp = await service.async_related(
                        external_ref=seed_id,
                        direction="references",
                        limit=5,
                    )
                    expand_response = expand_resp.to_dict()

                    # 评估扩展结果的相关性
                    expand_papers = expand_response.get("papers", [])
                    if expand_papers and not skip_judge and deps:
                        expand_judge = await run_relevance_judge(
                            deps,
                            case_id=f"{case_id}_expand",
                            topic=case.get("topic", ""),
                            concept_groups=case.get("concept_groups", []),
                            papers=expand_papers[:5],
                        )
                        if expand_judge["status"] == "ok":
                            scores = [s.get("score", 0) for s in expand_judge.get("scores", [])]
                            expand_avg = sum(scores) / len(scores) if scores else 0
                            metrics["expand_relevance_avg"] = expand_avg

                    # 落盘 expand_response
                    with open(raw_dir / "expand_response.json", "w", encoding="utf-8") as f:
                        json.dump(expand_response, f, ensure_ascii=False, indent=2)

                    metrics["expand_success"] = bool(expand_papers)
                except Exception as exc:
                    logger.warning(f"用例 {case_id} 引文扩展失败：{exc}")
                    metrics["expand_error"] = str(exc)

        latency = time.time() - start_time

        return (
            CaseMetrics(
                case_id=case_id,
                domain=case["domain"],
                difficulty=case["difficulty"],
                status="ok",
                latency_s=latency,
                judge_status=judge_status,
                metrics=metrics,
            ),
            judge_output,
            expand_response,
        )

    except Exception as exc:
        logger.exception(f"用例 {case_id} 执行异常")
        return (
            CaseMetrics(
                case_id=case_id,
                domain=case["domain"],
                difficulty=case["difficulty"],
                status="infra_error",
                latency_s=time.time() - start_time,
                judge_status=None,
                metrics={"error": str(exc)},
            ),
            None,
            None,
        )


def calculate_golden_recall(papers: list[dict], golden_papers: list[dict]) -> float:
    """计算 golden 论文的召回率。

    检查前 20 篇中有多少命中了 golden_papers 的 arxiv_id/doi/标题。

    Args:
        papers: 检索结果前 20 篇
        golden_papers: golden 论文列表 [{"arxiv_id": ..., "doi": ..., "title_hint": ...}, ...]

    Returns:
        召回率（0~1）
    """
    if not golden_papers:
        return 1.0

    # 规范化论文 ID
    paper_dois = set()
    paper_arxiv_ids = set()
    paper_titles = set()

    for paper in papers:
        doi = paper.get("doi", "")
        if doi:
            paper_dois.add(normalize_doi(doi))

        paper_id = paper.get("paperId", "")
        if paper_id and re.match(r"^\d{4}\.\d{4,5}(?:v\d+)?$", paper_id):
            paper_arxiv_ids.add(normalize_arxiv_id(paper_id))

        title = paper.get("title", "")
        if title:
            paper_titles.add(normalize_title(title))

    # 检查 golden 论文
    matched = 0
    for golden in golden_papers:
        matched_this = False

        # 按 arxiv_id 匹配
        if golden.get("arxiv_id"):
            if normalize_arxiv_id(golden["arxiv_id"]) in paper_arxiv_ids:
                matched_this = True

        # 按 doi 匹配
        if not matched_this and golden.get("doi"):
            if normalize_doi(golden["doi"]) in paper_dois:
                matched_this = True

        # 按标题提示匹配
        if not matched_this and golden.get("title_hint"):
            hint_normalized = normalize_title(golden["title_hint"])
            for title in paper_titles:
                if hint_normalized in title or title in hint_normalized:
                    matched_this = True
                    break

        if matched_this:
            matched += 1

    return matched / len(golden_papers) if golden_papers else 0


# ==================== 聚合与报告 ====================

def aggregate_metrics(all_cases: list[CaseMetrics]) -> AggregateMetrics:
    """聚合所有用例的指标。

    聚合规则：
    - 裁判类指标只对 status=ok && judge_status=ok 的用例求均值
    - 确定性指标对 status=ok 的用例求统计

    Args:
        all_cases: 所有用例的评估结果

    Returns:
        AggregateMetrics 聚合数据
    """
    ok_cases = [c for c in all_cases if c.status == "ok"]
    judged_cases = [c for c in all_cases if c.status == "ok" and c.judge_status == "ok"]

    # 裁判指标
    ndcg_values = []
    precision_values = []
    golden_recalls = []
    expand_success_count = 0
    expand_relevances = []
    rerank_monotonicity = []

    # 中文注释：把"裁判饱和"的用例单独拎出来计数。它们只在下面三个**裁判类**指标上
    # 被排除——分数全同意味着 nDCG 必然是 1，混进均值只会抬高分数、掩盖真实排序质量。
    # 金标召回是确定性指标、扩展成功率与裁判无关，都照常统计。
    saturated_cases = [c for c in judged_cases if c.metrics.get("judge_saturated")]

    for case in judged_cases:
        saturated = bool(case.metrics.get("judge_saturated"))
        if not saturated:
            if "ndcg_at_10" in case.metrics:
                ndcg_values.append(case.metrics["ndcg_at_10"])
            if "precision_at_10" in case.metrics:
                precision_values.append(case.metrics["precision_at_10"])
            if "rerank_monotonicity_gap" in case.metrics:
                rerank_monotonicity.append(case.metrics["rerank_monotonicity_gap"])
        if "golden_recall" in case.metrics:
            golden_recalls.append(case.metrics["golden_recall"])
        if case.metrics.get("expand_success"):
            expand_success_count += 1
        if "expand_relevance_avg" in case.metrics:
            expand_relevances.append(case.metrics["expand_relevance_avg"])

    # 确定性指标
    duplicate_sum = sum(c.metrics.get("duplicate_violations", 0) for c in ok_cases)

    # 字段完整性：聚合所有用例的数据
    all_completeness = []
    for case in ok_cases:
        complete = case.metrics.get("field_completeness", {})
        if complete:
            all_completeness.append(complete)

    field_completeness = {
        "title": sum(c.get("title", 0) for c in all_completeness) / len(all_completeness) if all_completeness else 0,
        "year": sum(c.get("year", 0) for c in all_completeness) / len(all_completeness) if all_completeness else 0,
        "abstract": sum(c.get("abstract", 0) for c in all_completeness) / len(all_completeness) if all_completeness else 0,
        "id_doi_or_paperid": sum(c.get("id_doi_or_paperid", 0) for c in all_completeness) / len(all_completeness) if all_completeness else 0,
    }

    # 源数和错误检查
    sources_ok_count = sum(1 for c in ok_cases if c.metrics.get("sources_ok", False))
    errors_ok_count = sum(1 for c in ok_cases if c.metrics.get("errors_empty", False))

    # 多源率
    multi_source_rates = [c.metrics.get("multi_source_topk_rate") for c in ok_cases if c.metrics.get("multi_source_topk_rate") is not None]

    # 延迟统计
    latencies = [c.latency_s for c in ok_cases if c.latency_s > 0]

    return AggregateMetrics(
        ndcg_at_10=sum(ndcg_values) / len(ndcg_values) if ndcg_values else None,
        precision_at_10=sum(precision_values) / len(precision_values) if precision_values else None,
        golden_recall=sum(golden_recalls) / len(golden_recalls) if golden_recalls else None,
        duplicate_violations=duplicate_sum,
        field_completeness=field_completeness,
        sources_ok_rate=sources_ok_count / len(ok_cases) if ok_cases else 0,
        errors_empty_rate=errors_ok_count / len(ok_cases) if ok_cases else 0,
        rerank_monotonicity_gap=sum(rerank_monotonicity) / len(rerank_monotonicity) if rerank_monotonicity else None,
        multi_source_topk_rate=sum(multi_source_rates) / len(multi_source_rates) if multi_source_rates else None,
        expand_success_rate=expand_success_count / len(ok_cases) if ok_cases else None,
        expand_relevance_avg=sum(expand_relevances) / len(expand_relevances) if expand_relevances else None,
        latency_s={
            "p50": percentile(latencies, 50) if latencies else 0,
            "p95": percentile(latencies, 95) if latencies else 0,
        },
        judge_saturated_cases=len(saturated_cases),
    )


def generate_retrieval_json(
    all_cases: list[CaseMetrics],
    mode: str,
    usage_total: dict,
    judge_deps: Any | None,
) -> dict:
    """生成 retrieval.json 输出。

    Args:
        all_cases: 所有用例评估结果
        mode: "live" / "skip-judge" / "replay"
        usage_total: 累计 token 用量
        judge_deps: JudgeDeps（用于成本估算）

    Returns:
        retrieval.json 的内容
    """
    aggregate = aggregate_metrics(all_cases)

    # 成本估算
    model_name = judge_deps.model_name if judge_deps else None
    cost_cny = estimate_cost_cny(usage_total, model_name)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "cases_total": 24,
        "cases_run": len(all_cases),
        "infra_errors": sum(1 for c in all_cases if c.status == "infra_error"),
        "aggregate": {
            "ndcg_at_10": aggregate.ndcg_at_10,
            "precision_at_10": aggregate.precision_at_10,
            "golden_recall": aggregate.golden_recall,
            "duplicate_violations": aggregate.duplicate_violations,
            "field_completeness": aggregate.field_completeness,
            "sources_ok_rate": aggregate.sources_ok_rate,
            "errors_empty_rate": aggregate.errors_empty_rate,
            "rerank_monotonicity_gap": aggregate.rerank_monotonicity_gap,
            "multi_source_topk_rate": aggregate.multi_source_topk_rate,
            "expand_success_rate": aggregate.expand_success_rate,
            "expand_relevance_avg": aggregate.expand_relevance_avg,
            "latency_s": aggregate.latency_s,
        },
        "cases": [
            {
                "case_id": c.case_id,
                "domain": c.domain,
                "difficulty": c.difficulty,
                "status": c.status,
                "latency_s": round(c.latency_s, 3),
                "metrics": c.metrics,
                "judge_status": c.judge_status,
            }
            for c in all_cases
        ],
        "usage_total": usage_total,
        "cost_estimate_cny": cost_cny,
    }


def format_threshold_warning(value: float | None, threshold: float, direction: str) -> str:
    """格式化指标值，如果低于/高于阈值则添加警告。

    Args:
        value: 指标值（None 表示未测）
        threshold: 阈值
        direction: "ge"（>=为通过）或 "le"（<=为通过）

    Returns:
        格式化后的字符串，如需警告则末尾加 ⚠️
    """
    if value is None:
        return "未测"

    # 判断是否需要警告
    warn = False
    if direction == "ge" and value < threshold:
        warn = True
    elif direction == "le" and value > threshold:
        warn = True

    formatted = f"{value:.4f}" if isinstance(value, float) else str(value)
    return f"{formatted} ⚠️" if warn else formatted


def generate_retrieval_markdown(all_cases: list[CaseMetrics], mode: str) -> str:
    """生成人读版 retrieval.md 报告。

    Args:
        all_cases: 所有用例评估结果
        mode: "live" / "skip-judge" / "replay"

    Returns:
        Markdown 格式的报告
    """
    lines = []
    lines.append("# 检索评估 L1 报告")
    lines.append("")
    lines.append(f"**模式**：{mode}")
    lines.append(f"**生成时间**：{datetime.now(timezone.utc).isoformat()}")
    lines.append("")

    # 聚合指标表
    aggregate = aggregate_metrics(all_cases)
    lines.append("## 聚合指标")
    lines.append("")
    lines.append("| 指标 | 值 | 阈值 |")
    lines.append("|------|-----|------|")

    # nDCG@10
    ndcg_val = format_threshold_warning(aggregate.ndcg_at_10, eval_config.THRESHOLDS.get("l1_ndcg_at_10", 0.75), "ge")
    lines.append(f"| nDCG@10 | {ndcg_val} | ≥ 0.75 |")
    # 中文注释：显式报告有几条用例因为"裁判给出的分数完全相同"被排除在均值之外。
    # 这个数字越大，说明裁判的区分度越差，上面几行裁判类指标的参考价值就越低。
    lines.append(f"| 裁判饱和用例数 | {aggregate.judge_saturated_cases} | 0 |")

    # Precision@10
    p10_val = format_threshold_warning(aggregate.precision_at_10, eval_config.THRESHOLDS.get("l1_precision_at_10", 0.7), "ge")
    lines.append(f"| Precision@10 | {p10_val} | ≥ 0.70 |")

    # Golden 召回
    golden_val = format_threshold_warning(aggregate.golden_recall, eval_config.THRESHOLDS.get("l1_golden_recall", 0.6), "ge")
    lines.append(f"| Golden 召回 | {golden_val} | ≥ 0.60 |")

    # 去重违规
    lines.append(f"| 去重违规数 | {aggregate.duplicate_violations} | 0 |")

    # 字段完整性
    lines.append(f"| Title 完整率 | {aggregate.field_completeness['title']:.2%} | ≥ 90% |")
    lines.append(f"| Year 完整率 | {aggregate.field_completeness['year']:.2%} | ≥ 90% |")
    lines.append(f"| Abstract 完整率 | {aggregate.field_completeness['abstract']:.2%} | ≥ 90% |")

    # 源数和错误
    lines.append(f"| 源数OK率 | {aggregate.sources_ok_rate:.2%} | 100% |")
    lines.append(f"| 无错误率 | {aggregate.errors_empty_rate:.2%} | ≥ 80% |")

    # 重排单调性
    mono_val = format_threshold_warning(aggregate.rerank_monotonicity_gap, eval_config.THRESHOLDS.get("l1_reranking_diff", 0.4), "ge")
    lines.append(f"| 重排单调性 | {mono_val} | ≥ 0.40 |")

    # 多源命中率
    ms_val = format_threshold_warning(aggregate.multi_source_topk_rate, 0.5, "ge")
    lines.append(f"| 多源top-k率 | {ms_val} | ≥ 0.50 |")

    # 扩展成功率
    exp_val = format_threshold_warning(aggregate.expand_success_rate, eval_config.THRESHOLDS.get("l1_expansion_success_rate", 0.9), "ge")
    lines.append(f"| 扩展成功率 | {exp_val} | ≥ 0.90 |")

    # 扩展相关性
    exp_rel_val = format_threshold_warning(aggregate.expand_relevance_avg, eval_config.THRESHOLDS.get("l1_expansion_relevance", 1.2), "ge")
    lines.append(f"| 扩展相关性均分 | {exp_rel_val} | ≥ 1.20 |")

    # 延迟
    p95_val = aggregate.latency_s.get("p95", 0)
    p95_threshold = eval_config.THRESHOLDS.get("l1_search_p95_latency_s", 15)
    p95_str = format_threshold_warning(p95_val, p95_threshold, "le")
    lines.append(f"| P95 延迟(s) | {p95_str} | ≤ {p95_threshold} |")

    lines.append("")
    lines.append("## 用例详情")
    lines.append("")
    lines.append("| case_id | domain | difficulty | status | latency_s | nDCG@10 | 裁判饱和 | P@10 | Golden | Duplicates | Fields | Errors | Expand |")
    lines.append("|---------|--------|-----------|--------|-----------|---------|---------|------|--------|-----------|--------|--------|--------|")

    for case in all_cases:
        status = case.status
        latency = f"{case.latency_s:.2f}" if case.latency_s else "N/A"
        ndcg = case.metrics.get("ndcg_at_10")
        p10 = case.metrics.get("precision_at_10")
        golden = case.metrics.get("golden_recall", 0)
        dups = case.metrics.get("duplicate_violations", 0)
        complete_title = case.metrics.get("field_completeness", {}).get("title", 0)
        errors = "✓" if case.metrics.get("errors_empty") else "✗"
        expand = "✓" if case.metrics.get("expand_success") else "✗" if case.metrics.get("expand_error") else "-"

        ndcg_str = f"{ndcg:.3f}" if ndcg is not None else "未测"
        p10_str = f"{p10:.3f}" if p10 is not None else "未测"

        sat_str = "⚠️饱和" if case.metrics.get("judge_saturated") else "-"
        lines.append(f"| {case.case_id} | {case.domain} | {case.difficulty} | {status} | {latency} | {ndcg_str} | {sat_str} | {p10_str} | {golden:.1%} | {dups} | {complete_title:.1%} | {errors} | {expand} |")

    lines.append("")
    return "\n".join(lines)


# ==================== CLI 与主函数 ====================

async def main():
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="检索评估 L1：对论文检索服务的组件级评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only",
        type=str,
        help="只运行指定用例，逗号分隔（如 r01,r02,r03）",
    )
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="跳过所有 LLM 裁判调用，只计算确定性指标（零成本）",
    )
    parser.add_argument(
        "--replay",
        type=str,
        help="不调用任何 API，重放历史检索结果（零成本）。"
             "参数要传报告目录下的 raw/ 子目录本身，"
             "例如 --replay eval/reports/20260913_120000/raw",
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="配合 --replay 使用：重放历史检索结果的同时重新跑一遍 LLM 裁判。"
             "默认重放不判分（保持零成本）；想验证提示词改动效果时用它，"
             "就不必重新做一次真实检索了。",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        help="报告输出目录，默认为 eval/reports/{YYYYMMDD_HHMMSS}/",
    )

    args = parser.parse_args()

    # 日志配置
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 加载用例
    cases_path = Path(eval_config.EVAL_DIR) / "datasets" / "retrieval_cases.json"
    with open(cases_path, "r", encoding="utf-8") as f:
        cases_data = json.load(f)

    all_cases_list = cases_data.get("cases", [])

    # 过滤用例
    if args.only:
        selected_ids = set(x.strip() for x in args.only.split(","))
        all_cases_list = [c for c in all_cases_list if c["case_id"] in selected_ids]
        logger.info(f"按 --only 过滤后保留 {len(all_cases_list)} 条用例")

    # 装配 judge deps
    judge_deps = None
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    # 中文注释：--rejudge（配合 --replay）时也要装配裁判，否则重放就没法重新判分。
    if not args.skip_judge and (not args.replay or args.rejudge):
        deps_result = await build_judge_deps()
        if deps_result["status"] == "ok":
            judge_deps = deps_result["deps"]
            logger.info("Judge 依赖装配成功")
        else:
            logger.error(f"Judge 依赖装配失败：{deps_result.get('reason')}")
            judge_deps = None

        # 打印预算估算（只在 live 模式下）
        estimated_tokens = len(all_cases_list) * 2 * 4000
        estimated_cost = estimate_cost_cny(
            {"input_tokens": estimated_tokens, "output_tokens": estimated_tokens},
            judge_deps.model_name if judge_deps else None
        )
        print("\n" + "=" * 60)
        print("全量评估预计成本估算")
        print(f"用例数：{len(all_cases_list)}")
        print(f"裁判调用：{len(all_cases_list)} × 2 次（search 结果 + 扩展结果）")
        print(f"预计 token 消耗：约 {estimated_tokens:,} 个")
        print(f"估算成本：约 {estimated_cost:.4f} 元")
        print(f"（价目表见 eval/config.py，仅供参考）")
        print("=" * 60 + "\n")

    # 确定输出目录
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = Path(eval_config.REPORTS_DIR) / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    out_dir.mkdir(parents=True, exist_ok=True)

    # 确定 replay 目录
    replay_raw_dir = None
    if args.replay:
        replay_raw_dir = Path(args.replay)
        if not replay_raw_dir.exists():
            logger.error(f"重放目录不存在：{replay_raw_dir}")
            return

    # 执行用例
    logger.info(f"开始执行 {len(all_cases_list)} 条评估用例...")
    all_results = []

    out_raw_dir = out_dir / "raw"
    out_raw_dir.mkdir(parents=True, exist_ok=True)

    for i, case in enumerate(all_cases_list, start=1):
        logger.info(f"[{i}/{len(all_cases_list)}] 执行用例 {case['case_id']}...")
        case_metric, judge_out, expand_resp = await run_case(
            case,
            judge_deps,
            skip_judge=args.skip_judge,
            replay_raw_dir=replay_raw_dir,
            out_raw_dir=out_raw_dir,
            rejudge=args.rejudge,
        )
        all_results.append(case_metric)

        # 累加 token 用量
        if judge_out and "usage" in judge_out:
            usage_total["input_tokens"] += judge_out["usage"].get("input_tokens", 0)
            usage_total["output_tokens"] += judge_out["usage"].get("output_tokens", 0)

    # 确定执行模式
    if args.replay:
        mode = "replay+rejudge" if args.rejudge else "replay"
    elif args.skip_judge:
        mode = "skip-judge"
    else:
        mode = "live"

    # 生成输出
    retrieval_json = generate_retrieval_json(all_results, mode, usage_total, judge_deps)

    # 落盘 retrieval.json
    retrieval_json_path = out_dir / "retrieval.json"
    with open(retrieval_json_path, "w", encoding="utf-8") as f:
        json.dump(retrieval_json, f, ensure_ascii=False, indent=2)

    logger.info(f"输出已保存到 {retrieval_json_path}")

    # 生成人读版 retrieval.md 报告
    retrieval_md = generate_retrieval_markdown(all_results, mode)
    retrieval_md_path = out_dir / "retrieval.md"
    with open(retrieval_md_path, "w", encoding="utf-8") as f:
        f.write(retrieval_md)

    logger.info(f"报告已保存到 {retrieval_md_path}")
    logger.info("评估完成")


if __name__ == "__main__":
    asyncio.run(main())
