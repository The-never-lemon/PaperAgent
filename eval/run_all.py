"""聚合入口：run_all.py
依次运行三层评估（L1 检索、L2 端到端、L3 可靠性），聚合总分，生成趋势对比报告。

使用方式：
  python eval/run_all.py
  python eval/run_all.py --skip-l1 --skip-l2
  python eval/run_all.py --skip-judge --only-l1 r01
  python eval/run_all.py --calibration
"""

import sys
import json
import argparse
import subprocess
import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# 确保输出 UTF-8（Windows 环境防护）
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 加入仓库根到 sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eval import config as eval_config
# 将 config.THRESHOLDS 里的指标映射到三层 runner 的 JSON 字段
# 格式：指标名 -> (层, 方向, JSON 路径, 解释)
# 方向说明：
#   "higher_better": 越高越好，score = min(实际/阈值, 1) × 100
#   "lower_better": 越低越好，score = min(阈值/max(实际, 1e-9), 1) × 100
#   "judge_1_5": 裁判 1-5 分制，score = 实际/5 × 100
#   "violation_count": 违规计数，0->100分，>0->0分
#   "no_scoring": 不参与计分，只呈现

METRIC_MAPPING = {
    # ===== L3 可靠性指标 =====
    "turn_completed_rate": ("L3", "higher_better", "turn_status.completed_rate", "Turn 完成率"),
    "tool_success_rate": ("L3", "higher_better", "SPECIAL:tool_stats", "工具成功率（由 extract_tool_success_rate 聚合计算）"),
    "llm_retry_rate": ("L3", "lower_better", "llm_stats.retry_rate", "LLM 重试率"),
    "download_success_rate": ("L3", "higher_better", "workspace_stats.download_success_rate", "论文下载成功率"),
    "abstract_fallback_rate": ("L3", "lower_better", "workspace_stats.abstract_fallback_rate", "Abstract Fallback 率"),
    "search_relaxed_rate": ("L3", "lower_better", "workspace_stats.relaxed_search_rate", "检索放宽率"),
    "evaluation_coverage": ("L3", "higher_better", "workspace_stats.evaluation_coverage", "评估覆盖率"),
    "cancel_rate": ("L3", "lower_better", "user_signals.cancel_rate", "取消率"),
    "retry_after_fail_rate": ("L3", "lower_better", "user_signals.retry_after_fail_rate", "失败后重试率"),

    # ===== L1 检索评估指标 =====
    "l1_ndcg_at_10": ("L1", "higher_better", "aggregate.ndcg_at_10", "nDCG@10"),
    "l1_precision_at_10": ("L1", "higher_better", "aggregate.precision_at_10", "Precision@10"),
    "l1_golden_recall": ("L1", "higher_better", "aggregate.golden_recall", "Golden Recall"),
    "l1_abstract_completeness": ("L1", "higher_better", "aggregate.field_completeness.abstract", "Abstract 完整性"),
    "l1_errors_empty_rate": ("L1", "higher_better", "aggregate.errors_empty_rate", "Errors Empty 率"),
    "l1_reranking_diff": ("L1", "higher_better", "aggregate.rerank_monotonicity_gap", "重排单调性 gap"),
    "l1_expansion_success_rate": ("L1", "higher_better", "aggregate.expand_success_rate", "扩展成功率"),
    "l1_expansion_relevance": ("L1", "higher_better", "aggregate.expand_relevance_avg", "扩展相关性均分"),

    # ===== L2 端到端评估指标 =====
    "l2_completion_rate": ("L2", "higher_better", "aggregate.task_completion_rate", "任务完成率"),
    "l2_citation_validity_rate": ("L2", "higher_better", "aggregate.citation_valid_rate", "引文有效率"),
    "l2_assertions_pass_rate": ("L2", "higher_better", "aggregate.assertions_pass_rate", "断言通过率"),
    "l2_relevance_judge_score": ("L2", "judge_1_5", "aggregate.judge.relevance_avg", "相关性裁判评分"),
    "l2_factuality_judge_score": ("L2", "judge_1_5", "aggregate.judge.faithfulness_avg", "忠实性裁判评分"),
    "l2_completeness": ("L2", "judge_1_5", "aggregate.judge.completeness_avg", "完整性裁判评分"),
    "l2_organization": ("L2", "judge_1_5", "aggregate.judge.organization_avg", "组织性裁判评分"),
}


def calculate_model_config_digest() -> str:
    """
    计算 config/model.json 的 SHA256 摘要（绝不复制内容）。
    """
    model_config_path = REPO_ROOT / "config" / "model.json"
    if not model_config_path.exists():
        return "N/A"

    try:
        with open(model_config_path, "rb") as f:
            content = f.read()
            return hashlib.sha256(content).hexdigest()
    except Exception:
        return "N/A"


def get_git_commit() -> Optional[str]:
    """
    获取当前 git commit SHA（失败置 null）。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def estimate_budget(skip_judge: bool, l1_cases: int = 24, l2_cases: int = 10) -> str:
    """
    生成预算估算字符串。

    Args:
        skip_judge: 是否跳过LLM裁判
        l1_cases: L1用例数（默认24）
        l2_cases: L2用例数（默认10）

    Returns:
        预算描述
    """
    if skip_judge:
        return "预算估算：零裁判成本（--skip-judge 模式）"

    # 按 config.PRICE_TABLE_CNY_PER_M_TOKENS 估算实际成本
    # 保守估计：每个L1用例平均4k输入+2k输出token，L2每个7k输入+3k输出token
    # 这与Task3的预算打印一致

    price_table = eval_config.PRICE_TABLE_CNY_PER_M_TOKENS.get("deepseek-v4-flash", {})
    if not price_table:
        return f"预算估算：无价目表数据"

    input_price = price_table.get("input", 2.0) / 1_000_000  # 转换为元/token
    output_price = price_table.get("output", 8.0) / 1_000_000

    # L1: 24个用例，每个平均4k input + 2k output
    l1_cost = (l1_cases * 4000 * input_price) + (l1_cases * 2000 * output_price)

    # L2: 10个用例，每个平均7k input + 3k output
    l2_cost = (l2_cases * 7000 * input_price) + (l2_cases * 3000 * output_price)

    total_cost = l1_cost + l2_cost

    return f"预算估算：L1(¥{l1_cost:.2f}) + L2(¥{l2_cost:.2f}) = 共¥{total_cost:.2f}（占位估算，请按官方价目核实）"


def run_layer_subprocess(
    layer_name: str,
    runner_script: str,
    args: argparse.Namespace,
    out_dir: Path,
) -> tuple[str, Optional[dict], bool]:
    """
    用 subprocess 运行一层评估，进程崩溃隔离。

    返回：(mode_str, json_data, success)
      - mode_str: "live" / "skip-judge" / "replay" / "skipped" / "failed"
      - json_data: 该层的 JSON 对象，或 None（失败/跳过）
      - success: 是否成功
    """
    # 决定是否跳过该层
    if layer_name == "L1" and args.skip_l1:
        return ("skipped", None, True)
    if layer_name == "L2" and args.skip_l2:
        return ("skipped", None, True)
    if layer_name == "L3" and args.skip_l3:
        return ("skipped", None, True)

    # 构建命令行
    cmd = [
        sys.executable,
        str(REPO_ROOT / "eval" / runner_script),
        "--out-dir", str(out_dir),
    ]

    # 透传参数（L1 和 L2）
    if layer_name == "L1":
        if args.skip_judge:
            cmd.append("--skip-judge")
        if args.only_l1:
            cmd.extend(["--only", args.only_l1])
        if args.replay_l1:
            cmd.extend(["--replay", args.replay_l1])
    elif layer_name == "L2":
        if args.skip_judge:
            cmd.append("--skip-judge")
        if args.only_l2:
            cmd.extend(["--only", args.only_l2])
        if args.include_holdout:
            cmd.append("--include-holdout")
        if args.base_url:
            cmd.extend(["--base-url", args.base_url])

    # L3 没有额外参数

    print(f"\n[{layer_name}] 启动子进程：{' '.join(cmd[1:])}")

    # 准备环境变量，确保 subprocess 能找到 src 模块
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)

    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=False,  # 透传 stdout/stderr
            timeout=3600,  # 1 小时超时
        )

        if result.returncode != 0:
            print(f"[{layer_name}] ✗ 进程退出码：{result.returncode}")
            return ("failed", None, False)

        # 尝试读回该层的 JSON
        json_file_map = {
            "L1": "retrieval.json",
            "L2": "e2e.json",
            "L3": "reliability.json",
        }
        json_path = out_dir / json_file_map[layer_name]

        if not json_path.exists():
            print(f"[{layer_name}] ✗ JSON 文件未生成：{json_path}")
            return ("failed", None, False)

        with open(json_path, "r", encoding="utf-8") as f:
            layer_json = json.load(f)

        # 决定 mode 字符串
        if layer_name == "L1":
            mode_str = layer_json.get("mode", "unknown")
        elif layer_name == "L2":
            mode_str = layer_json.get("mode", "unknown")
        else:  # L3
            mode_str = "run"  # L3 没有 mode 字段

        print(f"[{layer_name}] ✓ JSON 读回成功 (mode={mode_str})")
        return (mode_str, layer_json, True)

    except subprocess.TimeoutExpired:
        print(f"[{layer_name}] ✗ 超时（>1小时）")
        return ("failed", None, False)
    except Exception as e:
        print(f"[{layer_name}] ✗ 异常：{e}")
        return ("failed", None, False)


def get_value_from_json(obj: Any, path: str) -> Any:
    """
    从嵌套字典中按路径取值。

    路径格式：
      "a.b.c" -> obj["a"]["b"]["c"]
      "a.b.c:invert" -> 1 - obj["a"]["b"]["c"]（用于反向指标）

    注：通配符 * 不在此函数处理，应使用专用函数（如 extract_tool_success_rate）
    """
    if ":invert" in path:
        path_clean = path.replace(":invert", "")
        val = get_value_from_json(obj, path_clean)
        return 1 - val if val is not None else None

    parts = path.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None

    return current


def extract_tool_success_rate(reliability_json: Optional[dict]) -> Optional[float]:
    """
    从 reliability.json 的 tool_stats.by_stage 聚合计算工具总体成功率。

    计算逻辑：失败率 = sum(各stage failed) / sum(各stage calls)
            成功率 = 1 - 失败率

    如果没有任何工具调用（calls==0），返回 None（"未测"）。
    这对应 config.THRESHOLDS 中 "tool_success_rate": 0.95 的阈值定义。

    Args:
        reliability_json: reliability.json 的完整内容

    Returns:
        工具成功率（0~1），或 None（无数据）
    """
    if reliability_json is None:
        return None

    tool_stats = reliability_json.get("tool_stats", {})
    by_stage = tool_stats.get("by_stage", {})

    if not by_stage:
        return None

    total_calls = 0
    total_failed = 0

    for stage_name, stage_data in by_stage.items():
        if isinstance(stage_data, dict):
            total_calls += stage_data.get("calls", 0)
            total_failed += stage_data.get("failed", 0)

    if total_calls == 0:
        return None

    failure_rate = total_failed / total_calls
    success_rate = 1 - failure_rate

    return success_rate


def calculate_score(value: Any, metric_key: str, thresholds: Dict) -> tuple[Optional[float], str]:
    """
    根据指标类型计算分数（0~100）。

    支持的方向（从 METRIC_MAPPING 获取）：
      - higher_better: score = min(value/threshold, 1) × 100
      - lower_better: score = min(threshold/max(value, 1e-9), 1) × 100
      - judge_1_5: score = value/5 × 100（1-5分制）
      - violation_count: score = 100 if value==0 else 0

    返回：(score, reason)
      - score: 分数值或 None（未测）
      - reason: 原因描述（如"未测"、"越高越好"等）
    """
    if value is None:
        return (None, "未测")

    threshold = thresholds.get(metric_key)
    direction, json_path = None, None

    # 从 METRIC_MAPPING 查方向
    if metric_key in METRIC_MAPPING:
        _, direction, json_path, _ = METRIC_MAPPING[metric_key]

    if direction == "higher_better":
        # 越高越好：score = min(value/threshold, 1) * 100
        if threshold is None:
            return (None, "无阈值")
        score = min(value / threshold, 1.0) * 100
        return (score, f"越高越好（实际{value:.3f} / 阈值{threshold}）")

    elif direction == "lower_better":
        # 越低越好：score = min(threshold/max(value, 1e-9), 1) * 100
        if threshold is None:
            return (None, "无阈值")
        score = min(threshold / max(value, 1e-9), 1.0) * 100
        return (score, f"越低越好（阈值{threshold} / 实际{value:.3f}）")

    elif direction == "judge_1_5":
        # 裁判 1-5 分制：score = value/5 * 100
        score = (value / 5.0) * 100
        return (score, f"裁判评分（{value:.1f}/5）")

    elif direction == "violation_count":
        # 违规计数：0 -> 100, >0 -> 0
        score = 100 if value == 0 else 0
        return (score, f"违规计数（{value} 个）")

    else:
        return (None, f"未知方向：{direction}")


def aggregate_layer_score(
    layer_json: Optional[dict],
    layer_name: str,
    thresholds: Dict,
) -> tuple[Optional[float], int, int, Dict[str, Any]]:
    """
    聚合一层的分数（该层所有参与计分的指标的算术平均）。

    返回：(layer_score, metrics_scored, metrics_missing, detailed_scores)
      - layer_score: 该层分数（0~100），或 None（无有效指标）
      - metrics_scored: 参与计分的指标数
      - metrics_missing: 未测的指标数
      - detailed_scores: {metric_key: {value, score, reason}}
    """
    if layer_json is None:
        return (None, 0, 0, {})

    detailed = {}
    scores = []
    missing_count = 0

    # 按 METRIC_MAPPING 迭代该层的指标
    for metric_key, (layer, direction, json_path, explanation) in METRIC_MAPPING.items():
        if layer != layer_name:
            continue

        # 特殊处理：tool_success_rate 由 extract_tool_success_rate 计算
        if json_path == "SPECIAL:tool_stats":
            value = extract_tool_success_rate(layer_json)
        else:
            # 从 JSON 中取值（常规路径）
            value = get_value_from_json(layer_json, json_path)

        # 计算分数
        score, reason = calculate_score(value, metric_key, thresholds)

        detailed[metric_key] = {
            "value": value,
            "score": score,
            "reason": reason,
            "explanation": explanation,
        }

        if score is not None:
            scores.append(score)
        else:
            missing_count += 1

    # 计算层平均分
    layer_score = sum(scores) / len(scores) if scores else None
    metrics_scored = len(scores)

    return (layer_score, metrics_scored, missing_count, detailed)


def find_previous_run(current_timestamp: str) -> Optional[dict]:
    """
    扫描 eval/reports/ 找到最近的历史运行（非当前）的 report.json。

    返回：前一次运行的 report.json 内容，或 None（无历史）
    """
    reports_dir = eval_config.REPORTS_DIR
    if not reports_dir.exists():
        return None

    # 扫描所有时间戳目录
    candidates = []
    for item in reports_dir.iterdir():
        if item.is_dir() and item.name != current_timestamp:
            report_json_path = item / "report.json"
            if report_json_path.exists():
                try:
                    with open(report_json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        candidates.append((item.name, data))
                except Exception:
                    pass

    if not candidates:
        return None

    # 按 started_at 降序排序，取最新的
    candidates.sort(
        key=lambda x: x[1].get("started_at", ""),
        reverse=True
    )

    return candidates[0][1] if candidates else None


def calculate_trend(current: float, previous: Optional[float]) -> str:
    """
    格式化趋势符号。

    返回：
      "↑ +2.3" / "↓ -1.1" / "→ 0.0"（变化 <0.05 时为 →）
    """
    if previous is None or current is None:
        return "→ N/A"

    delta = current - previous
    if abs(delta) < 0.05:
        return f"→ {delta:+.1f}"
    elif delta > 0:
        return f"↑ {delta:+.1f}"
    else:
        return f"↓ {delta:+.1f}"


def render_report_json(
    run_id: str,
    started_at: str,
    finished_at: str,
    git_commit: Optional[str],
    model_config_digest: str,
    mode_dict: Dict[str, str],
    layer_results: Dict[str, Any],
    layer_scores: Dict[str, Optional[float]],
    weights_normalized: Dict[str, float],
    overall_score: float,
    total_judge_cost: float,
    previous_run: Optional[dict],
    calibration_result: Optional[dict] = None,
) -> dict:
    """
    构造 report.json（schema 冻结）。
    """
    # 计算趋势
    trend_data = {
        "previous_run_id": previous_run["run_id"] if previous_run else None,
        "overall_delta": None,
        "layer_deltas": {"L1": None, "L2": None, "L3": None},
        "completion_rate_delta": None,
        "cost_delta": None,
    }

    if previous_run:
        prev_overall = previous_run["summary"]["overall_score"]
        if overall_score is not None and prev_overall is not None:
            trend_data["overall_delta"] = overall_score - prev_overall

        for layer in ["L1", "L2", "L3"]:
            prev_layer_score = previous_run["summary"]["layers"].get(layer, {}).get("score")
            current_layer_score = layer_scores.get(layer)
            if prev_layer_score is not None and current_layer_score is not None:
                trend_data["layer_deltas"][layer] = current_layer_score - prev_layer_score

        prev_cost = previous_run["summary"]["costs"].get("judge_cost_estimate_cny", 0)
        trend_data["cost_delta"] = total_judge_cost - prev_cost

    # 构造层信息
    layers_dict = {}
    for layer in ["L1", "L2", "L3"]:
        score = layer_scores.get(layer)
        layers_dict[layer] = {
            "score": score,
            "metrics_scored": layer_results[layer]["metrics_scored"] if layer in layer_results else 0,
            "metrics_missing": layer_results[layer]["metrics_missing"] if layer in layer_results else 0,
        }

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "git_commit": git_commit,
        "model_config_digest": model_config_digest,
        "mode": mode_dict,
        "summary": {
            "overall_score": overall_score,
            "weights_normalized": weights_normalized,
            "layers": layers_dict,
            "costs": {
                "judge_cost_estimate_cny": total_judge_cost,
                "note": "估算（价目表见 config.py）",
            },
        },
        "trend": trend_data,
        "calibration": calibration_result,
        "layers_raw": {
            "retrieval": layer_results.get("L1", {}).get("json"),
            "e2e": layer_results.get("L2", {}).get("json"),
            "reliability": layer_results.get("L3", {}).get("json"),
        },
    }

    return report


def render_report_markdown(
    run_id: str,
    started_at: str,
    git_commit: Optional[str],
    model_config_digest: str,
    mode_dict: Dict[str, str],
    layer_scores: Dict[str, Optional[float]],
    weights_normalized: Dict[str, float],
    overall_score: float,
    total_judge_cost: float,
    previous_run: Optional[dict],
    layer_results: Dict[str, Any],
    calibration_result: Optional[dict] = None,
) -> str:
    """
    生成人读版 report.md。
    """
    lines = []

    # ===== 裁判可信度警告（85%线） =====
    if calibration_result and calibration_result.get("status") == "ok":
        if calibration_result.get("labeled", 0) > 0:
            agreement = calibration_result.get("agreement_within_1", 0)
            if agreement < 0.85:
                lines.append("⚠️ **警告：裁判可信度不足**")
                lines.append(f"一致率 {agreement:.1%} < 85%，本次 L1/L2 judge 分仅供参考")
                lines.append("")

    # ===== 总览 =====
    lines.append("# 评估聚合报告")
    lines.append("")
    lines.append(f"**生成时间：** {started_at}")
    lines.append(f"**Git Commit：** {git_commit[:8] if git_commit else 'N/A'}")
    lines.append(f"**模型配置：** {model_config_digest[:8]}")
    lines.append("")

    # 总分（加粗大字）
    lines.append(f"## 总分：**{overall_score:.1f}** / 100")
    lines.append("")

    # 三层分
    lines.append("### 分层评分")
    lines.append("")
    lines.append("| 层 | 分数 | 权重 | 模式 | 状态 |")
    lines.append("|----|----|----|----|----| ")

    for layer in ["L1", "L2", "L3"]:
        score = layer_scores.get(layer)
        weight = weights_normalized.get(layer, 0)
        mode = mode_dict.get(layer, "unknown")

        if score is None:
            score_str = "未测"
            status = "⊘"
        else:
            score_str = f"{score:.1f}"
            status = "✓"

        lines.append(f"| {layer} | {score_str} | {weight:.1%} | {mode} | {status} |")

    lines.append("")

    # 趋势
    if previous_run:
        prev_overall = previous_run["summary"]["overall_score"]
        delta = calculate_trend(overall_score, prev_overall)
        lines.append(f"**趋势（vs 上次）：** {delta}")
        lines.append("")
    else:
        lines.append("**趋势：** 首次运行，无历史对比")
        lines.append("")

    # 成本和用例数
    lines.append("### 运行参数")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|----|----|")
    lines.append(f"| 裁判成本（估算）| ¥{total_judge_cost:.2f} |")
    if previous_run:
        prev_cost = previous_run["summary"]["costs"].get("judge_cost_estimate_cny", 0)
        cost_delta = calculate_trend(total_judge_cost, prev_cost)
        lines.append(f"| 成本趋势 | {cost_delta} |")
    lines.append(f"| Run ID | {run_id} |")
    lines.append("")

    # ===== 各层详情 =====
    # L1 检索
    if layer_results.get("L1", {}).get("json"):
        lines.append("## L1 检索评估")
        lines.append("")
        l1_json = layer_results["L1"]["json"]
        lines.append(f"**用例数：** {l1_json.get('cases_run')}/{l1_json.get('cases_total')}")
        lines.append(f"**裁判成本：** ¥{l1_json.get('cost_estimate_cny', 0):.2f}")
        lines.append("")

        # 聚合指标
        agg = l1_json.get("aggregate", {})
        lines.append("| 指标 | 值 |")
        lines.append("|----|----|")
        for key in ["ndcg_at_10", "precision_at_10", "golden_recall", "sources_ok_rate"]:
            val = agg.get(key)
            if val is not None:
                lines.append(f"| {key} | {val:.4f} |")
        lines.append("")

    # L2 端到端
    if layer_results.get("L2", {}).get("json"):
        lines.append("## L2 端到端评估")
        lines.append("")
        l2_json = layer_results["L2"]["json"]
        lines.append(f"**用例数：** {l2_json.get('cases_run')}/{l2_json.get('cases_total')}")
        lines.append(f"**裁判成本：** ¥{l2_json.get('judge_cost_estimate_cny', 0):.2f}")
        lines.append("")

        # 聚合指标
        agg = l2_json.get("aggregate", {})
        lines.append("| 指标 | 值 |")
        lines.append("|----|----|")
        for key in ["task_completion_rate", "citation_valid_rate", "assertions_pass_rate"]:
            val = agg.get(key)
            if val is not None:
                lines.append(f"| {key} | {val:.2%} |")
        lines.append("")

    # L3 可靠性
    if layer_results.get("L3", {}).get("json"):
        lines.append("## L3 可靠性统计")
        lines.append("")
        l3_json = layer_results["L3"]["json"]

        turn_status = l3_json.get("turn_status", {})
        lines.append(f"**Turn 完成率：** {turn_status.get('completed_rate', 0):.1%}")

        workspace = l3_json.get("workspace_stats", {})
        # 中文注释：和 L3 单层报告保持一致的写法——每个比率都带上样本量。
        # 有些指标样本极小（精读全历史只有 2 次），只看百分比会把"1/2"误读成严重问题。
        download_attempted = workspace.get('download_attempted', 0)
        lines.append(
            f"**论文下载成功率：** {workspace.get('download_success_rate', 0):.1%}"
            f"（{workspace.get('download_succeeded', 0)}/{download_attempted}）"
        )
        lines.append(
            f"**Abstract Fallback 率：** {workspace.get('abstract_fallback_rate', 0):.1%}"
            f"（{workspace.get('abstract_fallback_count', 0)}/{workspace.get('deep_read_attempted', 0)}）"
        )
        lines.append(
            f"**评估覆盖率（会话级）：** {workspace.get('evaluation_coverage', 0):.1%}"
            f"（{workspace.get('sessions_with_evaluation', 0)}/{workspace.get('sessions_with_workspace', 0)} 个会话）"
        )
        lines.append("")

    # ===== 附录 =====
    lines.append("## 附录：数据质量与校准")
    lines.append("")

    # 校准结果
    if calibration_result:
        lines.append("### 裁判校准结果")
        lines.append("")
        if calibration_result.get("status") == "ok":
            labeled = calibration_result.get("labeled", 0)
            if labeled > 0:
                lines.append(f"**已标注样本数：** {labeled}")
                lines.append(f"**一致率：** {calibration_result.get('agreement_within_1', 0):.1%}")
                lines.append(f"**Spearman相关系数：** {calibration_result.get('spearman', 0):.3f}")

                cm = calibration_result.get("confusion_matrix", {})
                if cm:
                    lines.append(f"\n**混淆矩阵：**")
                    for k, v in sorted(cm.items()):
                        lines.append(f"- {k}: {v}")
            else:
                lines.append(f"**待标注样本数：** {calibration_result.get('unlabeled', 0)}")
                lines.append("**状态：** 校准集待人工标注，本次跳过")
        else:
            lines.append(f"**状态：** 校准失败 - {calibration_result.get('reason')}")
        lines.append("")

    lines.append("### 数据质量")
    lines.append("")
    lines.append("本次评估流量（eval_ 前缀会话）与真实用户流量分开统计。")
    lines.append("工具级耗时无法单独测量（待 P4 埋点；可参考 Turn 级耐时）。")
    lines.append("")

    return "\n".join(lines)


def run_calibration_if_enabled() -> Optional[dict]:
    """
    若启用校准模式，进程内导入 judge 并运行校准。

    返回校准结果或 None（未运行）。
    校准结果格式：
      {
        "status": "ok" | "failed",
        "labeled": 校准集中已标注的样本数,
        "unlabeled": 未标注的样本数,
        "agreement_within_1": 一致率,
        "spearman": Spearman相关系数,
        "confusion_matrix": {...},
        "reason": "error message"（若status==failed）
      }
    """
    try:
        from eval.lib.judge import build_judge_deps, run_calibration
    except ImportError as e:
        return {
            "status": "failed",
            "reason": f"无法导入 judge 模块：{e}"
        }

    # 读取校准集
    calibration_file = eval_config.EVAL_DIR / "datasets" / "judge_calibration.json"
    if not calibration_file.exists():
        return {
            "status": "failed",
            "reason": "校准集文件不存在"
        }

    try:
        with open(calibration_file, "r", encoding="utf-8") as f:
            calibration_data = json.load(f)
    except Exception as e:
        return {
            "status": "failed",
            "reason": f"无法读取校准集：{e}"
        }

    cases = calibration_data.get("cases", [])
    if not cases:
        return {
            "status": "ok",
            "labeled": 0,
            "unlabeled": 0,
            "note": "校准集为空"
        }

    # 检查是否有标注（human_score 非 null）
    labeled_count = sum(1 for c in cases if c.get("human_score") is not None)
    unlabeled_count = len(cases) - labeled_count

    if labeled_count == 0:
        # 没有标注，返回未标注状态但不调用 judge
        return {
            "status": "ok",
            "labeled": 0,
            "unlabeled": unlabeled_count,
            "note": "校准集待人工标注，本次跳过"
        }

    # 有标注，初始化 judge 并运行校准
    print("正在初始化 LLM 裁判...")
    judge_result = build_judge_deps()
    if judge_result["status"] != "ok":
        return {
            "status": "failed",
            "reason": f"裁判初始化失败：{judge_result.get('reason')}"
        }

    judge_deps = judge_result["deps"]
    print(f"裁判就绪：{judge_deps.model_name}")

    print(f"运行校准集评估（{labeled_count} 样本已标注）...")
    try:
        calib_result = run_calibration(cases, judge_deps)
        return calib_result
    except Exception as e:
        return {
            "status": "failed",
            "reason": f"校准运行失败：{e}"
        }


def main():
    # ===== CLI 参数解析 =====
    parser = argparse.ArgumentParser(
        description="聚合入口：依次运行三层评估，聚合总分，生成报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python eval/run_all.py                                    # 全量评估
  python eval/run_all.py --skip-l1 --skip-l2               # 仅 L3 统计
  python eval/run_all.py --skip-judge --only-l1 r01,r02    # L1 快速回归 + L3
  python eval/run_all.py --calibration                      # 校准集评估
        """,
    )

    parser.add_argument("--skip-l1", action="store_true", help="跳过 L1 检索评估")
    parser.add_argument("--skip-l2", action="store_true", help="跳过 L2 端到端评估")
    parser.add_argument("--skip-l3", action="store_true", help="跳过 L3 可靠性统计")
    parser.add_argument("--skip-judge", action="store_true", help="跳过 LLM 裁判（只用确定性指标）")
    parser.add_argument("--only-l1", type=str, default=None, help="L1 仅运行指定用例（逗号分隔，如 r01,r02）")
    parser.add_argument("--only-l2", type=str, default=None, help="L2 仅运行指定用例")
    parser.add_argument("--replay-l1", type=str, default=None, help="L1 重放历史数据（指定 raw/ 目录）")
    parser.add_argument("--include-holdout", action="store_true", help="L2 包含 holdout 测试用例")
    parser.add_argument("--calibration", action="store_true", help="运行校准集评估（--skip-l1 --skip-l2 --skip-l3 --calibration）")
    parser.add_argument("--base-url", type=str, default=None, help="L2 后端 URL（默认 http://127.0.0.1:8000）")
    parser.add_argument("--out-dir", type=Path, default=None, help="输出目录（默认 eval/reports/{{时间戳}}）")

    args = parser.parse_args()

    # 处理 --calibration 标志
    if args.calibration:
        args.skip_l1 = True
        args.skip_l2 = True
        args.skip_l3 = True

    # ===== 输出目录 =====
    if args.out_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.out_dir = eval_config.REPORTS_DIR / timestamp
    else:
        timestamp = args.out_dir.name

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"评估聚合入口 - run_all.py")
    print(f"{'='*60}")
    print(f"输出目录：{args.out_dir}")
    print(f"时间戳：{timestamp}")

    # ===== 元数据 =====
    run_id = timestamp
    started_at = datetime.now(timezone.utc).isoformat()
    git_commit = get_git_commit()
    model_config_digest = calculate_model_config_digest()

    print(f"Git Commit：{git_commit[:8] if git_commit else 'N/A'}")
    print(f"模型配置：{model_config_digest[:8]}")

    # ===== 预算估算 =====
    print(f"\n{estimate_budget(args.skip_judge)}")

    # ===== 运行三层 =====
    print(f"\n{'='*60}")
    print("运行评估层")
    print(f"{'='*60}")

    layer_results = {}
    mode_dict = {}
    layer_json_data = {}
    all_success = True

    for layer_name, runner_script in [("L1", "run_retrieval_eval.py"), ("L2", "run_e2e_eval.py"), ("L3", "run_reliability_stats.py")]:
        mode, json_data, success = run_layer_subprocess(layer_name, runner_script, args, args.out_dir)
        mode_dict[layer_name] = mode

        if not success:
            all_success = False
            layer_results[layer_name] = {"metrics_scored": 0, "metrics_missing": 0}
        else:
            layer_json_data[layer_name] = json_data

            # 聚合该层的分数
            score, metrics_scored, metrics_missing, detailed = aggregate_layer_score(json_data, layer_name, eval_config.THRESHOLDS)
            layer_results[layer_name] = {
                "score": score,
                "metrics_scored": metrics_scored,
                "metrics_missing": metrics_missing,
                "detailed": detailed,
                "json": json_data,
            }

    # ===== 计算总分 =====
    print(f"\n{'='*60}")
    print("计算总分")
    print(f"{'='*60}")

    layer_scores = {}
    valid_layers = []

    for layer in ["L1", "L2", "L3"]:
        score = layer_results.get(layer, {}).get("score")
        layer_scores[layer] = score

        if score is not None:
            valid_layers.append(layer)
            print(f"{layer}：{score:.1f}")
        else:
            print(f"{layer}：未测（跳过或失败）")

    # 权重归一化（某层被跳过时权重在剩余层间按比例归一）
    if valid_layers:
        weights = eval_config.LAYER_WEIGHTS.copy()
        total_weight = sum(weights.get(layer, 0) for layer in valid_layers)

        weights_normalized = {}
        for layer in ["L1", "L2", "L3"]:
            if layer in valid_layers:
                weights_normalized[layer] = weights.get(layer, 0) / total_weight
            else:
                weights_normalized[layer] = 0.0

        # 计算总分
        overall_score = sum(
            layer_scores[layer] * weights_normalized[layer]
            for layer in valid_layers
        )
    else:
        weights_normalized = {"L1": 0.0, "L2": 0.0, "L3": 0.0}
        overall_score = None

    print(f"\n权重归一化：")
    for layer in ["L1", "L2", "L3"]:
        print(f"  {layer}：{weights_normalized[layer]:.1%}")

    if overall_score is not None:
        print(f"\n总分：{overall_score:.1f} / 100")
    else:
        print(f"\n总分：无有效指标")

    # ===== 趋势对比 =====
    print(f"\n{'='*60}")
    print("趋势对比")
    print(f"{'='*60}")

    previous_run = find_previous_run(timestamp)

    if previous_run:
        prev_overall = previous_run["summary"]["overall_score"]
        if overall_score is not None:
            delta_str = calculate_trend(overall_score, prev_overall)
            print(f"总分趋势：{delta_str}")

        for layer in ["L1", "L2", "L3"]:
            prev_score = previous_run["summary"]["layers"].get(layer, {}).get("score")
            if prev_score is not None and layer_scores[layer] is not None:
                delta_str = calculate_trend(layer_scores[layer], prev_score)
                print(f"{layer} 趋势：{delta_str}")
    else:
        print("首次运行，无历史对比")

    # ===== 校准集评估（如启用） =====
    calibration_result = None
    if args.calibration:
        print(f"\n{'='*60}")
        print("校准集评估")
        print(f"{'='*60}")
        calibration_result = run_calibration_if_enabled()
        if calibration_result:
            if calibration_result.get("status") == "ok":
                # 校准成功，设置 mode.calibration = true
                mode_dict["calibration"] = True
                print(f"✓ 校准完成")
                if calibration_result.get("labeled") > 0:
                    print(f"  一致率：{calibration_result.get('agreement_within_1', 0):.1%}")
                    print(f"  Spearman相关系数：{calibration_result.get('spearman', 0):.3f}")
            else:
                # 校准失败，设置 mode.calibration = false 并记录失败原因
                mode_dict["calibration"] = False
                print(f"✗ 校准失败：{calibration_result.get('reason')}")
    else:
        # 未传 --calibration 标志，设置 mode.calibration = false
        mode_dict["calibration"] = False

    # ===== 生成报告 =====
    print(f"\n{'='*60}")
    print("生成报告")
    print(f"{'='*60}")

    finished_at = datetime.now(timezone.utc).isoformat()

    # 计算总裁判成本
    total_judge_cost = 0.0
    for layer_name in ["L1", "L2"]:
        if layer_name in layer_json_data and layer_json_data[layer_name] is not None:
            if layer_name == "L1":
                total_judge_cost += layer_json_data[layer_name].get("cost_estimate_cny", 0.0)
            elif layer_name == "L2":
                total_judge_cost += layer_json_data[layer_name].get("judge_cost_estimate_cny", 0.0)

    # 生成 report.json
    # 注：overall_score为None时转换为0.0是为了JSON序列化兼容（报告中会明确注明无有效指标）
    report_json = render_report_json(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        git_commit=git_commit,
        model_config_digest=model_config_digest,
        mode_dict=mode_dict,
        layer_results=layer_results,
        layer_scores=layer_scores,
        weights_normalized=weights_normalized,
        overall_score=overall_score if overall_score is not None else 0.0,
        total_judge_cost=total_judge_cost,
        previous_run=previous_run,
        calibration_result=calibration_result,
    )

    report_json_path = args.out_dir / "report.json"
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report_json, f, ensure_ascii=False, indent=2)
    print(f"✓ report.json 已保存：{report_json_path}")

    # 生成 report.md
    report_markdown = render_report_markdown(
        run_id=run_id,
        started_at=started_at,
        git_commit=git_commit,
        model_config_digest=model_config_digest,
        mode_dict=mode_dict,
        layer_scores=layer_scores,
        weights_normalized=weights_normalized,
        overall_score=overall_score if overall_score is not None else 0.0,
        total_judge_cost=total_judge_cost,
        previous_run=previous_run,
        layer_results=layer_results,
        calibration_result=calibration_result,
    )

    report_md_path = args.out_dir / "report.md"
    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write(report_markdown)
    print(f"✓ report.md 已保存：{report_md_path}")

    # ===== 最后总结 =====
    print(f"\n{'='*60}")
    print("评估完成")
    print(f"{'='*60}")

    overall_score_str = f"{overall_score:.1f}" if overall_score is not None else "N/A"
    l1_score_str = f"{layer_scores['L1']:.1f}" if layer_scores['L1'] is not None else "N/A"
    l2_score_str = f"{layer_scores['L2']:.1f}" if layer_scores['L2'] is not None else "N/A"
    l3_score_str = f"{layer_scores['L3']:.1f}" if layer_scores['L3'] is not None else "N/A"

    print(f"\n总分：{overall_score_str} / 100")
    print(f"三层分：L1={l1_score_str}, L2={l2_score_str}, L3={l3_score_str}")
    print(f"报告路径：{args.out_dir}")
    print()


if __name__ == "__main__":
    main()
