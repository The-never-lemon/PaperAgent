"""
L3 可靠性统计器 - 离线分析事件流和日志，生成可靠性与效率指标

本模块负责：
1. 读取会话的 events.jsonl 文件
2. 解析日志文件中的 LLM 调用记录
3. 遍历工作区的 papers.json
4. 计算 turn 级、工具级、LLM 级、token 与成本、工作区级、用户信号等指标
5. 区分评估流量与真实流量
6. 生成结构化的 JSON 报告
"""

import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from collections import defaultdict, Counter
from typing import Dict, List, Any, Optional, Tuple
from statistics import median, quantiles


@dataclass
class EventStatsDeps:
    """事件统计的依赖配置"""
    sessions_root: Path           # 会话数据根目录
    log_paths: List[Path]         # 要分析的日志文件列表
    sqlite_path: Path             # SQLite 数据库路径
    eval_session_prefix: str = "eval_"  # 评估流量的会话标记


def percentile(data: List[float], p: int) -> float:
    """
    计算分位数（p 为 0-100 之间的百分比，如 50 表示中位数）
    使用简单的线性插值方法，避免依赖 numpy
    """
    if not data:
        return 0.0

    sorted_data = sorted(data)
    n = len(sorted_data)

    if n == 1:
        return sorted_data[0]

    # 使用线性插值计算分位数
    index = (p / 100.0) * (n - 1)
    lower_idx = int(index)
    upper_idx = min(lower_idx + 1, n - 1)

    if lower_idx == upper_idx:
        return sorted_data[lower_idx]

    # 在两个值之间线性插值
    fraction = index - lower_idx
    return sorted_data[lower_idx] * (1 - fraction) + sorted_data[upper_idx] * fraction


def parse_iso_datetime(iso_str: str) -> Optional[datetime]:
    """
    解析 ISO 格式的时间字符串
    格式：2026-09-08T05:19:35.698766+00:00
    """
    try:
        # 移除时区信息后解析
        if '+' in iso_str:
            iso_str = iso_str.split('+')[0]
        elif iso_str.endswith('Z'):
            iso_str = iso_str[:-1]

        return datetime.fromisoformat(iso_str)
    except:
        return None


def calculate_time_diff_seconds(start_time: str, end_time: str) -> Optional[float]:
    """
    计算两个 ISO 时间字符串之间的差值（秒）
    """
    start = parse_iso_datetime(start_time)
    end = parse_iso_datetime(end_time)

    if start and end:
        delta = end - start
        return delta.total_seconds()

    return None


def load_session_events(session_dir: Path) -> List[Dict[str, Any]]:
    """
    加载单个会话的所有事件
    返回按 seq_no 排序的事件列表
    """
    events_file = session_dir / "events.jsonl"

    if not events_file.exists():
        return []

    events = []
    try:
        with open(events_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError:
                        # 跳过损坏的 JSON 行
                        pass
    except Exception as e:
        print(f"警告：无法读取 {events_file}: {e}")
        return []

    # 按 seq_no 排序以确保时间顺序
    events.sort(key=lambda e: e.get('seq_no', 0))

    return events


def _extract_paper_id(arguments_summary: Any) -> Optional[str]:
    """
    从工具调用的参数摘要里取出论文编号。

    中文注释：
    事件流里每次工具调用都会把参数存成一段 JSON 字符串（arguments_summary），
    形如 {"paper_id": "10.1145/3620666.3651380", "direction": "references"}。
    我们要靠它知道"这次动作到底作用在哪篇论文上"，从而用"真正发起过动作的论文"
    作为成功率的分母，而不是拿工作区里所有论文当分母。

    取不到（参数缺失、JSON 解析失败、没有 paper_id 字段）就返回 None，
    调用方会跳过这一次，不会把脏数据算进分母。
    """

    if not arguments_summary:
        return None

    try:
        payload = json.loads(arguments_summary) if isinstance(arguments_summary, str) else arguments_summary
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    paper_id = payload.get('paper_id')
    if paper_id is None:
        return None

    text = str(paper_id).strip()
    return text or None


def load_workspace(session_dir: Path) -> Optional[Dict[str, Any]]:
    """
    加载会话的工作区 papers.json 数据
    """
    workspace_file = session_dir / "workspace" / "papers.json"

    if not workspace_file.exists():
        return None

    try:
        with open(workspace_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"警告：无法读取 {workspace_file}: {e}")
        return None


def parse_log_llm_calls(log_paths: List[Path]) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    从日志文件中解析 LLM 调用信息
    返回 (调用列表, 成功解析的行数, 解析失败的行数)
    """
    llm_calls = []
    parsed_count = 0
    failed_count = 0

    for log_path in log_paths:
        if not log_path.exists():
            continue

        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if "模型调用完成" not in line:
                        continue

                    try:
                        # 日志格式：时间 | LEVEL | logger | req=... | 消息 | ctx={JSON}
                        # 提取 ctx 部分
                        if "ctx=" not in line:
                            failed_count += 1
                            continue

                        ctx_start = line.find("ctx=")
                        ctx_json_str = line[ctx_start + 4:]

                        # 尝试解析 JSON
                        ctx = json.loads(ctx_json_str)

                        # 提取日志的时间戳（行开始）
                        time_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                        if time_match:
                            log_time_str = time_match.group(1)
                            ctx['_log_time'] = log_time_str

                        llm_calls.append(ctx)
                        parsed_count += 1
                    except (json.JSONDecodeError, ValueError):
                        failed_count += 1
                        continue
        except Exception as e:
            print(f"警告：无法读取日志 {log_path}: {e}")
            continue

    return llm_calls, parsed_count, failed_count


def get_session_title_from_db(sqlite_path: Path, session_id: str) -> Optional[str]:
    """
    从 SQLite 数据库查询会话标题
    使用只读 URI 防止锁竞争
    """
    if not sqlite_path.exists():
        return None

    try:
        uri = f"file:{sqlite_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cursor = conn.cursor()

        cursor.execute("SELECT title FROM session WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return row[0]
    except Exception as e:
        print(f"警告：无法查询数据库 {sqlite_path}: {e}")
        pass

    return None


def compute_reliability_stats(
    sessions_root: Path,
    log_paths: List[Path],
    sqlite_path: Path,
    eval_session_prefix: str = "eval_",
) -> Dict[str, Any]:
    """
    主入口：计算所有可靠性统计指标
    返回结构化的统计结果 dict（对应最终 JSON schema）
    """

    # ========== 第一部分：收集所有会话数据 ==========

    # 扫描会话目录
    session_dirs = []
    skipped_sessions = []

    if not sessions_root.exists():
        return {"error": f"会话根目录不存在: {sessions_root}"}

    for item in sessions_root.iterdir():
        if not item.is_dir():
            continue

        # smoke2 是垃圾目录，跳过
        if item.name == "smoke2":
            skipped_sessions.append(item.name)
            continue

        session_dirs.append(item)

    # 按目录名排序以确保一致性
    session_dirs.sort(key=lambda x: x.name)

    # ========== 第二部分：分析每个会话的事件 ==========

    # 统计数据容器
    turn_statuses = Counter()  # {status: count}
    turn_durations = []        # 所有 turn 的耗时
    tool_stats = defaultdict(lambda: {  # 按 stage 分组的工具统计
        'calls': 0,
        'failed': 0,
        'failure_rate': 0.0,
        'duration_s_p50': 0.0,
        'duration_s_p95': 0.0,
        'top_errors': [],
        'durations': [],  # 临时存储用于计算分位数
    })
    user_messages_by_session = defaultdict(list)  # 记录每个会话的用户消息
    cancelled_turns = 0
    failed_then_retried = 0  # 失败后 60 秒内有新用户消息的 turn

    traffic_split = {
        'real': {'turn_status': Counter()},
        'eval': {'turn_status': Counter()},
    }

    by_day_turns = defaultdict(lambda: Counter())  # {日期: {状态: count}}

    # 中文注释：记录"到底对哪篇论文动过手"——这是下载成功率 / abstract_fallback 率的分子分母来源。
    # 为什么不直接用工作区里累积的论文总数当分母？因为工作区里绝大部分论文只是检索结果躺在那里，
    # 用户从来没要求下载或精读。拿它们当分母，会把下载成功率算成 0.4%（1/244）这种没有意义的数字，
    # 而且 0.9 的阈值在这个口径下数学上永远不可能达到。
    # 这里改成从工具调用反推：只有真正发起过 download_paper / deep_read_paper 的论文才算进分母。
    # 元素是 (会话目录名, 论文编号)，这样同一篇论文出现在多个会话时不会互相干扰。
    download_attempt_papers: set = set()
    deep_read_attempt_papers: set = set()

    # 遍历每个会话
    for session_dir in session_dirs:
        session_id = session_dir.name

        # 加载该会话的事件
        events = load_session_events(session_dir)

        if not events:
            continue

        # 从数据库获取会话标题
        session_title = get_session_title_from_db(sqlite_path, session_id)

        # 判断流量类型
        is_eval_traffic = session_title and session_title.startswith(eval_session_prefix)
        traffic_type = 'eval' if is_eval_traffic else 'real'

        # ===== 分析 turn 级别指标 =====

        # 按 turn_id 分组事件
        turns_by_id = defaultdict(list)
        for event in events:
            turn_id = event.get('metadata', {}).get('turn_id')
            if turn_id:
                turns_by_id[turn_id].append(event)

        # 处理每个 turn
        for turn_id, turn_events in turns_by_id.items():
            # 找 turn_end 事件
            turn_end_event = None
            for e in turn_events:
                if e.get('event_type') == 'turn_end':
                    turn_end_event = e
                    break

            if not turn_end_event:
                continue

            # 提取 turn 状态
            status = turn_end_event.get('metadata', {}).get('status', 'unknown')
            turn_statuses[status] += 1
            traffic_split[traffic_type]['turn_status'][status] += 1

            # 按日期记录
            turn_time = turn_end_event.get('created_at')
            if turn_time:
                turn_date = turn_time.split('T')[0]  # 提取 YYYY-MM-DD
                by_day_turns[turn_date][status] += 1

            # 计算 turn 耗时（从最早事件到 turn_end）
            if turn_events:
                first_event_time = turn_events[0].get('created_at')
                end_event_time = turn_end_event.get('created_at')

                if first_event_time and end_event_time:
                    duration_s = calculate_time_diff_seconds(first_event_time, end_event_time)
                    if duration_s is not None:
                        turn_durations.append(duration_s)

            # ===== 分析工具调用 =====

            # 中文注释：先把同一个步骤的所有上报事件合并成一条记录，再统计次数。
            # 为什么必须合并？因为一个耗时长的工具会反复上报 status='running' 的进度事件
            # （实测一次 6 分 43 秒的精读，同一步骤上报了 58 条 running + 1 条 completed）。
            # 以前是"每条 running 都算一次调用"，结果报告里 deep_read_paper 显示调用了 64 次，
            # 而真实调用只有 2 次——分母虚高 32 倍，所有失败率都跟着失真。
            # 现在按 event_key 合并（event_key 在同一轮对话里唯一标识一个步骤），一步骤只算一次。
            step_records: Dict[str, Dict[str, Any]] = {}

            for event in turn_events:
                if event.get('event_type') != 'runtime_event':
                    continue

                metadata = event.get('metadata', {})

                # 只处理 type=workflow_step 的事件
                if metadata.get('type') != 'workflow_step':
                    continue

                # stage 在嵌套的 metadata 里
                inner_metadata = metadata.get('metadata', {})
                stage = inner_metadata.get('stage')
                step_key = inner_metadata.get('event_key')
                if not stage or not step_key:
                    continue

                record = step_records.setdefault(step_key, {
                    'stage': stage,
                    'failed': False,
                    'error': '',
                    'created_at': None,
                    'completed_at': None,
                    'arguments_summary': inner_metadata.get('arguments_summary'),
                })

                # 只要出现过一次 failed，这个步骤就算失败（同一步骤可能同时有 running 和 failed 两条）
                if metadata.get('status') == 'failed':
                    record['failed'] = True
                    if not record['error']:
                        record['error'] = str(event.get('content', ''))

                # 起止时间取到就更新，用于算耗时
                if metadata.get('created_at'):
                    record['created_at'] = metadata.get('created_at')
                if metadata.get('completed_at'):
                    record['completed_at'] = metadata.get('completed_at')

            for step_key, record in step_records.items():
                stage = record['stage']

                # 记录调用：一个步骤只算一次调用
                tool_stats[stage]['calls'] += 1

                if record['failed']:
                    tool_stats[stage]['failed'] += 1
                    if record['error']:
                        # 截断到 200 字
                        tool_stats[stage]['top_errors'].append(record['error'][:200])

                # 计算耗时
                created_at = record['created_at']
                completed_at = record['completed_at']
                if created_at and completed_at:
                    duration_s = calculate_time_diff_seconds(created_at, completed_at)
                    if duration_s is not None:
                        tool_stats[stage]['durations'].append(duration_s)

                # 中文注释：记下"对哪篇论文动过手"。只有下载和精读这两个动作才意味着
                # 需要去拿全文，所以它们才是下载成功率的合理分母来源。
                if stage in ('download_paper', 'deep_read_paper'):
                    attempted_paper_id = _extract_paper_id(record['arguments_summary'])
                    if attempted_paper_id:
                        bucket = download_attempt_papers if stage == 'download_paper' else deep_read_attempt_papers
                        bucket.add((session_id, attempted_paper_id))
                        # 精读内部也会去拿全文，所以它同样算一次"下载尝试"
                        if stage == 'deep_read_paper':
                            download_attempt_papers.add((session_id, attempted_paper_id))

            # ===== 分析用户消息与取消 =====

            if status == 'cancelled':
                cancelled_turns += 1

            # 查找该 turn 中的用户消息事件
            turn_user_messages = []
            for e in turn_events:
                if e.get('event_type') == 'message':
                    metadata = e.get('metadata', {})
                    if metadata.get('role') == 'user':
                        turn_user_messages.append(e.get('created_at'))
                        user_messages_by_session[session_id].append({
                            'turn_id': turn_id,
                            'time': e.get('created_at')
                        })

            # 检查失败后重试：如果该 turn 状态是 failed，检查后续 60 秒内是否有新用户消息
            if status == 'failed' and turn_end_event.get('created_at'):
                fail_time = parse_iso_datetime(turn_end_event.get('created_at'))

                # 查找同会话中晚于 fail_time 的用户消息
                for other_turn_id, other_events in turns_by_id.items():
                    if other_turn_id == turn_id:
                        continue

                    for e in other_events:
                        if e.get('event_type') == 'message':
                            metadata = e.get('metadata', {})
                            if metadata.get('role') != 'user':
                                continue

                            msg_time = parse_iso_datetime(e.get('created_at'))
                            if msg_time and fail_time:
                                time_diff = (msg_time - fail_time).total_seconds()

                                # 检查是否在 60 秒内且时间更晚
                                if 0 < time_diff <= 60:
                                    failed_then_retried += 1
                                    break

    # ========== 第三部分：分析日志中的 LLM 调用 ==========

    llm_calls, llm_parsed_count, llm_failed_count = parse_log_llm_calls(log_paths)

    llm_error_kinds = Counter()
    llm_finish_reasons = Counter()
    llm_retries = 0
    llm_durations_by_op = defaultdict(list)

    llm_tokens_from_logs = {'input': 0, 'output': 0}

    for call in llm_calls:
        # 错误类型统计
        error_kind = call.get('error_kind')
        if error_kind:
            llm_error_kinds[error_kind] += 1

        # 完成原因统计
        finish_reason = call.get('finish_reason')
        if finish_reason:
            llm_finish_reasons[finish_reason] += 1

        # 重试率
        attempts = call.get('attempts', 1)
        if attempts > 1:
            llm_retries += 1

        # 按操作类型的耗时
        operation = call.get('operation')
        duration_ms = call.get('duration_ms')
        if operation and duration_ms is not None:
            llm_durations_by_op[operation].append(duration_ms)

        # Token 统计
        usage = call.get('usage', {})
        if isinstance(usage, dict):
            # 处理不同的 token 字段名
            input_tokens = usage.get('prompt_tokens') or usage.get('input_tokens', 0)
            output_tokens = usage.get('completion_tokens') or usage.get('output_tokens', 0)

            llm_tokens_from_logs['input'] += input_tokens
            llm_tokens_from_logs['output'] += output_tokens

    # ========== 第四部分：收集工作区统计 ==========

    workspace_stats = {
        'papers_total': 0,
        'papers_with_fulltext': 0,
        'papers_with_evaluation': 0,
        'papers_with_deep_read': 0,
        'abstract_fallback_count': 0,
    }

    search_history_relaxed = 0
    search_history_total = 0

    # 中文注释：把每个会话的论文表存下来，供后面按"尝试口径"核对某篇论文是否真的拿到了全文。
    # 键是会话目录名，值是那个会话工作区里的 {论文编号: 论文信息}。
    workspace_papers_by_session: Dict[str, Dict[str, Any]] = {}
    # 中文注释：评估覆盖率改成会话级（和 eval/README.md 里写的"评估用例覆盖的会话占比"对齐）：
    # 分母是有工作区的会话数，分子是其中至少有一篇论文被评估过的会话数。
    sessions_with_workspace = 0
    sessions_with_evaluation = 0

    for session_dir in session_dirs:
        if session_dir.name == "smoke2":
            continue

        workspace = load_workspace(session_dir)
        if not workspace:
            continue

        sessions_with_workspace += 1

        # 统计论文
        papers = workspace.get('papers', {})
        workspace_papers_by_session[session_dir.name] = papers
        session_has_evaluation = False

        for paper_id, paper_info in papers.items():
            workspace_stats['papers_total'] += 1

            # 下载成功率（分子）：全文是否真的落到本地缓存
            if paper_info.get('fulltext_cached'):
                workspace_stats['papers_with_fulltext'] += 1

            # 评估覆盖率（分子，会话级）：这个会话里有没有论文被评估过
            if paper_info.get('evaluation'):
                workspace_stats['papers_with_evaluation'] += 1
                session_has_evaluation = True

            # Abstract fallback 率
            deep_read = paper_info.get('deep_read')
            if deep_read:
                workspace_stats['papers_with_deep_read'] += 1
                if deep_read.get('source') == 'abstract_fallback':
                    workspace_stats['abstract_fallback_count'] += 1

        if session_has_evaluation:
            sessions_with_evaluation += 1

        # 统计搜索历史
        search_history = workspace.get('search_history', [])
        for history in search_history:
            search_history_total += 1
            if history.get('relaxed'):
                search_history_relaxed += 1

    # ========== 第五部分：聚合事件流中的 token ==========

    tokens_from_events = {'input': 0, 'output': 0}

    for session_dir in session_dirs:
        if session_dir.name == "smoke2":
            continue

        events = load_session_events(session_dir)

        for event in events:
            if event.get('event_type') != 'runtime_event':
                continue

            metadata = event.get('metadata', {})
            detail_content = event.get('detail_content', {})

            # 中文注释：token 提取的真实事件结构（2026-09-10 核实）：
            # - 真实数据中，token 字段只存在于 metadata 顶层（不在 detail_content 中）
            # - detail_content 恒为空 dict {}
            # - metadata.metadata 有同值镜像但代码不读取它
            # 采用 OR 逻辑：优先取 metadata，缺失时才回退 detail_content（防后人重复计数）

            # 优先从 metadata 提取 token（true path，真实数据在这里）
            input_tokens = metadata.get('input_tokens', 0)
            output_tokens = metadata.get('output_tokens', 0)

            # 回退逻辑：如果 metadata 没有，再从 detail_content 尝试
            if input_tokens == 0:
                input_tokens = detail_content.get('input_tokens', 0)
            if output_tokens == 0:
                output_tokens = detail_content.get('output_tokens', 0)

            tokens_from_events['input'] += input_tokens
            tokens_from_events['output'] += output_tokens

    # ========== 第六部分：计算最终指标和聚合 ==========

    # 处理工具错误的 top-3
    for stage in tool_stats:
        # 计算失败率
        calls = tool_stats[stage]['calls']
        failed = tool_stats[stage]['failed']

        if calls > 0:
            tool_stats[stage]['failure_rate'] = failed / calls

        # 计算耗时 p50/p95
        durations = tool_stats[stage]['durations']
        if durations:
            tool_stats[stage]['duration_s_p50'] = percentile(durations, 50)
            tool_stats[stage]['duration_s_p95'] = percentile(durations, 95)

        # 处理错误信息（去重计数后取 top-3）
        error_counter = Counter(tool_stats[stage]['top_errors'])
        top_errors = []
        for error_text, count in error_counter.most_common(3):
            top_errors.append({'text': error_text, 'count': count})

        tool_stats[stage]['top_errors'] = top_errors

        # 删除临时字段
        if 'durations' in tool_stats[stage]:
            del tool_stats[stage]['durations']

    # 计算 turn 耗时分位数
    turn_duration_stats = {
        'p50': 0.0,
        'p95': 0.0,
        'max': 0.0,
    }

    if turn_durations:
        turn_duration_stats['p50'] = percentile(turn_durations, 50)
        turn_duration_stats['p95'] = percentile(turn_durations, 95)
        turn_duration_stats['max'] = max(turn_durations)

    # 计算 LLM 操作的耗时分位数
    llm_duration_by_op_stats = {}
    for operation, durations_ms in llm_durations_by_op.items():
        if durations_ms:
            llm_duration_by_op_stats[operation] = {
                'p50': percentile(durations_ms, 50),
                'p95': percentile(durations_ms, 95),
                'max': max(durations_ms),
            }

    # 计算比率和覆盖率
    total_turns = sum(turn_statuses.values())
    completed_turns = turn_statuses.get('completed', 0)
    completed_rate = completed_turns / total_turns if total_turns > 0 else 0.0

    cancel_rate = cancelled_turns / total_turns if total_turns > 0 else 0.0

    # 失败后重试率
    failed_turns = turn_statuses.get('failed', 0)
    retry_after_fail_rate = failed_then_retried / failed_turns if failed_turns > 0 else 0.0

    # 工作区统计率

    # 中文注释：下载成功率走"尝试口径"——分母是真正发起过下载或精读的论文数，
    # 不是工作区里累积的全部论文数。分子是这些论文里全文真的落到本地缓存的篇数。
    # 例：历史数据里只对 2 篇论文动过手（1 篇 arXiv 下成功、1 篇 ACM 被 403 拒），
    # 那么成功率是 1/2 = 50%，而不是 1/244 = 0.4%（后者把 242 篇用户从没要求下载的
    # 检索结果也算进了分母）。
    download_succeeded = 0
    for session_name, attempted_paper_id in download_attempt_papers:
        info = workspace_papers_by_session.get(session_name, {}).get(attempted_paper_id)
        if info and info.get('fulltext_cached'):
            download_succeeded += 1

    download_attempted_total = len(download_attempt_papers)
    download_success_rate = (
        download_succeeded / download_attempted_total
        if download_attempted_total > 0 else 0.0
    )

    # 中文注释：abstract_fallback 率同样走尝试口径——分母是真正发起过精读的论文数。
    # 注意历史样本只有 2 篇，所以这个比率天然波动极大（1/2 就是 50%），
    # 报告里必须连同样本量一起看，不能单独拿这个数字下结论。
    fallback_from_attempts = 0
    for session_name, attempted_paper_id in deep_read_attempt_papers:
        info = workspace_papers_by_session.get(session_name, {}).get(attempted_paper_id)
        if info and (info.get('deep_read') or {}).get('source') == 'abstract_fallback':
            fallback_from_attempts += 1

    deep_read_attempted_total = len(deep_read_attempt_papers)
    abstract_fallback_rate = (
        fallback_from_attempts / deep_read_attempted_total
        if deep_read_attempted_total > 0 else 0.0
    )

    search_relaxed_rate = (
        search_history_relaxed / search_history_total
        if search_history_total > 0 else 0.0
    )

    # 中文注释：评估覆盖率按会话级算（与 eval/README.md 的定义一致）：
    # 有工作区的会话里，有多少个至少评估过一篇论文。
    evaluation_coverage = (
        sessions_with_evaluation / sessions_with_workspace
        if sessions_with_workspace > 0 else 0.0
    )

    # LLM 统计
    llm_total_calls = len(llm_calls)
    llm_retry_rate = llm_retries / llm_total_calls if llm_total_calls > 0 else 0.0

    # ========== 第七部分：构造最终 JSON ==========

    # 生成时间戳
    now = datetime.utcnow().isoformat() + 'Z'

    # 构造成本估算（使用占位价格）
    cost_estimate = 0.0
    # 从 config 导入价目表，这里简化处理
    # 实际应该从 config 导入，但为了避免循环导入，这里用硬编码的占位价格
    price_input = 2.0 / 1_000_000  # 元/token
    price_output = 8.0 / 1_000_000  # 元/token

    total_tokens_from_events = tokens_from_events['input'] + tokens_from_events['output']
    if total_tokens_from_events > 0:
        cost_estimate = (
            tokens_from_events['input'] * price_input +
            tokens_from_events['output'] * price_output
        )

    result = {
        'schema_version': 1,
        'generated_at': now,
        'scope': {
            'sessions_total': len(session_dirs),
            'sessions_real': sum(
                1 for s in session_dirs
                if get_session_title_from_db(sqlite_path, s.name) is None or
                not get_session_title_from_db(sqlite_path, s.name).startswith(eval_session_prefix)
            ),
            'sessions_eval': sum(
                1 for s in session_dirs
                if get_session_title_from_db(sqlite_path, s.name) and
                get_session_title_from_db(sqlite_path, s.name).startswith(eval_session_prefix)
            ),
            'sessions_skipped': skipped_sessions,
            'log_files': [str(p) for p in log_paths if p.exists()],
        },
        'turn_status': {
            'completed': turn_statuses.get('completed', 0),
            'failed': turn_statuses.get('failed', 0),
            'cancelled': turn_statuses.get('cancelled', 0),
            'interrupted': turn_statuses.get('interrupted', 0),
            'total': total_turns,
            'completed_rate': completed_rate,
        },
        'turn_duration_s': turn_duration_stats,
        'tool_stats': {
            'by_stage': dict(tool_stats),
        },
        'llm_stats': {
            'calls_total': llm_total_calls,
            'error_kind_dist': dict(llm_error_kinds),
            'retry_rate': llm_retry_rate,
            'finish_reason_dist': dict(llm_finish_reasons),
            'duration_ms_by_operation': llm_duration_by_op_stats,
        },
        'tokens': {
            'from_events': tokens_from_events,
            'from_logs': llm_tokens_from_logs,
            'cost_estimate_cny': round(cost_estimate, 4),
            'cost_is_estimate': True,
        },
        'workspace_stats': {
            'papers_total': workspace_stats['papers_total'],
            # 中文注释：下面每个比率都连分子分母一起给出。原因是有些指标样本极小
            # （比如精读全历史只发生过 2 次），只看一个百分比会严重误导，
            # 必须能看到"这是几除以几"才能判断这个数字能不能用。
            'download_success_rate': download_success_rate,
            'download_succeeded': download_succeeded,
            'download_attempted': download_attempted_total,
            'abstract_fallback_rate': abstract_fallback_rate,
            'abstract_fallback_count': fallback_from_attempts,
            'deep_read_attempted': deep_read_attempted_total,
            'relaxed_search_rate': search_relaxed_rate,
            'evaluation_coverage': evaluation_coverage,
            'sessions_with_evaluation': sessions_with_evaluation,
            'sessions_with_workspace': sessions_with_workspace,
            'papers_with_evaluation': workspace_stats['papers_with_evaluation'],
            'papers_with_deep_read': workspace_stats['papers_with_deep_read'],
            'papers_with_fulltext': workspace_stats['papers_with_fulltext'],
        },
        'user_signals': {
            'cancel_rate': cancel_rate,
            'retry_after_fail_rate': retry_after_fail_rate,
            'turns_by_day': dict(by_day_turns),
        },
        'traffic_split': {
            'real': {
                'turn_status': dict(traffic_split['real']['turn_status']),
            },
            'eval': {
                'turn_status': dict(traffic_split['eval']['turn_status']),
            },
        },
        'data_quality': {
            'log_lines_parsed': llm_parsed_count,
            'log_lines_failed': llm_failed_count,
            'notes': [],
        },
    }

    # 添加数据质量备注
    if skipped_sessions:
        result['data_quality']['notes'].append(f"跳过了 {len(skipped_sessions)} 个垃圾目录：{skipped_sessions}")

    if llm_failed_count > 0:
        result['data_quality']['notes'].append(
            f"日志解析失败 {llm_failed_count} 行（可能是格式变更）"
        )

    # 按日期排序 turns_by_day
    result['user_signals']['turns_by_day'] = {
        k: dict(v)
        for k, v in sorted(result['user_signals']['turns_by_day'].items())
    }

    return result
