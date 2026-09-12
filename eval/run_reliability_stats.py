"""
CLI 入口：可靠性统计生成器
用法：python eval/run_reliability_stats.py [选项]
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

# 确保输出 UTF-8（Windows 环境防护）
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 加入仓库根到 sys.path，方便导入
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eval.config import (
    SESSIONS_DIR, LOGS_DIR, SQLITE_DB_PATH, REPORTS_DIR,
    EVAL_SESSION_PREFIX, THRESHOLDS
)
from eval.lib.event_stats import compute_reliability_stats


def format_markdown_report(stats: dict) -> str:
    """
    把统计结果格式化为 Markdown 报告
    标题带生成时间，低于阈值的指标标记 ⚠️
    """

    # 解析生成时间
    generated_at = stats.get('generated_at', '')
    report = f"# L3 可靠性统计报告\n\n"
    report += f"**生成时间：** {generated_at}\n\n"

    # ===== Turn 状态摘要 =====
    report += "## Turn 状态分布\n\n"

    turn_status = stats.get('turn_status', {})
    total = turn_status.get('total', 0)
    completed_rate = turn_status.get('completed_rate', 0.0)

    report += "| 状态 | 计数 | 百分比 |\n"
    report += "|------|------|--------|\n"

    for status in ['completed', 'failed', 'cancelled', 'interrupted']:
        count = turn_status.get(status, 0)
        pct = 100 * count / total if total > 0 else 0
        warning = ""

        # 检查是否低于阈值（completed 是 >= 比较，因为更高更好）
        if status == "completed" and completed_rate < THRESHOLDS.get('turn_completed_rate', 0.9):
            warning = " ⚠️"

        report += f"| {status} | {count} | {pct:.1f}% |{warning}\n"

    report += f"\n**完成率：** {100 * completed_rate:.1f}%"
    if completed_rate < THRESHOLDS.get('turn_completed_rate', 0.9):
        report += " ⚠️"
    report += "\n\n"

    # ===== Turn 耗时 =====
    report += "## Turn 耗时分布\n\n"

    turn_duration = stats.get('turn_duration_s', {})
    report += "| 分位数 | 耗时（秒） |\n"
    report += "|--------|----------|\n"
    report += f"| P50 | {turn_duration.get('p50', 0):.2f} |\n"
    report += f"| P95 | {turn_duration.get('p95', 0):.2f} |\n"
    report += f"| Max | {turn_duration.get('max', 0):.2f} |\n\n"

    # ===== 工具统计 =====
    report += "## 工具执行统计\n\n"

    tool_stats = stats.get('tool_stats', {}).get('by_stage', {})
    for stage, stats_data in sorted(tool_stats.items()):
        report += f"### {stage}\n\n"
        report += "| 指标 | 值 |\n"
        report += "|------|---|\n"
        report += f"| 调用次数 | {stats_data.get('calls', 0)} |\n"
        report += f"| 失败数 | {stats_data.get('failed', 0)} |\n"

        # 失败率需要检查阈值（失败率越低越好）
        failure_rate = stats_data.get('failure_rate', 0.0)
        failure_rate_warning = ""
        # 检查是否超过阈值（这里用的是 tool_success_rate，需要转换成失败率阈值）
        # 简报说"工具成功率 >= 95%"，即失败率 <= 5%
        max_failure_rate = 1.0 - THRESHOLDS.get('tool_success_rate', 0.95)
        if failure_rate > max_failure_rate:
            failure_rate_warning = " ⚠️"

        report += f"| 失败率 | {100 * failure_rate:.1f}% |{failure_rate_warning}\n"
        report += f"| 耗时 P50 | {stats_data.get('duration_s_p50', 0):.2f}s |\n"
        report += f"| 耗时 P95 | {stats_data.get('duration_s_p95', 0):.2f}s |\n"

        # 显示 top 错误
        top_errors = stats_data.get('top_errors', [])
        if top_errors:
            report += f"\n**常见错误：**\n"
            for error in top_errors:
                report += f"- {error['text'][:100]}... (出现 {error['count']} 次)\n"

        report += "\n"

    # ===== LLM 统计 =====
    report += "## LLM 调用统计\n\n"

    llm_stats = stats.get('llm_stats', {})
    report += "| 指标 | 值 |\n"
    report += "|------|---|\n"
    report += f"| 总调用数 | {llm_stats.get('calls_total', 0)} |\n"

    # LLM 重试率检查阈值（重试率越低越好，阈值 <= 10%）
    llm_retry_rate = llm_stats.get('retry_rate', 0.0)
    llm_retry_warning = ""
    if llm_retry_rate > THRESHOLDS.get('llm_retry_rate', 0.1):
        llm_retry_warning = " ⚠️"

    report += f"| 重试率 | {100 * llm_retry_rate:.1f}% |{llm_retry_warning}\n"

    # 错误分布
    error_kinds = llm_stats.get('error_kind_dist', {})
    if error_kinds:
        report += f"\n**错误分布：**\n"
        for error_kind, count in sorted(error_kinds.items(), key=lambda x: -x[1]):
            report += f"- {error_kind}: {count}\n"

    # 完成原因分布
    finish_reasons = llm_stats.get('finish_reason_dist', {})
    if finish_reasons:
        report += f"\n**完成原因分布：**\n"
        for reason, count in sorted(finish_reasons.items(), key=lambda x: -x[1]):
            report += f"- {reason}: {count}\n"

    report += "\n"

    # ===== Token 与成本 =====
    report += "## Token 与成本估算\n\n"

    tokens = stats.get('tokens', {})
    events_tokens = tokens.get('from_events', {})
    log_tokens = tokens.get('from_logs', {})
    cost = tokens.get('cost_estimate_cny', 0.0)

    report += "| 来源 | 输入 Token | 输出 Token |\n"
    report += "|------|----------|----------|\n"
    report += f"| 事件流 | {events_tokens.get('input', 0)} | {events_tokens.get('output', 0)} |\n"
    report += f"| 日志 | {log_tokens.get('input', 0)} | {log_tokens.get('output', 0)} |\n\n"

    report += f"**成本估算（占位价格）：** ¥{cost:.4f}\n\n"

    # ===== 工作区统计 =====
    report += "## 工作区统计\n\n"

    workspace = stats.get('workspace_stats', {})
    report += "| 指标 | 值 | 样本（分子/分母） |\n"
    report += "|------|---|---|\n"
    report += f"| 论文总数 | {workspace.get('papers_total', 0)} | — |\n"

    # 中文注释：下面每个比率都把分子/分母一起列出来。因为有些指标的样本极小
    # （比如精读全历史只发生过 2 次），只写一个百分比会严重误导——看到"50%"
    # 会以为问题很大，看到"1/2"才知道这只是两篇里有一篇降级了。

    # 下载成功率检查阈值（越高越好，>= 90%）
    # 口径：分母是真正发起过下载或精读的论文数，不是工作区里累积的全部论文数。
    download_success_rate = workspace.get('download_success_rate', 0.0)
    download_warning = ""
    if download_success_rate < THRESHOLDS.get('download_success_rate', 0.9):
        download_warning = " ⚠️"
    download_sample = f"{workspace.get('download_succeeded', 0)}/{workspace.get('download_attempted', 0)}"
    report += f"| 下载成功率 | {100 * download_success_rate:.1f}% | {download_sample}{download_warning} |\n"

    # Abstract Fallback 率检查阈值（越低越好，<= 20%）
    abstract_fallback_rate = workspace.get('abstract_fallback_rate', 0.0)
    abstract_warning = ""
    if abstract_fallback_rate > THRESHOLDS.get('abstract_fallback_rate', 0.2):
        abstract_warning = " ⚠️"
    fallback_sample = f"{workspace.get('abstract_fallback_count', 0)}/{workspace.get('deep_read_attempted', 0)}"
    report += f"| Abstract Fallback 率 | {100 * abstract_fallback_rate:.1f}% | {fallback_sample}{abstract_warning} |\n"

    # 搜索放宽率检查阈值（越低越好，<= 30%）
    relaxed_search_rate = workspace.get('relaxed_search_rate', 0.0)
    relaxed_warning = ""
    if relaxed_search_rate > THRESHOLDS.get('search_relaxed_rate', 0.3):
        relaxed_warning = " ⚠️"
    report += f"| 搜索放宽率 | {100 * relaxed_search_rate:.1f}% | —{relaxed_warning} |\n"

    # 评估覆盖率检查阈值（越高越好，>= 80%）
    # 口径：会话级——有工作区的会话里，有多少个至少评估过一篇论文。
    evaluation_coverage = workspace.get('evaluation_coverage', 0.0)
    coverage_warning = ""
    if evaluation_coverage < THRESHOLDS.get('evaluation_coverage', 0.8):
        coverage_warning = " ⚠️"
    coverage_sample = f"{workspace.get('sessions_with_evaluation', 0)}/{workspace.get('sessions_with_workspace', 0)} 个会话"
    report += f"| 评估覆盖率 | {100 * evaluation_coverage:.1f}% | {coverage_sample}{coverage_warning} |\n\n"

    # ===== 用户信号 =====
    report += "## 用户信号\n\n"

    user_signals = stats.get('user_signals', {})
    report += "| 信号 | 值 |\n"
    report += "|------|---|\n"

    # 取消率检查阈值（越低越好，<= 5%）
    cancel_rate = user_signals.get('cancel_rate', 0.0)
    cancel_warning = ""
    if cancel_rate > THRESHOLDS.get('cancel_rate', 0.05):
        cancel_warning = " ⚠️"
    report += f"| 取消率 | {100 * cancel_rate:.1f}% |{cancel_warning}\n"

    # 失败后重试率检查阈值（越低越好，<= 30%）
    retry_rate = user_signals.get('retry_after_fail_rate', 0.0)
    retry_warning = ""
    if retry_rate > THRESHOLDS.get('retry_after_fail_rate', 0.3):
        retry_warning = " ⚠️"
    report += f"| 失败后重试率 | {100 * retry_rate:.1f}% |{retry_warning}\n\n"

    # 按日期的 turn 分布
    turns_by_day = user_signals.get('turns_by_day', {})
    if turns_by_day:
        report += "**按日期的 Turn 状态分布：**\n\n"
        report += "| 日期 | Completed | Failed | Cancelled | Interrupted |\n"
        report += "|------|-----------|--------|-----------|-------------|\n"

        for date in sorted(turns_by_day.keys()):
            day_data = turns_by_day[date]
            report += f"| {date} | "
            report += f"{day_data.get('completed', 0)} | "
            report += f"{day_data.get('failed', 0)} | "
            report += f"{day_data.get('cancelled', 0)} | "
            report += f"{day_data.get('interrupted', 0)} |\n"

    report += "\n"

    # ===== 流量区分 =====
    report += "## 流量区分（评估 vs 真实）\n\n"

    traffic_split = stats.get('traffic_split', {})

    report += "### 真实用户流量\n\n"
    real_statuses = traffic_split.get('real', {}).get('turn_status', {})
    if real_statuses:
        report += "| 状态 | 计数 |\n"
        report += "|------|------|\n"
        for status, count in sorted(real_statuses.items()):
            report += f"| {status} | {count} |\n"
    else:
        report += "（暂无数据）\n"

    report += "\n### 评估流量\n\n"
    eval_statuses = traffic_split.get('eval', {}).get('turn_status', {})
    if eval_statuses:
        report += "| 状态 | 计数 |\n"
        report += "|------|------|\n"
        for status, count in sorted(eval_statuses.items()):
            report += f"| {status} | {count} |\n"
    else:
        report += "（暂无数据）\n"

    report += "\n"

    # ===== 数据质量 =====
    report += "## 数据质量\n\n"

    data_quality = stats.get('data_quality', {})
    report += "| 项目 | 值 |\n"
    report += "|------|---|\n"
    report += f"| 日志行解析成功 | {data_quality.get('log_lines_parsed', 0)} |\n"
    report += f"| 日志行解析失败 | {data_quality.get('log_lines_failed', 0)} |\n\n"

    notes = data_quality.get('notes', [])
    if notes:
        report += "**备注：**\n"
        for note in notes:
            report += f"- {note}\n"

    # 添加工具耐时数据质量说明
    report += "\n**工具级耐时数据质量说明：**\n"
    report += "- 工具级耐时（p50/p95）均为 0：事件数据中各工具的 created_at 和 completed_at 时间戳相同，无法测量单个工具耐时\n"
    report += "- Turn 级耐时（聚合）正确计算，从会话首个事件到 turn_end 的时间差已采集\n"

    return report


def print_summary(stats: dict) -> None:
    """
    打印精简摘要（到 stdout）
    """

    print("\n" + "=" * 60)
    print("L3 可靠性统计 - 执行摘要")
    print("=" * 60)

    # 会话数
    scope = stats.get('scope', {})
    print(f"\n会话总数：{scope.get('sessions_total', 0)}")
    print(f"  - 真实流量：{scope.get('sessions_real', 0)}")
    print(f"  - 评估流量：{scope.get('sessions_eval', 0)}")
    print(f"  - 跳过：{scope.get('sessions_skipped', [])}")

    # Turn 四态
    turn_status = stats.get('turn_status', {})
    print(f"\nTurn 状态分布：")
    print(f"  - Completed：{turn_status.get('completed', 0)} ({100*turn_status.get('completed_rate', 0):.1f}%)")
    print(f"  - Failed：{turn_status.get('failed', 0)}")
    print(f"  - Cancelled：{turn_status.get('cancelled', 0)}")
    print(f"  - Interrupted：{turn_status.get('interrupted', 0)}")

    # Token 与成本
    tokens = stats.get('tokens', {})
    events_tokens = tokens.get('from_events', {})
    log_tokens = tokens.get('from_logs', {})
    cost = tokens.get('cost_estimate_cny', 0.0)

    total_tokens = events_tokens.get('input', 0) + events_tokens.get('output', 0)
    print(f"\nToken 统计（事件口径）：")
    print(f"  - 输入：{events_tokens.get('input', 0):,}")
    print(f"  - 输出：{events_tokens.get('output', 0):,}")
    print(f"  - 小计：{total_tokens:,}")

    print(f"\n成本估算（占位价格）：¥{cost:.4f}")

    # 最差的 3 个指标
    print(f"\n可能的风险指标：")

    issues = []

    # 完成率
    if turn_status.get('completed_rate', 0) < 0.9:
        issues.append(f"Turn 完成率 {100*turn_status.get('completed_rate', 0):.1f}% < 90%")

    # 工具成功率
    tool_stats = stats.get('tool_stats', {}).get('by_stage', {})
    for stage, stage_data in tool_stats.items():
        failure_rate = stage_data.get('failure_rate', 0.0)
        if failure_rate > 0.1:  # 失败率 > 10%
            issues.append(f"工具 {stage} 失败率 {100*failure_rate:.1f}% > 10%")

    workspace = stats.get('workspace_stats', {})

    # 下载成功率（越高越好）
    download_rate = workspace.get('download_success_rate', 0)
    if download_rate < THRESHOLDS.get('download_success_rate', 0.9):
        issues.append(
            f"论文下载成功率 {100*download_rate:.1f}% < "
            f"{100*THRESHOLDS.get('download_success_rate', 0.9):.0f}%"
            f"（样本 {workspace.get('download_succeeded', 0)}/{workspace.get('download_attempted', 0)}）"
        )

    # 中文注释：以前这段摘要只检查了下载成功率，漏掉了 Abstract Fallback 率和评估覆盖率——
    # 它们虽然在 Markdown 报告里标了 ⚠️，但终端摘要里完全不提，看摘要的人会以为没这回事。
    # 现在补齐，并且每个数字都带上样本量，避免"1/2 = 50%"被当成严重问题。

    # Abstract Fallback 率（越低越好）
    fallback_rate = workspace.get('abstract_fallback_rate', 0)
    if fallback_rate > THRESHOLDS.get('abstract_fallback_rate', 0.2):
        issues.append(
            f"Abstract Fallback 率 {100*fallback_rate:.1f}% > "
            f"{100*THRESHOLDS.get('abstract_fallback_rate', 0.2):.0f}%"
            f"（样本 {workspace.get('abstract_fallback_count', 0)}/{workspace.get('deep_read_attempted', 0)}）"
        )

    # 评估覆盖率（越高越好）
    coverage = workspace.get('evaluation_coverage', 0)
    if coverage < THRESHOLDS.get('evaluation_coverage', 0.8):
        issues.append(
            f"评估覆盖率 {100*coverage:.1f}% < "
            f"{100*THRESHOLDS.get('evaluation_coverage', 0.8):.0f}%"
            f"（{workspace.get('sessions_with_evaluation', 0)}/{workspace.get('sessions_with_workspace', 0)} 个会话）"
        )

    if issues:
        # 中文注释：以前这里写死只打印前 3 条（issues[:3]），第 4 条起会被悄悄吞掉，
        # 看摘要的人根本不知道还有别的问题。现在全打印，并明确告诉还有多少条没列。
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
    else:
        print("  (无重大风险)")

    print("\n" + "=" * 60 + "\n")


def main():
    """
    主入口
    """

    parser = argparse.ArgumentParser(
        description="L3 可靠性统计生成器 - 从事件流和日志中分析系统可靠性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python eval/run_reliability_stats.py
  python eval/run_reliability_stats.py --sessions-root data/sessions --logs-dir logs
        """,
    )

    parser.add_argument(
        '--sessions-root',
        type=Path,
        default=SESSIONS_DIR,
        help=f"会话数据根目录（默认：{SESSIONS_DIR}）",
    )

    parser.add_argument(
        '--logs-dir',
        type=Path,
        default=LOGS_DIR,
        help=f"日志文件目录（默认：{LOGS_DIR}）",
    )

    parser.add_argument(
        '--out-dir',
        type=Path,
        default=None,
        help=f"报告输出目录（默认：eval/reports/{{时间戳}}）",
    )

    args = parser.parse_args()

    # 决定输出目录
    if args.out_dir is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        args.out_dir = REPORTS_DIR / timestamp
    else:
        args.out_dir = Path(args.out_dir)

    # 创建输出目录
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # 确定日志文件列表
    log_paths = []
    if args.logs_dir.exists():
        log_paths = list(args.logs_dir.glob('app.log*'))
        log_paths.sort()

    # 执行统计计算
    print(f"正在分析 {args.sessions_root}...\n")

    stats = compute_reliability_stats(
        sessions_root=args.sessions_root,
        log_paths=log_paths,
        sqlite_path=SQLITE_DB_PATH,
        eval_session_prefix=EVAL_SESSION_PREFIX,
    )

    # 检查是否有错误
    if 'error' in stats:
        print(f"错误：{stats['error']}", file=sys.stderr)
        sys.exit(1)

    # 打印摘要
    print_summary(stats)

    # 写 JSON 报告
    json_path = args.out_dir / 'reliability.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(f"✓ JSON 报告已保存：{json_path}")

    # 写 Markdown 报告
    markdown_report = format_markdown_report(stats)
    md_path = args.out_dir / 'reliability.md'
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(markdown_report)

    print(f"✓ Markdown 报告已保存：{md_path}")

    print(f"\n报告目录：{args.out_dir}")


if __name__ == '__main__':
    main()
