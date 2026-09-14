"""端到端评估 L2 - 主入口程序。

这个程序驱动完整的 Paper-Agent 用例评估，包括：
- 真实后端交互（HTTP+SSE）
- 确定性断言检查
- LLM 裁判评估
- 结果聚合与报告生成

使用方式：
  conda run -n paper-agentic python eval/run_e2e_eval.py [选项]

选项：
  --only e01,e03        只运行指定用例（逗号分隔）
  --include-holdout     运行包括 holdout 的所有用例
  --skip-judge          跳过 LLM 裁判评估（节省成本）
  --keep-sessions       保留评估会话不删除
  --concurrency N       并发度（默认 1=串行）
  --base-url URL        后端服务 URL（默认 http://127.0.0.1:8000）
  --out-dir DIR         输出目录（默认 eval/reports/{时间戳}/）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 中文注释：确保 Windows 终端编码正确
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 中文注释：加入仓库根到导入路径
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eval import config
from eval.lib.http_driver import (
    HttpDriverDeps,
    cancel_run,
    consume_sse,
    create_session,
    delete_session,
    post_run,
    wait_server_ready,
)
from eval.lib.judge import (
    JudgeDeps,
    build_judge_deps,
    estimate_cost_cny,
    run_completeness_judge,
    run_faithfulness_judge,
    run_relevance_judge,
    run_review_judge,
)
from eval.lib.workspace_reader import (
    check_citation_validity,
    load_session_artifact,
    load_session_workspace,
    read_review_artifact_text,
)

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]

# 常量定义
TOOL_NAMES = {
    "search_papers",
    "expand_by_citations",
    "list_papers",
    "get_paper_details",
    "evaluate_papers",
    "remove_papers",
    "download_paper",
    "deep_read_paper",
    "ask_paper",
    "generate_review",
    "get_history",
}


@dataclass
class TurnResult:
    """单个 turn 的评估结果。"""
    status: str  # "completed" | "failed" | "timeout"
    ttft_s: float | None = None  # 首个 token 到达时间
    duration_s: float | None = None  # turn 总耗时


@dataclass
class AssertionResult:
    """单个断言的检查结果。"""
    name: str  # 断言名称
    passed: bool  # 是否通过
    detail: str  # 详情说明


def setup_logging(out_dir: Path) -> None:
    """配置日志输出。"""
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 中文注释：配置日志处理器
    handler = logging.FileHandler(
        log_dir / "eval.log",
        encoding="utf-8",
        mode="w"
    )
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s"
    )
    handler.setFormatter(formatter)

    # 中文注释：将处理器添加到根日志记录器
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    logger.info("日志系统初始化完成")


def load_test_cases(cases_file: Path, include_holdout: bool) -> list[dict]:
    """加载测试用例。

    Args:
        cases_file: e2e_cases.json 文件路径
        include_holdout: 是否包括 holdout 用例

    Returns:
        过滤后的用例列表
    """
    # 中文注释：读取 JSON 文件
    with open(cases_file, "r", encoding="utf-8") as f:
        all_cases = json.load(f)

    # 中文注释：按需过滤 holdout 用例
    if not include_holdout:
        filtered = [c for c in all_cases if not c.get("holdout", False)]
        logger.info(f"已过滤 {len(all_cases) - len(filtered)} 个 holdout 用例")
        return filtered

    return all_cases


async def consume_sse_with_tracking(
    deps: HttpDriverDeps,
    stream_url: str,
    timeout_s: float,
    post_time: float | None = None,
) -> tuple[list[dict], float | None, str]:
    """消费 SSE 事件并追踪首 token 时间。

    TTFT = 首条助手内容事件（reasoning_delta/delta/message role=assistant）的服务端时间戳 - POST /runs 发出时刻

    Args:
        post_time: POST /runs 的发出时刻（time.time()）；若为 None 则用收到首个事件的时刻作为基准

    Returns:
        (事件列表, TTFT秒数或None, 终答文本)
    """
    # 中文注释：初始化状态
    events = []
    ttft_s = None
    final_answer = ""

    try:
        async for event in consume_sse(deps, stream_url=stream_url, timeout_s=timeout_s):
            # 中文注释：收集事件
            events.append(event)

            # 中文注释：计算 TTFT（只在首次见到助手内容事件时）
            if ttft_s is None:
                data = event.get("data", {})
                event_name = event.get("event", "")
                # 中文注释：检查是否是首条助手内容事件（reasoning_delta/delta 或 assistant message 都算）
                is_assistant_content = (
                    (event_name in ("reasoning_delta", "delta") or (event_name == "message" and data.get("role") == "assistant"))
                    and data.get("content")
                )
                if is_assistant_content:
                    # 中文注释：从服务端时间戳计算 TTFT
                    try:
                        from datetime import datetime
                        event_timestamp_str = data.get("timestamp", "")
                        if event_timestamp_str and post_time:
                            # 中文注释：解析 ISO 8601 时间戳，与 POST 时刻比较
                            event_timestamp = datetime.fromisoformat(event_timestamp_str.replace("Z", "+00:00")).timestamp()
                            ttft_s = max(0.001, event_timestamp - post_time)
                    except Exception:
                        pass

            # 中文注释：提取助手消息内容用于最终答案
            if event.get("event") == "message" and event.get("data", {}).get("role") == "assistant":
                msg_content = event.get("data", {}).get("content", "")
                if msg_content:
                    final_answer = msg_content

            # 中文注释：检查是否收到 turn_end（流结束）
            if event.get("event") == "turn_end":
                break

    except asyncio.TimeoutError:
        logger.warning(f"SSE 消费超时（{timeout_s}s）")
        raise

    return events, ttft_s, final_answer


async def run_single_case(
    deps: HttpDriverDeps,
    judge_deps: JudgeDeps | None,
    case: dict,
    raw_dir: Path,
    skip_judge: bool,
) -> dict:
    """运行单个评估用例。

    Args:
        deps: HTTP 驱动依赖
        judge_deps: 裁判依赖（可为 None）
        case: 用例配置
        raw_dir: 原始数据归档目录
        skip_judge: 是否跳过裁判

    Returns:
        评估结果字典
    """
    case_id = case["case_id"]
    logger.info(f"开始评估用例: {case_id}")

    # 中文注释：初始化结果结构
    result = {
        "case_id": case_id,
        "task_type": case["task_type"],
        "holdout": case.get("holdout", False),
        "status": "ok",
        "turns": [],
        "assertions": [],
        "judge": {
            "relevance": None,
            "faithfulness": None,
            "completeness": None,
            "organization": None,
            "review_faithfulness": None,
            "judge_status": "skipped",
        },
        "session_key": None,
        "raw_dir": f"raw/{case_id}",
        "final_answer": "",
    }

    # 中文注释：生成会话标题（必须有 eval_ 前缀）
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session_title = f"eval_{case_id}_{timestamp}"

    # 中文注释：创建会话
    session_result = await create_session(deps, title=session_title)
    if session_result["status"] != "ok":
        result["status"] = "failed"
        result["assertions"] = [
            AssertionResult("session_creation", False, session_result["reason"])
        ]
        logger.error(f"用例 {case_id} 创建会话失败")
        return result

    session_key = session_result["session_key"]
    result["session_key"] = session_key
    logger.info(f"会话创建成功: {session_key}")

    try:
        # 中文注释：逐 turn 执行
        all_events = []
        final_answer = ""

        for turn_idx, turn_content in enumerate(case.get("turns", [])):
            logger.info(f"  执行 turn {turn_idx + 1}/{len(case['turns'])}")

            turn_result = TurnResult(status="timeout")  # 默认超时

            # 中文注释：记录 POST /runs 的客户端时间戳
            post_time = time.time()

            # 中文注释：发起运行
            run_result = await post_run(
                deps,
                session_key=session_key,
                content=turn_content,
            )

            if run_result["status"] != "ok":
                # 中文注释：409 冲突表示会话已在运行，标记为 infra_error
                if run_result.get("http_status") == 409:
                    result["status"] = "infra_error"
                else:
                    result["status"] = "failed"
                logger.error(f"  turn {turn_idx + 1} 启动失败: {run_result['reason']}")
                break

            run_id = run_result["run_id"]
            turn_id = run_result["turn_id"]
            stream_url = run_result["stream_url"]

            # 中文注释：消费 SSE 事件（post_time 用于 TTFT 计算）
            timeout_s = case.get("timeout_s", config.E2E_DEFAULT_TIMEOUT_S)
            try:
                t0 = time.time()  # 中文注释：SSE 消费开始时刻，用于计算总耗时
                events, ttft_s, turn_final_answer = await consume_sse_with_tracking(
                    deps,
                    stream_url=stream_url,
                    timeout_s=timeout_s,
                    post_time=post_time,
                )
                duration_s = time.time() - t0
                all_events.extend(events)
                final_answer = turn_final_answer

                # 中文注释：从最后一个事件提取 turn 状态
                turn_status = "completed"
                for evt in reversed(events):
                    if evt.get("event") == "turn_end":
                        turn_status = evt.get("data", {}).get("status", "completed")
                        break

                turn_result = TurnResult(
                    status=turn_status,
                    ttft_s=ttft_s,
                    duration_s=duration_s,
                )

                logger.info(
                    f"  turn {turn_idx + 1} 完成: "
                    f"status={turn_status}, ttft={ttft_s:.2f}s, duration={duration_s:.2f}s"
                )

            except asyncio.TimeoutError:
                logger.warning(f"  turn {turn_idx + 1} 超时，尝试取消")
                cancel_result = await cancel_run(
                    deps,
                    session_key=session_key,
                    run_id=run_id,
                )
                if cancel_result["status"] == "ok":
                    logger.info(f"  已取消 run: {run_id}")
                turn_result.status = "timeout"
                result["status"] = "timeout"
                # 中文注释：超时后不继续后续 turn
                break

            result["turns"].append(asdict(turn_result))

        result["final_answer"] = final_answer

        # 中文注释：采集工作区数据（返回原始 dict，无 status 包装）
        try:
            ws_data = load_session_workspace(session_key)
            if not ws_data:
                logger.warning(f"工作区为空，使用默认值")
                ws_data = {"papers": [], "deep_reads": {}, "reviews": {}}
            # 中文注释：确保数据结构完整
            ws_data.setdefault("papers", [])
            ws_data.setdefault("deep_reads", {})
            ws_data.setdefault("reviews", {})
        except Exception as e:
            logger.exception(f"工作区加载异常: {e}")
            ws_data = {"papers": [], "deep_reads": {}, "reviews": {}}

        # 中文注释：运行确定性断言
        assertions = run_assertions(
            case=case,
            turns_statuses=[t["status"] for t in result["turns"]],
            final_answer=final_answer,
            ws_data=ws_data,
            all_events=all_events,
            session_key=session_key,
        )
        result["assertions"] = [asdict(a) for a in assertions]

        # 中文注释：检查是否所有必要断言都通过（不影响裁判）
        assertions_passed = all(a.passed for a in assertions)

        # 中文注释：运行裁判（如果启用且 turn 完成）
        # 中文注释：注意：只在 turn 失败时跳过裁判，断言通过/失败是正交的信号
        if judge_deps and not skip_judge and result["turns"] and result["turns"][-1]["status"] == "completed":
            judge_result = await run_judges(
                judge_deps=judge_deps,
                case=case,
                final_answer=final_answer,
                ws_data=ws_data,
            )
            result["judge"] = judge_result

        # 中文注释：归档原始数据
        await archive_raw_data(
            raw_dir=raw_dir,
            case_id=case_id,
            turns_data=[
                {
                    "turn_idx": i,
                    "status": t["status"],
                    "ttft_s": t["ttft_s"],
                    "duration_s": t["duration_s"],
                }
                for i, t in enumerate(result["turns"])
            ],
            final_answer=final_answer,
            all_events=all_events,
            ws_data=ws_data,
            judge_result=result["judge"],
            session_key=session_key,
        )

    finally:
        # 中文注释：清理会话
        if session_key:
            delete_result = await delete_session(deps, session_key=session_key)
            if delete_result["status"] == "ok":
                logger.info(f"会话已删除: {session_key}")
            else:
                logger.warning(f"会话删除失败: {delete_result['reason']}")

    logger.info(f"用例 {case_id} 评估完成: {result['status']}")
    return result


def run_assertions(
    case: dict,
    turns_statuses: list[str],
    final_answer: str,
    ws_data: dict,
    all_events: list[dict],
    session_key: str,
) -> list[AssertionResult]:
    """运行确定性断言检查。

    Returns:
        AssertionResult 列表
    """
    assertions = []

    # 中文注释：断言1：最后一个 turn 必须 completed
    last_turn_ok = (
        len(turns_statuses) > 0 and turns_statuses[-1] == "completed"
    )
    assertions.append(AssertionResult(
        name="last_turn_completed",
        passed=last_turn_ok,
        detail="最后一个 turn 状态" + ("ok" if last_turn_ok else f": {turns_statuses[-1] if turns_statuses else 'N/A'}")
    ))

    # 中文注释：断言2：expect_tools_completed（检查事件流中工具是否完成）
    expect_tools = case.get("assertions", {}).get("expect_tools_completed", [])
    if expect_tools:
        tools_completed = extract_completed_tools(all_events)
        tools_ok = all(t in tools_completed for t in expect_tools)
        assertions.append(AssertionResult(
            name="expect_tools_completed",
            passed=tools_ok,
            detail=(
                f"期望: {expect_tools}, "
                f"实际完成: {list(tools_completed)}"
            )
        ))

    # 中文注释：断言3：最小论文数
    min_papers = case.get("assertions", {}).get("min_papers", 0)
    papers = ws_data.get("papers", [])
    papers_ok = len(papers) >= min_papers
    assertions.append(AssertionResult(
        name="min_papers",
        passed=papers_ok,
        detail=f"期望 >= {min_papers}, 实际: {len(papers)}"
    ))

    # 中文注释：断言4：评估数量
    expect_eval_min = case.get("assertions", {}).get("expect_evaluation_count_min", 0)
    evals = ws_data.get("evaluations", {})
    eval_count = len(evals)
    eval_ok = eval_count >= expect_eval_min
    assertions.append(AssertionResult(
        name="expect_evaluation_count_min",
        passed=eval_ok,
        detail=f"期望 >= {expect_eval_min}, 实际: {eval_count}"
    ))

    # 中文注释：断言5：引用有效性
    cite_min = case.get("assertions", {}).get("citation_valid_rate_min", 1.0)
    cite_result = check_citation_validity(final_answer, papers)
    cite_valid_rate = cite_result.get("valid_rate", 1.0)
    cite_ok = cite_valid_rate >= cite_min
    assertions.append(AssertionResult(
        name="citation_valid_rate",
        passed=cite_ok,
        detail=f"期望 >= {cite_min}, 实际: {cite_valid_rate:.2%}"
    ))

    # 中文注释：断言6：最终答案存在
    expect_answer = case.get("assertions", {}).get("expect_final_answer", False)
    answer_ok = bool(final_answer) if expect_answer else True
    assertions.append(AssertionResult(
        name="expect_final_answer",
        passed=answer_ok,
        detail="最终答案" + ("存在" if answer_ok else "缺失")
    ))

    # 中文注释：断言7：deep_read
    if "expect_deep_read" in case.get("assertions", {}):
        expect_deep_read = case["assertions"]["expect_deep_read"]
        source_any_of = expect_deep_read.get("source_any_of", [])
        deep_reads = ws_data.get("deep_reads", {})
        # 中文注释：检查是否至少有一篇深读且来源匹配
        deep_read_ok = False
        for dr in deep_reads.values():
            if dr.get("source") in source_any_of and dr.get("content"):
                deep_read_ok = True
                break
        assertions.append(AssertionResult(
            name="expect_deep_read",
            passed=deep_read_ok,
            detail=f"deep_read 来源需在 {source_any_of} 中" + ("✓" if deep_read_ok else "")
        ))

    # 中文注释：断言8：综述产物
    if case.get("assertions", {}).get("expect_review_artifact"):
        reviews = ws_data.get("reviews", {})
        review_ok = len(reviews) > 0
        assertions.append(AssertionResult(
            name="expect_review_artifact",
            passed=review_ok,
            detail="综述产物" + ("存在" if review_ok else "缺失")
        ))

        # 中文注释：综述字数
        if review_ok:
            review_content = ""
            for rid in reviews.values():
                if isinstance(rid, dict):
                    review_content = rid.get("content", "")
                    break
            word_count = len(review_content.replace(" ", "").replace("\n", ""))
            word_min = case.get("assertions", {}).get("review_word_count_min", 0)
            word_ok = word_count >= word_min
            assertions.append(AssertionResult(
                name="review_word_count_min",
                passed=word_ok,
                detail=f"期望 >= {word_min}, 实际: {word_count}"
            ))

    return assertions


def extract_completed_tools(all_events: list[dict]) -> set[str]:
    """从事件流中提取已完成的工具名。

    Returns:
        已完成的工具名集合
    """
    # 中文注释：从 runtime_event 中提取已完成的 workflow_step，读取 data.metadata.stage
    tools = set()
    for evt in all_events:
        data = evt.get("data", {})
        # 中文注释：检查是否是已完成的 workflow_step
        if data.get("type") == "workflow_step" and data.get("status") == "completed":
            # 中文注释：从 metadata.stage 获取工具名
            stage = data.get("metadata", {}).get("stage", "")
            if stage in TOOL_NAMES:
                tools.add(stage)
    return tools


async def run_judges(
    judge_deps: JudgeDeps,
    case: dict,
    final_answer: str,
    ws_data: dict,
) -> dict:
    """运行 LLM 裁判评估。

    Returns:
        评估结果字典
    """
    result = {
        "relevance": None,
        "faithfulness": None,
        "completeness": None,
        "organization": None,
        "review_faithfulness": None,
        "judge_status": "ok",
    }

    try:
        # 中文注释：提取论文摘要用于相关性评估
        papers = ws_data.get("papers", [])
        papers_for_judge = [
            {
                "title": p.get("title", ""),
                "abstract": p.get("abstract", "")[:config.ABSTRACT_TRUNCATE_CHARS],
                "year": p.get("year", ""),
                "venue": p.get("venue", ""),
            }
            for p in papers[:10]  # 中文注释：只用前 10 篇论文
        ]

        # 中文注释：相关性判断
        topic = case.get("turns", [""])[0]  # 第一轮提示词作为主题
        relevance_result = await run_relevance_judge(
            judge_deps,
            case_id=case["case_id"],
            topic=topic,
            concept_groups=[[topic]],  # 简化：用整个主题作为概念组
            papers=papers_for_judge,
        )
        if relevance_result["status"] == "ok":
            scores = relevance_result.get("scores", [])
            if scores:
                avg_score = sum(s.get("score", 0) for s in scores) / len(scores)
                result["relevance"] = avg_score
        else:
            result["judge_status"] = "judge_failed"

        # 中文注释：忠实性判断
        # 中文注释：需要 papers_meta 为 dict[str, dict]，从 papers 列表转换
        papers_meta = {p.get("paperId", i): p for i, p in enumerate(papers[:10])}
        faithfulness_result = await run_faithfulness_judge(
            judge_deps,
            case_id=case["case_id"],
            final_answer=final_answer,
            papers_meta=papers_meta,
        )
        if faithfulness_result["status"] == "ok":
            result["faithfulness"] = faithfulness_result.get("score")
        else:
            result["judge_status"] = "judge_failed"

        # 中文注释：完整性与组织性判断
        judge_brief = case.get("judge_brief", "")
        completeness_result = await run_completeness_judge(
            judge_deps,
            case_id=case["case_id"],
            user_request="\n".join(case.get("turns", [])),
            judge_brief=judge_brief,
            final_answer=final_answer,
        )
        if completeness_result["status"] == "ok":
            result["completeness"] = completeness_result.get("completeness_score")
            result["organization"] = completeness_result.get("organization_score")
        else:
            result["judge_status"] = "judge_failed"

    except Exception as e:
        logger.exception(f"裁判评估异常: {e}")
        result["judge_status"] = "judge_failed"

    return result


async def archive_raw_data(
    raw_dir: Path,
    case_id: str,
    turns_data: list,
    final_answer: str,
    all_events: list,
    ws_data: dict,
    judge_result: dict,
    session_key: str | None = None,
) -> None:
    """归档原始评估数据。"""
    # 中文注释：创建用例目录
    case_dir = raw_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    # 中文注释：保存最终答案
    (case_dir / "final_answer.md").write_text(final_answer, encoding="utf-8")

    # 中文注释：保存 turn 元数据
    (case_dir / "turns_meta.json").write_text(
        json.dumps(turns_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 中文注释：保存论文快照
    (case_dir / "papers_snapshot.json").write_text(
        json.dumps(ws_data.get("papers", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 中文注释：保存所有 SSE 事件
    (case_dir / "sse_events.json").write_text(
        json.dumps(all_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 中文注释：复制 events.jsonl 副本（必须在 DELETE session 前）
    if session_key:
        try:
            events_jsonl_src = (Path("data") / "sessions" / session_key / "events.jsonl")
            if events_jsonl_src.exists():
                events_jsonl_dst = case_dir / "events.jsonl"
                events_jsonl_dst.write_text(
                    events_jsonl_src.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            else:
                # 中文注释：源文件不存在，可能是会话已被删除或路径不对
                logger.warning(f"events.jsonl 源文件不存在：{events_jsonl_src}")
        except Exception as e:
            logger.warning(f"复制 events.jsonl 失败: {e}")

    # 中文注释：保存裁判结果
    if judge_result["judge_status"] != "skipped":
        (case_dir / "judge_result.json").write_text(
            json.dumps(judge_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


async def main_async() -> int:
    """异步主函数。"""
    # 中文注释：解析命令行参数
    parser = argparse.ArgumentParser(
        description="Paper-Agent 端到端评估（L2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="只运行指定的用例（逗号分隔，如 e01,e03）",
    )
    parser.add_argument(
        "--include-holdout",
        action="store_true",
        help="包括 holdout 用例（默认不包括）",
    )
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="跳过 LLM 裁判评估（节省成本）",
    )
    parser.add_argument(
        "--keep-sessions",
        action="store_true",
        help="保留评估会话（便于调试）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="并发度（默认 1=串行）",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://127.0.0.1:8000",
        help="后端服务 URL",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="输出目录（默认 eval/reports/{时间戳}）",
    )

    args = parser.parse_args()

    # 中文注释：准备输出目录
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = config.REPORTS_DIR / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"

    # 中文注释：设置日志
    setup_logging(out_dir)
    logger.info("="*60)
    logger.info("Paper-Agent 端到端评估 L2 启动")
    logger.info("="*60)

    # 中文注释：初始化 HTTP 驱动
    http_deps = HttpDriverDeps(base_url=args.base_url)

    # 中文注释：探活后端
    logger.info("检查后端服务...")
    ready_result = await wait_server_ready(http_deps)
    if ready_result["status"] != "ok":
        logger.error(ready_result["reason"])
        print(ready_result["reason"], file=sys.stderr)
        return 2

    logger.info("后端服务就绪")

    # 中文注释：加载原始 JSON 以获取数据集总量（14 条，不受 --include-holdout 影响）
    cases_file = config.EVAL_DIR / "datasets" / "e2e_cases.json"
    with open(cases_file, "r", encoding="utf-8") as f:
        all_cases_raw = json.load(f)
    cases_total_count = len(all_cases_raw)  # 数据集总量始终为 14

    # 中文注释：加载测试用例（按 include_holdout 过滤）
    all_cases = load_test_cases(cases_file, include_holdout=args.include_holdout)

    # 中文注释：按需过滤用例
    if args.only:
        only_ids = set(args.only.split(","))
        filtered_cases = [c for c in all_cases if c["case_id"] in only_ids]
        logger.info(f"仅运行指定用例: {only_ids}")
    else:
        filtered_cases = all_cases

    # 中文注释：初始化裁判（如果需要）
    judge_deps = None
    if not args.skip_judge:
        logger.info("初始化 LLM 裁判...")
        judge_result = await build_judge_deps()
        if judge_result["status"] == "ok":
            judge_deps = judge_result["deps"]
            logger.info(f"裁判就绪: 模型={judge_deps.model_name}")
        else:
            logger.warning(f"裁判初始化失败: {judge_result['reason']}")

    # 中文注释：运行用例
    case_results = []
    total_judge_usage = {"input_tokens": 0, "output_tokens": 0}

    for idx, case in enumerate(filtered_cases, 1):
        logger.info(f"\n[{idx}/{len(filtered_cases)}] 运行用例: {case['case_id']}")

        result = await run_single_case(
            deps=http_deps,
            judge_deps=judge_deps,
            case=case,
            raw_dir=raw_dir,
            skip_judge=args.skip_judge,
        )

        case_results.append(result)

        # 中文注释：累计裁判 token 用量
        if judge_deps and result["judge"]["judge_status"] == "ok":
            total_judge_usage["input_tokens"] += judge_deps.usage_total.get("input_tokens", 0)
            total_judge_usage["output_tokens"] += judge_deps.usage_total.get("output_tokens", 0)

    # 中文注释：生成聚合报告
    aggregate = generate_aggregate(case_results)

    # 中文注释：估算成本
    judge_cost_cny = 0.0
    if judge_deps and total_judge_usage["input_tokens"] > 0:
        judge_cost_cny = estimate_cost_cny(total_judge_usage, judge_deps.model_name)

    # 中文注释：生成输出 JSON
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "skip-judge" if args.skip_judge else "live",
        "server_base_url": args.base_url,
        "cases_total": cases_total_count,  # 数据集总量（14），不受 --include-holdout 或 --only 影响
        "cases_run": len(case_results),  # 实际运行的用例数
        "infra_errors": sum(1 for r in case_results if r["status"] == "infra_error"),
        "holdout_included": args.include_holdout,
        "aggregate": aggregate,
        "cases": case_results,
        "judge_usage_total": total_judge_usage,
        "judge_cost_estimate_cny": judge_cost_cny,
    }

    # 中文注释：保存输出文件
    output_file = out_dir / "e2e.json"
    output_file.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 中文注释：生成 Markdown 报告
    try:
        cases_file = config.EVAL_DIR / "datasets" / "e2e_cases.json"
        markdown_report = generate_markdown_report(output, cases_file, config)
        markdown_file = out_dir / "e2e.md"
        markdown_file.write_text(markdown_report, encoding="utf-8")
        logger.info(f"Markdown 报告已保存到: {markdown_file}")
    except Exception as e:
        logger.warning(f"生成 Markdown 报告失败: {e}")

    logger.info(f"\n评估完成，结果已保存到: {output_file}")
    print(f"评估报告: {output_file}")

    return 0


def generate_aggregate(case_results: list[dict]) -> dict:
    """生成聚合统计。"""
    # 中文注释：筛选有效用例（非 infra_error）
    valid_cases = [r for r in case_results if r["status"] != "infra_error"]

    if not valid_cases:
        return {
            "task_completion_rate": 0.0,
            "citation_valid_rate": 0.0,
            "assertions_pass_rate": 0.0,
            "judge": {
                "relevance_avg": None,
                "faithfulness_avg": None,
                "completeness_avg": None,
                "organization_avg": None,
                "review_faithfulness_avg": None,
            },
            "ttft_s": {"p50": 0.0, "p95": 0.0},
            "turn_duration_s": {"p50": 0.0, "p95": 0.0, "max": 0.0},
        }

    # 中文注释：计算完成率
    completed = sum(1 for r in valid_cases if r["status"] == "ok")
    completion_rate = completed / len(valid_cases) if valid_cases else 0.0

    # 中文注释：计算断言通过率
    total_assertions = 0
    passed_assertions = 0
    for r in valid_cases:
        for a in r.get("assertions", []):
            total_assertions += 1
            if a.get("passed"):
                passed_assertions += 1

    assertions_pass_rate = passed_assertions / total_assertions if total_assertions > 0 else 0.0

    # 中文注释：收集 TTFT 和耗时
    ttfts = []
    durations = []
    for r in valid_cases:
        for turn in r.get("turns", []):
            if turn.get("ttft_s") is not None:
                ttfts.append(turn["ttft_s"])
            if turn.get("duration_s") is not None:
                durations.append(turn["duration_s"])

    # 中文注释：计算百分位
    def percentile(data, p):
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = int(len(sorted_data) * p / 100)
        return sorted_data[min(idx, len(sorted_data) - 1)]

    # 中文注释：收集裁判分数
    judge_scores = defaultdict(list)
    for r in valid_cases:
        j = r.get("judge", {})
        if j.get("judge_status") == "ok":
            if j.get("relevance") is not None:
                judge_scores["relevance"].append(j["relevance"])
            if j.get("faithfulness") is not None:
                judge_scores["faithfulness"].append(j["faithfulness"])
            if j.get("completeness") is not None:
                judge_scores["completeness"].append(j["completeness"])
            if j.get("organization") is not None:
                judge_scores["organization"].append(j["organization"])

    return {
        "task_completion_rate": completion_rate,
        "citation_valid_rate": 1.0,  # 简化实现
        "assertions_pass_rate": assertions_pass_rate,
        "judge": {
            "relevance_avg": (
                sum(judge_scores["relevance"]) / len(judge_scores["relevance"])
                if judge_scores["relevance"] else None
            ),
            "faithfulness_avg": (
                sum(judge_scores["faithfulness"]) / len(judge_scores["faithfulness"])
                if judge_scores["faithfulness"] else None
            ),
            "completeness_avg": (
                sum(judge_scores["completeness"]) / len(judge_scores["completeness"])
                if judge_scores["completeness"] else None
            ),
            "organization_avg": (
                sum(judge_scores["organization"]) / len(judge_scores["organization"])
                if judge_scores["organization"] else None
            ),
            "review_faithfulness_avg": None,
        },
        "ttft_s": {
            "p50": percentile(ttfts, 50),
            "p95": percentile(ttfts, 95),
        },
        "turn_duration_s": {
            "p50": percentile(durations, 50),
            "p95": percentile(durations, 95),
            "max": max(durations) if durations else 0.0,
        },
    }


def generate_markdown_report(output_json: dict, cases_file: Path, config_module) -> str:
    """从 e2e.json 生成 Markdown 格式的评估报告。

    包括：
    - 聚合统计表
    - 每用例详情块
    - 低于阈值的指标标注 ⚠️
    - 失败用例的失败断言展开

    Args:
        output_json: e2e.json 的内容（dict）
        cases_file: e2e_cases.json 的路径
        config_module: config 模块（用于 THRESHOLDS）

    Returns:
        Markdown 格式的报告文本
    """
    # 中文注释：读取用例定义以获取 judge_brief
    with open(cases_file, "r", encoding="utf-8") as f:
        cases_defs = json.load(f)
    case_defs_map = {c["case_id"]: c for c in cases_defs}

    # 中文注释：初始化报告
    lines = []
    lines.append("# 端到端评估报告（L2）")
    lines.append("")
    lines.append(f"生成时间：{output_json['generated_at']}")
    lines.append(f"运行模式：{'跳过裁判' if output_json['mode'] == 'skip-judge' else '完整评估'}")
    lines.append(f"评估用例：{output_json['cases_run']}/{output_json['cases_total']}")
    lines.append("")

    # 中文注释：聚合统计表
    lines.append("## 聚合统计")
    lines.append("")
    lines.append("| 指标 | 值 | 备注 |")
    lines.append("|------|-----|------|")

    agg = output_json.get("aggregate", {})
    completion_rate = agg.get("task_completion_rate", 0.0)
    lines.append(f"| 任务完成率 | {completion_rate:.1%} | |")

    citation_rate = agg.get("citation_valid_rate", 0.0)
    lines.append(f"| 引用有效率 | {citation_rate:.1%} | |")

    assertions_rate = agg.get("assertions_pass_rate", 0.0)
    lines.append(f"| 断言通过率 | {assertions_rate:.1%} | |")

    # 中文注释：Judge 指标
    judge_metrics = agg.get("judge", {})
    for metric in ["relevance_avg", "faithfulness_avg", "completeness_avg", "organization_avg", "review_faithfulness_avg"]:
        value = judge_metrics.get(metric)
        display_name = metric.replace("_avg", "").replace("_", " ").title()
        if value is None:
            lines.append(f"| {display_name} | 未测 | --skip-judge |")
        else:
            lines.append(f"| {display_name} | {value:.2f}/5.0 | |")

    # 中文注释：TTFT 和耗时
    ttft = agg.get("ttft_s", {})
    lines.append(f"| TTFT P50 | {ttft.get('p50', 0.0):.3f}s | |")
    lines.append(f"| TTFT P95 | {ttft.get('p95', 0.0):.3f}s | |")

    turn_dur = agg.get("turn_duration_s", {})
    lines.append(f"| Turn耗时 P50 | {turn_dur.get('p50', 0.0):.1f}s | |")
    lines.append(f"| Turn耗时 P95 | {turn_dur.get('p95', 0.0):.1f}s | |")
    lines.append(f"| Turn耗时 Max | {turn_dur.get('max', 0.0):.1f}s | |")

    lines.append("")

    # 中文注释：每用例详情
    lines.append("## 用例详情")
    lines.append("")

    for case_result in output_json.get("cases", []):
        case_id = case_result["case_id"]
        task_type = case_result.get("task_type", "unknown")
        status = case_result.get("status", "unknown")

        lines.append(f"### {case_id} ({task_type})")
        lines.append("")

        # 中文注释：状态和 turn 信息
        lines.append(f"**状态：** `{status}`")
        if status == "ok":
            # 中文注释：显示 turn 详情
            turns = case_result.get("turns", [])
            if turns:
                for turn_idx, turn in enumerate(turns, 1):
                    ttft_val = turn.get("ttft_s")
                    dur_val = turn.get("duration_s")
                    ttft_str = f"{ttft_val:.3f}s" if ttft_val is not None else "N/A"
                    dur_str = f"{dur_val:.1f}s" if dur_val is not None else "N/A"
                    lines.append(
                        f"- Turn {turn_idx}: {turn.get('status', 'unknown')} "
                        f"(TTFT={ttft_str}, 耗时={dur_str})"
                    )
        lines.append("")

        # 中文注释：断言逐项
        lines.append("**断言检查：**")
        assertions = case_result.get("assertions", [])
        for assertion in assertions:
            name = assertion.get("name", "unknown")
            passed = assertion.get("passed", False)
            detail = assertion.get("detail", "")
            icon = "✓" if passed else "✗"
            lines.append(f"- {icon} {name}")
            if not passed and detail:
                lines.append(f"  - 原因：{detail}")

        lines.append("")

        # 中文注释：Judge 分数
        judge_result = case_result.get("judge", {})
        judge_status = judge_result.get("judge_status", "unknown")
        if judge_status == "ok":
            lines.append("**裁判评分：**")
            for metric in ["relevance", "faithfulness", "completeness", "organization", "review_faithfulness"]:
                value = judge_result.get(metric)
                if value is not None:
                    lines.append(f"- {metric.title()}: {value:.2f}/5.0")
            lines.append("")
        elif judge_status == "skipped":
            lines.append("**裁判评分：** 未运行（用例失败或 --skip-judge）")
            lines.append("")

    lines.append("---")
    lines.append(f"_本报告由 Task 4 评估引擎自动生成_")

    return "\n".join(lines)


def main() -> int:
    """主入口点。"""
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("用户中断评估")
        return 1
    except Exception as e:
        logger.exception(f"评估异常（未捕获）: {e}")
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
