"""LLM-as-judge 评估模块。

这个模块提供五个核心评估函数，通过 LLM 对论文相关性、答案忠实性、综述质量、
完整性进行打分，以及对校准集进行评估。所有评估都返回结构化结果，包含状态码、
详细指标和 token 用量统计。

主要函数：
- run_relevance_judge: 判断论文与研究主题的相关性（0-2分）
- run_faithfulness_judge: 判断最终答案是否被引用论文支撑（1-5分）
- run_review_judge: 判断综述内容是否被引用论文支撑（1-5分）
- run_completeness_judge: 判断答案完整性和组织质量（1-5分）
- run_calibration: 对校准集进行评估，对比 LLM 分数与人工标注

所有 LLM 调用都采用非流式模式，获得结构化 JSON 输出，支持自动重试和容错。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from src.llm.config import ModelConfig, SystemConfig
from src.llm.factory import make_provider
from src.llm.base import normalize_token_usage
from src.utils import get_logger
from src.utils.llm_json import parse_llm_json

from eval import config

logger = get_logger(__name__)

JsonObject = dict[str, Any]


# ============================= 数据结构 =============================


@dataclass(slots=True)
class JudgeDeps:
    """裁判模块的运行时依赖。

    包含已初始化的 LLM provider、模型名、并发限制和 token 用量统计器。
    所有评估函数都接收这个依赖对象，确保共享同一个 provider 实例和用量累计。

    Attributes:
        provider: 装配好的 LLM provider（eval_judge 档位）
        model_name: 实际使用的模型名
        semaphore_limit: 并发上限
        usage_total: 累计 token 用量字典
    """
    provider: Any
    model_name: str
    semaphore_limit: int
    usage_total: dict


# ============================= 依赖初始化 =============================


async def build_judge_deps() -> dict:
    """装配 judge 依赖。

    从 config/model.json 读取模型配置，解析 eval_judge 档位（不存在时回退到
    default_agent），创建 LLM provider 实例并返回依赖对象。

    Returns:
        成功: {"status": "ok", "deps": JudgeDeps(...)}
        失败: {"status": "failed", "reason": "人话原因"}
    """
    try:
        # 中文注释：读取全局模型配置
        import json as _json
        from pathlib import Path as _Path

        model_config_path = _Path("config/model.json")
        if not model_config_path.exists():
            return {"status": "failed", "reason": "config/model.json 文件不存在"}

        model_data = _json.loads(model_config_path.read_text(encoding="utf-8"))
        system_config = SystemConfig.load()
        config_obj = ModelConfig.from_dict(model_data, system_config)

        # 中文注释：解析 eval_judge 档位（不存在时自动回退到 default_agent）
        agent = config_obj.resolve_agent(config.EVAL_JUDGE_AGENT)

        # 中文注释：解析 provider 配置并创建实例
        provider_name, provider_config = config_obj.resolve_provider_config(agent)

        # 中文注释：用 make_provider 工厂函数创建 provider 快照
        snapshot = make_provider(config_obj, config.EVAL_JUDGE_AGENT)

        # 中文注释：创建依赖对象
        deps = JudgeDeps(
            provider=snapshot.provider,
            model_name=snapshot.model,
            semaphore_limit=config.JUDGE_MAX_CONCURRENCY,
            usage_total={"input_tokens": 0, "output_tokens": 0},
        )

        logger.info(
            "judge 依赖装配成功",
            extra={
                "model": deps.model_name,
                "semaphore_limit": deps.semaphore_limit,
            },
        )

        return {"status": "ok", "deps": deps}

    except Exception as exc:
        logger.exception("judge 依赖装配失败")
        return {"status": "failed", "reason": f"装配失败：{exc}"}


# ============================= 辅助函数 =============================


async def _call_judge_llm(deps: JudgeDeps, prompt: str, max_retries: int = 1) -> dict:
    """调用 LLM 获取 JSON 结果，包含自动重试和容错。

    Args:
        deps: JudgeDeps 依赖
        prompt: 完整的 prompt 文本（应该要求只输出 JSON）
        max_retries: 如果第一次输出解析失败，最多重试几次

    Returns:
        成功: {"status": "ok", "data": <parsed_json>, "usage": {...}}
        LLM 调用失败: {"status": "failed", "reason": "..."}
        JSON 解析失败: {"status": "judge_failed", "reason": "..."}
    """
    attempt = 0
    current_prompt = prompt

    while attempt <= max_retries:
        try:
            # 中文注释：调用非流式 chat 接口获取 JSON 输出
            response = await deps.provider.chat(
                messages=[{"role": "user", "content": current_prompt}],
                temperature=config.JUDGE_TEMPERATURE,
            )

            # 中文注释：处理 LLM 调用本身的错误
            if not response.ok:
                error_msg = response.error_kind or "unknown_error"
                return {
                    "status": "failed",
                    "reason": f"LLM 调用失败（{error_msg}）",
                }

            # 中文注释：累加 token 用量
            if response.usage:
                normalized = normalize_token_usage(response.usage)
                deps.usage_total["input_tokens"] += normalized.get(
                    "input_tokens", 0
                )
                deps.usage_total["output_tokens"] += normalized.get(
                    "output_tokens", 0
                )

            content = response.content or ""

            # 中文注释：两级容错方案：先尝试整体 json.loads，失败则正则抠第一个 {...} 块
            try:
                result = json.loads(content)
                return {
                    "status": "ok",
                    "data": result,
                    "usage": dict(deps.usage_total),
                }
            except json.JSONDecodeError:
                # 中文注释：尝试正则匹配第一个 JSON 块
                match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content)
                if match:
                    try:
                        result = json.loads(match.group())
                        return {
                            "status": "ok",
                            "data": result,
                            "usage": dict(deps.usage_total),
                        }
                    except json.JSONDecodeError:
                        pass

                # 中文注释：如果有重试次数，追加错误提示重新调用
                if attempt < max_retries:
                    attempt += 1
                    current_prompt = (
                        prompt
                        + "\n\n上次输出不是合法 JSON，请只输出 JSON 格式的结果，不包含任何其他文本。"
                    )
                    continue

                return {
                    "status": "judge_failed",
                    "reason": f"LLM 输出无法解析为 JSON（第 {attempt + 1} 次尝试仍失败）",
                }

        except Exception as exc:
            logger.exception("judge LLM 调用异常")
            return {"status": "failed", "reason": f"调用异常：{exc}"}

    return {
        "status": "judge_failed",
        "reason": "超过最大重试次数仍未成功",
    }


async def _call_llm_with_semaphore(
    deps: JudgeDeps, prompt: str, semaphore: asyncio.Semaphore, max_retries: int = 1
) -> dict:
    """带信号量限流的 LLM 调用包装。"""
    async with semaphore:
        return await _call_judge_llm(deps, prompt, max_retries)


# ============================= 相关性评估 =============================


async def run_relevance_judge(
    deps,
    *,
    case_id: str,
    topic: str,
    concept_groups: list[list[str]],
    papers: list[dict],
) -> dict:
    """判断论文与研究主题的相关性。

    一次批量调用判多篇论文的相关性。相关性分为三个等级：
    - 0: 论文与主题无关或仅靠关键词碰瓷
    - 1: 同大领域或相邻问题，有背景价值
    - 2: 直接命中研究主题且涉及至少一个概念组的核心工作

    Args:
        deps: JudgeDeps 依赖
        case_id: 用例 ID
        topic: 研究主题描述
        concept_groups: 概念分组（组间 AND、组内 OR）
        papers: 论文列表 [{"title", "abstract", "year", "venue"}, ...]

    Returns:
        成功: {
            "status": "ok",
            "case_id": ...,
            "scores": [{"index": 1, "score": 0, "reason": "..."}, ...],
            "usage": {"input_tokens": n, "output_tokens": n}
        }
        失败: {"status": "failed"|"judge_failed", "reason": "..."}
    """
    # 中文注释：截断 abstract 到配置长度
    truncated_papers = []
    for paper in papers:
        p = dict(paper)
        if p.get("abstract"):
            p["abstract"] = p["abstract"][: config.ABSTRACT_TRUNCATE_CHARS]
        truncated_papers.append(p)

    # 中文注释：构造 prompt
    concept_text = " ".join(
        [f"【{i + 1}】" + " 或 ".join(group) for i, group in enumerate(concept_groups)]
    )

    papers_text = "\n".join(
        [
            f"{i + 1}. 【{p.get('title', '')}】\n"
            f"   摘要：{p.get('abstract', '')}\n"
            f"   年份：{p.get('year', 'N/A')} | 会议/期刊：{p.get('venue', 'N/A')}"
            for i, p in enumerate(truncated_papers)
        ]
    )

    prompt = f"""请评价以下论文与研究主题的相关性。

【研究主题】{topic}

【核心概念分组】{concept_text}

【待评论文列表】
{papers_text}

【评分标准】
0 分：论文与主题完全无关，或仅因为关键词出现而看起来相关（"碰瓷"）
1 分：论文涉及同一大领域或相邻问题，可作为背景知识参考，但不是直接研究对象
2 分：论文直接解决或贡献于研究主题，且涉及上述至少一个概念组的核心工作

【输出要求】
请只输出 JSON 格式的评分结果，格式如下：
{{
  "scores": [
    {{"index": 1, "score": 0, "reason": "此论文讨论的是...与研究主题无关"}},
    {{"index": 2, "score": 2, "reason": "该论文直接针对...问题提出了..."}}
  ]
}}

每个 score 对象必须包含 index（论文序号）、score（0/1/2）、reason（一句话理由）。"""

    # 中文注释：调用 judge LLM
    result = await _call_judge_llm(deps, prompt, max_retries=1)

    if result["status"] != "ok":
        return result

    # 中文注释：验证返回结果的格式和内容
    data = result["data"]
    scores = data.get("scores", [])

    # 中文注释：检查分数数量是否与论文数量一致
    if len(scores) != len(papers):
        logger.warning(
            "相关性评估分数数量不匹配",
            extra={"expected": len(papers), "got": len(scores), "case_id": case_id},
        )
        return {
            "status": "judge_failed",
            "reason": f"返回分数数量({len(scores)})与论文数量({len(papers)})不匹配",
        }

    # 中文注释：验证每个分数的有效性
    for score_obj in scores:
        score_val = score_obj.get("score")
        if score_val not in (0, 1, 2):
            logger.warning(
                "相关性评估分数无效",
                extra={"score": score_val, "case_id": case_id},
            )
            return {
                "status": "judge_failed",
                "reason": f"分数无效：{score_val}（应为 0/1/2）",
            }

    # 中文注释：检查 index 覆盖
    indices = sorted([s.get("index") for s in scores])
    expected_indices = list(range(1, len(papers) + 1))
    if indices != expected_indices:
        logger.warning(
            "相关性评估 index 不连续",
            extra={"got": indices, "expected": expected_indices, "case_id": case_id},
        )
        return {
            "status": "judge_failed",
            "reason": f"论文编号不连续或缺失",
        }

    return {
        "status": "ok",
        "case_id": case_id,
        "scores": scores,
        "usage": result["usage"],
    }


# ============================= 忠实性评估 =============================


async def run_faithfulness_judge(
    deps,
    *,
    case_id: str,
    final_answer: str,
    papers_meta: dict[str, dict],
) -> dict:
    """判断最终答案是否被引用论文支撑（忠实性评估）。

    采用两阶段流程：
    1. 从答案中抽取带引用标注的论断
    2. 逐条判断论断的支撑度（1-5分）

    Args:
        deps: JudgeDeps 依赖
        case_id: 用例 ID
        final_answer: 最终答案文本
        papers_meta: 论文元数据 {paper_id: {"title", "abstract", "deep_read_summary"(可空)}, ...}

    Returns:
        成功: {
            "status": "ok",
            "case_id": ...,
            "claims": [{"claim": "...", "paper_id": "...", "score": 1-5, "evidence": "..."}, ...],
            "avg_score": float,
            "usage": {"input_tokens": n, "output_tokens": n}
        }
        无任何论断时: {
            "status": "ok",
            "case_id": ...,
            "claims": [],
            "avg_score": None,
            ...
        }
        失败: {"status": "failed"|"judge_failed", "reason": "..."}
    """
    # 第一阶段：抽取论断
    extract_prompt = f"""请从以下答案中抽取所有带引用标注的论断。

【答案文本】
{final_answer}

【任务要求】
- 只抽取明确带引用标注的论断（如"[引用论文 ID]"或"[ID]"的形式）
- 最多抽取 {config.MAX_CLAIMS_PER_CASE} 条
- 对每条论断，提取其完整表述和所引用的论文 ID

【输出要求】
请只输出 JSON 格式，包含 claims 列表，每个 claim 包含：
- claim: 论断的完整文本
- paper_id: 引用的论文 ID
格式如下：
{{
  "claims": [
    {{"claim": "该方法在 ImageNet 上达到 95% 准确率", "paper_id": "paper_001"}},
    {{"claim": "使用了 Transformer 架构", "paper_id": "paper_002"}}
  ]
}}"""

    extract_result = await _call_judge_llm(deps, extract_prompt, max_retries=1)
    if extract_result["status"] != "ok":
        return {
            "status": extract_result["status"],
            "reason": extract_result.get("reason", "论断抽取失败"),
        }

    extract_data = extract_result["data"]
    claims_to_check = extract_data.get("claims", [])

    # 中文注释：如果没有任何论断，直接返回
    if not claims_to_check:
        return {
            "status": "ok",
            "case_id": case_id,
            "claims": [],
            "avg_score": None,
            "usage": extract_result["usage"],
        }

    # 第二阶段：并发判断每条论断的支撑度
    semaphore = asyncio.Semaphore(deps.semaphore_limit)
    check_tasks = [
        _check_claim_faithfulness(
            deps, semaphore, claim, papers_meta
        )
        for claim in claims_to_check
    ]

    checked_claims = await asyncio.gather(*check_tasks)

    # 中文注释：计算平均分数
    valid_scores = [c["score"] for c in checked_claims if c["score"] is not None]
    avg_score = sum(valid_scores) / len(valid_scores) if valid_scores else None

    return {
        "status": "ok",
        "case_id": case_id,
        "claims": checked_claims,
        "avg_score": avg_score,
        "usage": extract_result["usage"],
    }


async def _check_claim_faithfulness(deps, semaphore, claim_obj, papers_meta):
    """检查单条论断的支撑度。"""
    claim = claim_obj.get("claim", "")
    paper_id = claim_obj.get("paper_id", "")

    # 中文注释：如果引用的论文不存在，直接标记为 score=1（矛盾/编造）
    if paper_id not in papers_meta:
        return {
            "claim": claim,
            "paper_id": paper_id,
            "score": 1,
            "evidence": f"引用了不存在的论文：{paper_id}",
        }

    paper_info = papers_meta[paper_id]
    title = paper_info.get("title", "")
    abstract = paper_info.get("abstract", "")
    deep_read = paper_info.get("deep_read_summary", "")

    # 中文注释：截断摘要
    abstract = abstract[: config.ABSTRACT_TRUNCATE_CHARS] if abstract else ""

    # 中文注释：准备判断的 prompt
    evidence_text = f"摘要：{abstract}"
    if deep_read:
        evidence_text += f"\n\n精读总结：{deep_read}"

    check_prompt = f"""请判断以下论断是否被论文内容支撑。

【论文信息】
标题：{title}
{evidence_text}

【待检论断】
"{claim}"

【评分标准】
5 分：完全可由论文摘要或精读内容支撑，论断表述准确
4 分：基本被支撑，但细节或表述有出入
3 分：方向正确但夸大了，或数据表述不精确
2 分：只有浅层关联，实质没有直接依据
1 分：与论文内容矛盾或明显编造

【输出要求】
请只输出 JSON 格式：
{{
  "score": 5,
  "evidence": "论文明确指出该方法在...取得了...的结果，与论断相符"
}}"""

    result = await _call_llm_with_semaphore(
        deps, check_prompt, semaphore, max_retries=1
    )

    if result["status"] != "ok":
        # 中文注释：LLM 调用失败也记录论断，标记为不可判断
        return {
            "claim": claim,
            "paper_id": paper_id,
            "score": None,
            "evidence": f"评估失败：{result.get('reason', '未知错误')}",
        }

    data = result["data"]
    score = data.get("score")
    evidence = data.get("evidence", "")

    # 中文注释：验证分数有效性
    if not isinstance(score, int) or score not in range(1, 6):
        score = None

    return {
        "claim": claim,
        "paper_id": paper_id,
        "score": score,
        "evidence": evidence,
    }


# ============================= 综述质量评估 =============================


async def run_review_judge(
    deps,
    *,
    case_id: str,
    review_text: str,
    papers_meta: list[dict],
) -> dict:
    """判断综述质量与内容支撑度。

    从综述中抽查最多 5 个自然小节，判断每个小节的内容是否被引用论文支撑。

    Args:
        deps: JudgeDeps 依赖
        case_id: 用例 ID
        review_text: 综述全文
        papers_meta: 综述引用的论文列表 [{"title", "abstract"}, ...]

    Returns:
        成功: {
            "status": "ok",
            "case_id": ...,
            "sections_checked": [{"section_title": "...", "score": 1-5, "reason": "..."}, ...],
            "faithfulness_score": 1.0-5.0,
            "usage": {"input_tokens": n, "output_tokens": n}
        }
        失败: {"status": "failed"|"judge_failed", "reason": "..."}
    """
    # 中文注释：截断综述文本
    review_text = review_text[: config.REVIEW_TRUNCATE_CHARS]

    # 中文注释：截断论文摘要
    papers_text = "\n".join(
        [
            f"【{i + 1}】{p.get('title', '')}\n"
            f"摘要：{p.get('abstract', '')[:config.ABSTRACT_TRUNCATE_CHARS]}"
            for i, p in enumerate(papers_meta)
        ]
    )

    prompt = f"""请评价以下综述的质量和内容支撑度。

【综述全文】
{review_text}

【引用论文列表】
{papers_text}

【任务】
1. 从综述中挑选最多 5 个关键小节进行抽查
2. 对每个小节判断其内容能否被引用论文摘要支撑
3. 使用 1~5 分制（见下方标准）
4. 给整体综述一个忠实性总分

【评分标准】（针对每个小节）
5 分：完全被引用论文支撑，陈述准确
4 分：基本被支撑，但有细节出入
3 分：方向正确但表述不够准确或有夸大
2 分：只有浅层关联，缺乏实质支撑
1 分：与论文内容矛盾或编造

【输出要求】
请只输出 JSON 格式：
{{
  "sections_checked": [
    {{"section_title": "介绍章节", "score": 4, "reason": "主要观点被论文支撑，细节表述略有调整"}},
    {{"section_title": "方法创新", "score": 3, "reason": "方向正确但表述略显夸大"}}
  ],
  "faithfulness_score": 3.5,
  "overall_reason": "综述总体质量不错但在某些细节上需要更严谨"
}}"""

    result = await _call_judge_llm(deps, prompt, max_retries=1)
    if result["status"] != "ok":
        return result

    data = result["data"]
    sections = data.get("sections_checked", [])
    faith_score = data.get("faithfulness_score")

    # 中文注释：验证综合分数
    if not isinstance(faith_score, (int, float)) or not (1 <= faith_score <= 5):
        logger.warning(
            "综述忠实性分数无效",
            extra={"score": faith_score, "case_id": case_id},
        )
        faith_score = None

    return {
        "status": "ok",
        "case_id": case_id,
        "sections_checked": sections,
        "faithfulness_score": faith_score,
        "usage": result["usage"],
    }


# ============================= 完整性评估 =============================


async def run_completeness_judge(
    deps,
    *,
    case_id: str,
    user_request: str,
    judge_brief: str,
    final_answer: str,
) -> dict:
    """判断答案的完整性和组织质量。

    两个维度：
    1. 完整性：逐条判断用户需求要点是否被覆盖（covered/partial/missed）
       分数由代码从 coverage 数组计算，公式写死在代码里
    2. 组织质量：评估答案的结构、可读性和引用规范

    Args:
        deps: JudgeDeps 依赖
        case_id: 用例 ID
        user_request: 用户原始需求
        judge_brief: 用例里的"需求要点清单"（如"①…②…③…"）
        final_answer: 最终答案文本

    Returns:
        成功: {
            "status": "ok",
            "case_id": ...,
            "coverage": [{"point": "要点1", "verdict": "covered"|"partial"|"missed"}, ...],
            "completeness_score": 1.0-5.0,
            "organization_score": 1.0-5.0,
            "organization_reason": "...",
            "usage": {"input_tokens": n, "output_tokens": n}
        }
        失败: {"status": "failed"|"judge_failed", "reason": "..."}
    """
    prompt = f"""请评价以下答案的完整性和组织质量。

【用户原始需求】
{user_request}

【需求要点清单】（用户需求的关键要点分解）
{judge_brief}

【最终答案】
{final_answer}

【任务】
1. 对需求要点清单中的每个要点，判断答案是否覆盖了它
   - covered: 充分覆盖
   - partial: 部分覆盖
   - missed: 未覆盖或未提及

2. 评估答案的组织质量（结构、可读性、引用标注规范）：1~5 分

【输出要求】
请只输出 JSON 格式：
{{
  "coverage": [
    {{"point": "用户需求第1点的内容", "verdict": "covered"}},
    {{"point": "用户需求第2点的内容", "verdict": "partial"}},
    {{"point": "用户需求第3点的内容", "verdict": "missed"}}
  ],
  "organization_score": 3.0,
  "organization_reason": "答案结构清晰，但引用标注不够规范"
}}"""

    result = await _call_judge_llm(deps, prompt, max_retries=1)
    if result["status"] != "ok":
        return result

    data = result["data"]
    coverage = data.get("coverage", [])
    organization_score = data.get("organization_score")
    org_reason = data.get("organization_reason", "")

    # 中文注释：从 coverage 数组计算完整性分数
    # 公式：得分 = 1 + 4 * (covered数 + 0.5*partial数) / 要点总数，四舍五入到 0.5
    completeness_score = None
    if coverage:
        covered_count = sum(1 for c in coverage if c.get("verdict") == "covered")
        partial_count = sum(1 for c in coverage if c.get("verdict") == "partial")
        total_count = len(coverage)

        # 中文注释：使用公式计算分数
        raw_score = 1 + 4 * (covered_count + 0.5 * partial_count) / total_count

        # 中文注释：四舍五入到 0.5 的倍数（half-up 舍入规则，不使用 Python 的银行家舍入）
        # 公式：乘以 2 后加 0.5，再取整，最后除以 2
        # 例如：raw_score=1.25 -> (1.25*2 + 0.5)//1 = 3//1 = 3 -> 3/2 = 1.5（正确）
        completeness_score = int(raw_score * 2 + 0.5) / 2

        # 中文注释：钳制到 [1, 5] 范围
        completeness_score = max(1.0, min(5.0, completeness_score))

        logger.info(
            "完整性分数已计算",
            extra={
                "case_id": case_id,
                "covered": covered_count,
                "partial": partial_count,
                "total": total_count,
                "score": completeness_score,
            },
        )

    # 中文注释：验证组织质量分数的有效性
    if not isinstance(organization_score, (int, float)) or not (1 <= organization_score <= 5):
        logger.warning(
            "组织质量分数无效",
            extra={"score": organization_score, "case_id": case_id},
        )
        organization_score = None

    return {
        "status": "ok",
        "case_id": case_id,
        "coverage": coverage,
        "completeness_score": completeness_score,
        "organization_score": organization_score,
        "organization_reason": org_reason,
        "usage": result["usage"],
    }


# ============================= 校准评估 =============================


async def run_calibration(deps, *, calibration_cases: list[dict]) -> dict:
    """对校准集进行评估并计算统计指标。

    处理 human_score 为 null 的条目（跳过），对已标注的用例调用相关性评估，
    然后计算：
    - ±1 一致率：|judge_score - human_score| <= 1 的比例
    - 精确一致率：judge_score == human_score 的比例
    - Spearman 秩相关（样本 < 3 时为 None）
    - 3x3 混淆矩阵

    Args:
        deps: JudgeDeps 依赖
        calibration_cases: judge_calibration.json 的 cases 列表

    Returns:
        成功（有标注）: {
            "status": "ok",
            "labeled": n,
            "unlabeled": m,
            "agreement_within_1": float,
            "exact_match": float,
            "spearman": float|None,
            "confusion": {...},
            "usage": {"input_tokens": n, "output_tokens": n}
        }
        成功（无标注）: {
            "status": "ok",
            "labeled": 0,
            "unlabeled": m,
            "note": "校准集尚未人工标注，跳过校准",
            "usage": {"input_tokens": 0, "output_tokens": 0}
        }
        失败: {"status": "failed", "reason": "..."}
    """
    # 中文注释：分离有标注和无标注的用例
    labeled_cases = [c for c in calibration_cases if c.get("human_score") is not None]
    unlabeled_cases = [
        c for c in calibration_cases if c.get("human_score") is None
    ]

    # 中文注释：如果没有任何标注，直接返回
    if not labeled_cases:
        return {
            "status": "ok",
            "labeled": 0,
            "unlabeled": len(unlabeled_cases),
            "note": "校准集尚未人工标注，跳过校准",
            "agreement_within_1": None,
            "exact_match": None,
            "spearman": None,
            "confusion": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    # 中文注释：对每个标注过的用例调用 run_relevance_judge
    judge_results = []
    for case in labeled_cases:
        paper = case.get("paper")
        if not paper:
            continue

        # 中文注释：单篇论文也按批量接口传 1 条
        result = await run_relevance_judge(
            deps,
            case_id=case.get("case_id", ""),
            topic=case.get("topic", ""),
            concept_groups=case.get("concept_groups", []),
            papers=[paper],
        )

        if result["status"] != "ok":
            logger.warning(
                "校准集评估失败",
                extra={
                    "case_id": case.get("case_id", ""),
                    "reason": result.get("reason", ""),
                },
            )
            continue

        # 中文注释：从结果中提取分数
        scores = result.get("scores", [])
        if scores:
            judge_score = scores[0].get("score")
            judge_results.append(
                {
                    "case_id": case.get("case_id", ""),
                    "human_score": case.get("human_score"),
                    "judge_score": judge_score,
                }
            )

    # 中文注释：如果无法获得有效的 judge 分数，返回失败
    if not judge_results:
        return {
            "status": "failed",
            "reason": "无法从校准集获得有效的评估分数",
        }

    # 中文注释：计算统计指标
    human_scores = [r["human_score"] for r in judge_results]
    judge_scores = [r["judge_score"] for r in judge_results]

    # 中文注释：±1 一致率
    within_1 = sum(
        1
        for h, j in zip(human_scores, judge_scores)
        if abs(j - h) <= 1
    )
    agreement_within_1 = within_1 / len(judge_results) if judge_results else 0

    # 中文注释：精确一致率
    exact = sum(
        1 for h, j in zip(human_scores, judge_scores) if j == h
    )
    exact_match = exact / len(judge_results) if judge_results else 0

    # 中文注释：Spearman 秩相关（样本少于 3 时无法计算）
    spearman = None
    if len(judge_results) >= 3:
        try:
            from scipy.stats import spearmanr

            corr, _ = spearmanr(human_scores, judge_scores)
            spearman = float(corr) if not (isinstance(corr, float) and corr != corr) else None
        except Exception as exc:
            logger.warning("Spearman 秩相关计算失败", extra={"error": str(exc)})

    # 中文注释：3x3 混淆矩阵（行 = human，列 = judge）
    confusion = {}
    for h_score in (0, 1, 2):
        confusion[str(h_score)] = {str(j): 0 for j in (0, 1, 2)}

    for h, j in zip(human_scores, judge_scores):
        if h in (0, 1, 2) and j in (0, 1, 2):
            confusion[str(h)][str(j)] += 1

    return {
        "status": "ok",
        "labeled": len(judge_results),
        "unlabeled": len(unlabeled_cases),
        "agreement_within_1": agreement_within_1,
        "exact_match": exact_match,
        "spearman": spearman,
        "confusion": confusion,
        "usage": dict(deps.usage_total),
    }


# ============================= 成本估算 =============================


def estimate_cost_cny(usage_total: dict, model_name: str | None = None) -> float:
    """估算 LLM 调用成本（人民币）。

    根据 config.PRICE_TABLE_CNY_PER_M_TOKENS 的价目表计算。
    如果模型不在价目表里，回退到价目表的第一个条目。
    估算值仅供参考，实际价格以服务商官方价目为准（见 config.py）。

    Args:
        usage_total: {"input_tokens": n, "output_tokens": n}
        model_name: 模型名，为 None 时自动使用价目表第一个条目

    Returns:
        估算成本（元）
    """
    # 中文注释：如果没有指定模型名，使用价目表第一个条目作为默认
    if model_name is None or model_name not in config.PRICE_TABLE_CNY_PER_M_TOKENS:
        if not config.PRICE_TABLE_CNY_PER_M_TOKENS:
            logger.warning("价目表为空，成本估算返回 0.0")
            return 0.0

        # 中文注释：回退到价目表的第一个模型
        model_name = next(iter(config.PRICE_TABLE_CNY_PER_M_TOKENS.keys()))
        logger.info(
            "成本估算使用默认模型",
            extra={"default_model": model_name},
        )

    # 中文注释：获取价目表
    pricing = config.PRICE_TABLE_CNY_PER_M_TOKENS.get(model_name)
    if not pricing:
        logger.warning(
            "模型不在价目表中，成本估算返回 0.0",
            extra={"model": model_name},
        )
        return 0.0

    input_tokens = usage_total.get("input_tokens", 0)
    output_tokens = usage_total.get("output_tokens", 0)

    # 中文注释：计算成本：token 数 / 100 万 × 单价（元/百万 token）
    input_cost = (
        input_tokens / 1_000_000 * pricing.get("input", 0)
    )
    output_cost = (
        output_tokens / 1_000_000 * pricing.get("output", 0)
    )

    total_cost = input_cost + output_cost
    return round(total_cost, 4)
