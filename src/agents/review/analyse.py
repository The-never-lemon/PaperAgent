# 文件作用：在写综述前阅读论文集，提炼共同主题、矛盾和可写方向。
"""综述的分析步骤：读论文集，再做一次领域综合。

这里不是独立子 Agent。综述流水线把已经装配好的模型传进来，
本模块只负责提示词、调用和「输出被截断就加码重试」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.llm import ProviderSnapshot
from src.llm.base import LLMResponse, attach_reasoning, normalize_token_usage
from src.utils import get_logger
from src.utils.llm_json import parse_llm_json

from src.agents.common.contracts import JsonObject
from src.agents.common.prompts import ANALYSE_OVERALL_SYSTEM_PROMPT, ANALYSE_SUBTOPIC_SYSTEM_PROMPT


logger = get_logger(__name__)


# 分析模型一次要产出六到八个字段、每个字段都是一整段论述，输出很容易撞上字数上限。
# 这里最多试 3 次：第一次用档位配的上限，之后每次翻倍，给被截断的输出留够空间。
ANALYSE_MAX_ATTEMPTS = 3

# 重试时 max_tokens 翻倍的上限。设这个天花板，是为了避免模型反复不守格式时，
# 上限被一路翻倍到离谱的数字（既浪费额度，也拖长一次综述的等待时间）。
ANALYSE_MAX_TOKENS_CEILING = 32768

# 子主题分析和全局综合要想完再写出长 JSON，60 秒常常不够。只把这两步的等待放宽，
# 主对话和写作仍用原来的 60 秒。
ANALYSE_CHAT_TIMEOUT_S = 180

# 子主题分析一次送给模型的用户消息大约不超过这么多字。按这个数把论文分成几批，
# 一张论文卡片不从中间切开，避免后半篇论文在这一步就丢失。
SUBTOPIC_BATCH_MAX_CHARS = 12000

# 全局综合不再把子主题分析的长文整段塞进去。每个长字段先收到这个字数，
# 整份用户材料再卡一个总上限。完整分析仍留在综述状态里。
OVERALL_FIELD_MAX_CHARS = 1500
OVERALL_USER_MAX_CHARS = 12000

# 截短时附在末尾，让模型知道后面还有原文，不要把截断当成已经写完。
_CLIP_NOTE = "……（原文更长，完整内容仍留在综述状态里）"

# 子主题分析里需要按批接起来的长文本字段。一致点是列表，合并时单独处理。
_SUBTOPIC_TEXT_FIELDS = ("研究现状", "矛盾点", "研究空白", "时间线演化", "技术方法栈演变")


@dataclass(slots=True)
class AnalyseModelResult:
    """保存一次分析模型调用的结果。

    中文说明：
    parsed 是已经解析出来的 JSON；如果为 None，说明模型不可用、调用失败、被截断
    或格式不对。reason 用简单中文说明失败原因，供综述流水线决定是否中止。
    """

    parsed: JsonObject | None = None
    raw_model_output: str = ""
    reason: str = ""


def _report_usage(response: object, usage_callback: Any | None) -> None:
    """把这次模型调用的真实用量交给流水线，不按文字长度估算。"""

    if not callable(usage_callback):
        return
    usage_callback(normalize_token_usage(getattr(response, "usage", None)))


async def analyse_subtopic(
    *,
    topic: str,
    group: JsonObject,
    llm: ProviderSnapshot | None,
    usage_callback: Any | None = None,
) -> AnalyseModelResult:
    """分析一个子主题，并返回解析后的 JSON。

    中文说明：这个阶段是整个综述里输入最长的一次模型调用——它要把几十篇
    论文的摘要一次性读完，再写出一段段带引用的分析。输入越厚，模型的思考
    就越长，而思考也要从同一个输出上限里扣。所以这里最容易撞上"话说到一半
    额度就没了"的情况：请求本身是成功的，但 JSON 被从中间截断，解析自然失败。
    这类失败原样重发还是同样结果，所以下面用升级输出上限的方式重试
    （见 _chat_json_with_retry）。
    """

    if llm is None:
        return AnalyseModelResult(reason="未配置可用分析模型")
    return await _chat_json_with_retry(
        llm=llm,
        messages=_subtopic_messages(topic=topic, group=group),
        temperature=0.2,
        reasoning_effort="medium",
        usage_callback=usage_callback,
        stage="子主题分析",
        timeout_s=ANALYSE_CHAT_TIMEOUT_S,
    )


async def analyse_overall(
    *,
    topic: str,
    subtopic_analyses: list[JsonObject],
    llm: ProviderSnapshot | None,
    usage_callback: Any | None = None,
) -> AnalyseModelResult:
    """综合所有子主题分析，并返回解析后的 JSON。

    中文说明：这个阶段要产出八个字段、每个字段都是一整段论述，输出量比子
    主题分析更大，同样容易在中途把上限用光，所以也走同一套"额度不够就加码
    重试"的逻辑。
    """

    if llm is None:
        return AnalyseModelResult(reason="未配置可用分析模型")
    return await _chat_json_with_retry(
        llm=llm,
        messages=_overall_messages(topic=topic, subtopic_analyses=subtopic_analyses),
        temperature=0.2,
        reasoning_effort="medium",
        usage_callback=usage_callback,
        stage="全局综合分析",
        timeout_s=ANALYSE_CHAT_TIMEOUT_S,
    )


async def _chat_json_with_retry(
    *,
    llm: ProviderSnapshot,
    messages: list[JsonObject],
    temperature: float,
    reasoning_effort: str,
    usage_callback: Any | None,
    stage: str,
    timeout_s: float | None = None,
) -> AnalyseModelResult:
    """要模型返回一段 JSON，失败时最多重试 ANALYSE_MAX_ATTEMPTS 次。

    中文说明：这里区分两种失败，因为它们的处理方式完全不同。

    第一种是"输出的额度先用完了"（结束原因是达到字数上限）。模型不是不听话，
    是话说到一半没额度了——重发一次同样的请求，结果还是被同一个上限卡住。
    唯一的出路是把上限调高，所以重试时把 max_tokens 加倍。

    第二种是"正常说完但没按格式给 JSON"。这种情况把上一次的坏输出和具体
    解析错误一起还给模型，让它在看得见错误的前提下重写一遍，比原样重发有效。

    两种都失败到底时，把原始输出和最后一次的原因交回去，由上层决定怎么处理。

    请求本身没成功（超时、断连）不走上面两种重试。那种失败重发只会再空等一轮，
    把超时原文贴回去让模型「重写 JSON」也没有用。
    """

    user_chars = sum(len(str(item.get("content") or "")) for item in messages if item.get("role") == "user")
    logger.info(
        "分析提示词已装好",
        extra={"stage": stage, "user_chars": user_chars, "timeout_s": timeout_s or 60},
    )
    attempts = ANALYSE_MAX_ATTEMPTS
    # 第一次用当前档位配的 max_tokens；后面每次重试都翻倍，给被截断的输出留出空间。
    # 同时设一个天花板，避免模型反复不守格式时限，上限被一路顶到离谱的数字。
    # 档位里没配上限时（base 为 None），全程交给 provider 自己决定。
    configured_max_tokens = _resolve_max_tokens(llm)
    last_raw = ""
    last_reason = "模型没有返回可解析的 JSON"
    last_response: LLMResponse | None = None
    for attempt in range(1, attempts + 1):
        max_tokens = _attempt_max_tokens(configured_max_tokens, attempt)
        try:
            response = await llm.provider.chat(
                _attempt_messages(messages, last_response, last_reason, attempt),
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            last_reason = f"分析模型调用失败：{exc}"
            logger.warning(
                "分析模型调用异常，准备重试",
                extra={"stage": stage, "attempt": attempt, "error": str(exc)[:200]},
            )
            continue
        # 失败的那一次也要记用量，否则重试花掉的额度在综述卡片上看不到。
        _report_usage(response, usage_callback)
        last_response = response
        last_raw = str(getattr(response, "content", "") or "")
        outcome = await _parse_response(response, max_tokens=max_tokens)
        if outcome.parsed is not None:
            return outcome
        # 超时、断连这类错误没有可解析的正文。再重试只会把 max_tokens 翻倍后空等。
        if str(getattr(response, "finish_reason", "") or "") == "error":
            logger.warning(
                "分析请求没有成功，不再当成 JSON 写坏了去重试",
                extra={
                    "stage": stage,
                    "attempt": attempt,
                    "finish_reason": "error",
                    "reason": outcome.reason[:200],
                },
            )
            return outcome
        last_reason = outcome.reason
        logger.warning(
            "分析模型没有返回可解析的 JSON，准备重试",
            extra={
                "stage": stage,
                "attempt": attempt,
                "max_tokens": max_tokens,
                "finish_reason": str(getattr(response, "finish_reason", "") or ""),
                "output_chars": len(last_raw),
                "reason": last_reason[:200],
            },
        )
    return AnalyseModelResult(raw_model_output=last_raw, reason=last_reason)


def _resolve_max_tokens(llm: ProviderSnapshot | None) -> int | None:
    """读出当前模型档位配置的输出上限；没配就返回 None（交给 provider 默认值）。

    中文说明：这个值就是"第一次请求"用的上限，后面的重试在它基础上翻倍。
    如果档位里没写，就返回 None，让 provider 用它自己的默认值（Anthropic 协议
    默认 4096，OpenAI 协议默认不发这个字段）。
    """

    generation = getattr(getattr(llm, "provider", None), "generation", None)
    value = getattr(generation, "max_tokens", None)
    try:
        resolved = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    # 上限必须是正数，写 0 或负数等于让模型没法说话。
    return resolved if resolved and resolved > 0 else None


def _attempt_max_tokens(configured_max_tokens: int | None, attempt: int) -> int | None:
    """算出这一次请求该用多大的输出上限。

    中文说明：
    第一次（attempt=1）就用配置里的值。第二次开始翻倍：被截断说明"这个上限不够"，
    那就要真的抬高它，否则重试多少次结果都一样。翻倍不是无限制的——设了天花板
    ANALYSE_MAX_TOKENS_CEILING，万一模型是"格式不守"而不是"被截断"，也不至于
    把上限一路顶到离谱的数字。

    配置里根本没写上限时返回 None，表示这次请求不带这个参数，交给 provider 的
    默认值（Anthropic 协议默认 4096）。
    """

    if configured_max_tokens is None:
        return None
    return min(configured_max_tokens * (2 ** (attempt - 1)), ANALYSE_MAX_TOKENS_CEILING)


def _attempt_messages(
    messages: list[JsonObject],
    last_response: LLMResponse | None,
    last_reason: str,
    attempt: int,
) -> list[JsonObject]:
    """准备这次要发出去的消息。

    中文说明：
    第一次原样发出去。第二次以后，把上一次的坏输出和具体错在哪一起附上——
    模型看不见自己上次写坏在哪，只会照着同样的毛病再写一遍。附带方式是：
    把上次的输出当成"模型自己的回答"放进对话里（assistant 那条），紧接着补一条
    用户消息指出问题并要求重写。这样既符合对话接口的格式要求，也让模型有据可依。
    开了思考档位时，上一轮的思考原文也要挂在那条助手消息上，否则上游会拒绝这次请求。
    """

    last_raw = str(getattr(last_response, "content", "") or "") if last_response is not None else ""
    if attempt == 1 or not last_raw.strip() or last_response is None:
        return list(messages)
    assistant = {"role": "assistant", "content": last_raw}
    attach_reasoning(assistant, last_response)
    return [
        *messages,
        assistant,
        {
            "role": "user",
            "content": (
                f"你上一次的输出无法被解析成要求的 JSON：{last_reason}。\n"
                "请严格按前面的输出规则重新只输出一个合法的 JSON 对象，"
                "不要包含解释文字、不要使用 Markdown 代码块、不要添加或删除任何字段。"
            ),
        },
    ]


async def _parse_response(response: Any, *, max_tokens: int | None = None) -> AnalyseModelResult:
    """把模型响应解析为 JSON。

    中文说明：
    先看这次请求本身成不成功（网络、鉴权、限流这些），再看内容是不是能用的 JSON。
    两者分开判，是为了让上层拿到的失败原因能指明到底卡在哪一步——是"请求没发出去"
    还是"发出去但内容不合格"。
    """

    raw_model_output = str(getattr(response, "content", "") or "")
    if not getattr(response, "ok", False):
        return AnalyseModelResult(
            raw_model_output=raw_model_output,
            reason=raw_model_output or str(getattr(response, "error_kind", "") or "分析模型调用失败"),
        )
    parsed = await _parse_analysis_json(raw_model_output)
    if parsed is None:
        return AnalyseModelResult(
            raw_model_output=raw_model_output,
            reason=_describe_unparsable(response, raw_model_output, max_tokens),
        )
    return AnalyseModelResult(parsed=parsed, raw_model_output=raw_model_output)


async def _parse_analysis_json(text: str) -> JsonObject | None:
    """把模型输出解析成 JSON 对象，解析不动就返回 None。

    中文说明：统一走项目里的 parse_llm_json（工程规范要求所有模型输出共用同一套
    JSON 解析，不允许各写一份正则）。
    """

    payload = await parse_llm_json(text, fallback={})
    return None if "parse_error" in payload else payload


def _describe_unparsable(response: Any, raw_model_output: str, max_tokens: int | None) -> str:
    """说清楚"这段输出为什么不能用"，供上层写进失败原因和日志。

    中文说明：以前不管是哪种原因，一律只报"模型没有返回可解析的 JSON"。
    但最需要看清楚的那句——"输出写到一半被字数上限掐断了"——恰恰是被这句话
    盖住的。所以这里在结尾把真正的原因补上。

    另外，"额度用完"还有一种更彻底的表现：模型把额度全花在思考上，一个字正文
    都没来得及写，此时输出是空的。空输出加上额度用满，同样按这种情况报出来。
    """

    finish_reason = str(getattr(response, "finish_reason", "") or "")
    if finish_reason in {"max_tokens", "length"}:
        used = f"（输出上限 {max_tokens} tokens）" if max_tokens else ""
        return f"模型在达到输出上限{used}时停止，没有写出完整的 JSON"
    if not raw_model_output.strip() and finish_reason == "end_turn":
        return "模型只输出了思考过程，没有输出任何正文内容"
    return "模型没有返回可解析的 JSON"


def _subtopic_messages(*, topic: str, group: JsonObject) -> list[JsonObject]:
    """为单个子主题生成提示词。"""

    return [
        {
            "role": "system",
            # 中文说明：子主题分析的系统规则统一放在 src/agents/common/prompts.py。
            "content": ANALYSE_SUBTOPIC_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "任务": "按子主题分析阅读节点产出的论文结构化摘要",
                    "用户综述主题": topic,
                    "子主题": group["subtopic"],
                    # "检索关键词": group["search_keyword"],
                    # "允许引用的paperId": paper_ids,
                    "输出要求": _analysis_schema_hint(),
                    "论文结构化摘要": group["papers"],
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def _overall_messages(*, topic: str, subtopic_analyses: list[JsonObject]) -> list[JsonObject]:
    """为全局综合分析生成提示词。

    中文说明：子主题分析的全文已经在综述状态里。这里只把每个长字段收成摘录，
    编号列表保持完整，这样模型仍能对上论文，又不会把整段分析一次塞爆。
    """

    field_limit = OVERALL_FIELD_MAX_CHARS
    content = ""
    clipped: list[str] = []
    # 字段先按 1500 字收。若加上编号后整份材料仍超过总上限，就把字段上限减半再装，
    # 直到装得下或字段已经短到 200 字。编号不砍。
    while True:
        summaries, clipped = _clip_subtopic_summaries(subtopic_analyses, field_limit)
        content = json.dumps(
            {
                "任务": "根据各子主题分析摘要做全局综合分析",
                "用户综述主题": topic,
                "输出要求": _overall_analysis_schema_hint(),
                "各子主题分析摘要": summaries,
            },
            ensure_ascii=False,
            indent=2,
        )
        if len(content) <= OVERALL_USER_MAX_CHARS or field_limit <= 200:
            break
        field_limit = max(200, field_limit // 2)
    logger.info(
        "全局综合提示词已装好",
        extra={
            "user_chars": len(content),
            "field_limit": field_limit,
            "clipped_fields": "、".join(clipped) if clipped else "无",
        },
    )
    return [
        {
            "role": "system",
            # 中文说明：全局分析单独使用专门提示词，强调跨主题归纳和证据可追溯。
            "content": ANALYSE_OVERALL_SYSTEM_PROMPT,
        },
        {"role": "user", "content": content},
    ]


def split_subtopic_batches(
    *,
    topic: str,
    subtopic: str,
    papers: list[JsonObject],
) -> list[list[JsonObject]]:
    """按提示词字数把论文卡片分成几批。

    中文说明：一批的用户消息大约不超过 SUBTOPIC_BATCH_MAX_CHARS。
    一张卡片要么整张进入这一批，要么留到下一批，不从卡片中间切开。
    单张卡片自己就已经超限时，仍单独成一批，避免它永远进不了任何一批。
    """

    if not papers:
        return [[]]
    batches: list[list[JsonObject]] = []
    current: list[JsonObject] = []
    for paper in papers:
        trial = [*current, paper]
        if current and _subtopic_user_chars(topic, subtopic, trial) > SUBTOPIC_BATCH_MAX_CHARS:
            batches.append(current)
            current = [paper]
        else:
            current = trial
    if current:
        batches.append(current)
    return batches


def _subtopic_user_chars(topic: str, subtopic: str, papers: list[JsonObject]) -> int:
    """算出这一批论文装进用户消息后有多少字。"""

    messages = _subtopic_messages(
        topic=topic,
        group={"subtopic": subtopic, "papers": papers},
    )
    return len(str(messages[-1].get("content") or ""))


def _clip_subtopic_summaries(
    subtopic_analyses: list[JsonObject],
    field_limit: int,
) -> tuple[list[JsonObject], list[str]]:
    """把子主题长字段收成摘录，返回摘录和被截短的字段名。"""

    clipped: list[str] = []
    summaries: list[JsonObject] = []
    for item in subtopic_analyses:
        summary: JsonObject = {
            "subtopic": item.get("subtopic"),
            "paper_count": item.get("paper_count"),
            "paperIds": item.get("paperIds", []),
        }
        for field in (*_SUBTOPIC_TEXT_FIELDS, "一致点"):
            text, did_clip = _clip_text(_field_as_text(item.get(field)), field_limit)
            summary[field] = text
            if did_clip and field not in clipped:
                clipped.append(field)
        summaries.append(summary)
    return summaries, clipped


def _field_as_text(value: Any) -> str:
    """把字段收成一段文字。列表按行接起来，空值变成空字符串。"""

    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _clip_text(text: str, limit: int) -> tuple[str, bool]:
    """超过上限就截断，并标明原文更长。"""

    if len(text) <= limit:
        return text, False
    keep = max(0, limit - len(_CLIP_NOTE))
    return text[:keep] + _CLIP_NOTE, True


def _analysis_schema_hint() -> JsonObject:
    """给模型看的输出格式说明。"""

    return {
        "研究现状": "详细说明当前研究进展、主要发现和代表性工作；相关句子必须使用 [paperId] 引用",
        "一致点": ["一个一致点用一整段文字说明，并使用 [paperId] 引用"],
        "矛盾点": "用一整段文字说明不同论文的观点、结果或适用条件为何不同；没有明确矛盾时如实说明；必须使用 [paperId] 引用",
        "研究空白": "用一整段文字说明尚未解决的问题、数据或方法不足；必须使用 [paperId] 引用",
        "时间线演化": "用一整段文字按时间说明研究如何演变；必须使用 [paperId] 引用",
        "技术方法栈演变": "方法从早期到近期怎么变化，必须出现 [paperId]",
    }


def _overall_analysis_schema_hint() -> JsonObject:
    """给综合分析模型的八部分独立输出格式说明。"""

    citation_rule = "使用 Markdown 文本详细作答；每个关键结论都要在句子中引用输入中的 [paperId]，没有证据时明确说明证据不足"
    return {
        "领域整体研究概况": f"{citation_rule}；概括整体研究热度、覆盖范围、核心方向、成熟度、核心命题和争议总览",
        "领域全域共性研究共识": f"{citation_rule}；只保留跨多个子主题或全场景通用的共识，并按研究价值、应用特征、运行规律、基础认知等维度归纳",
        "领域核心研究争议与矛盾体系": f"{citation_rule}；说明对立观点、支撑文献、争议底层逻辑，以及场景、约束、视角或方法差异造成的适用边界",
        "领域系统性研究空白与局限": f"{citation_rule}；必须从研究地域、研究视角、研究时长、研究方法、研究内容、研究场景六个维度整理全域缺口",
        "领域研究时序演化脉络": f"{citation_rule}；依据论文发表时间划分阶段，说明每阶段的整体特征、关注重点、突破和不足",
        "领域技术与研究方法迭代脉络": f"{citation_rule}；梳理主流技术和方法的更替路径、适配场景、迭代动因、优势与局限",
        "各子主题横向差异对比分析": f"{citation_rule}；比较各子主题的成熟度、共识统一度、争议集中度、研究缺口体量、技术应用深度和研究丰富度，区分强弱板块",
        "领域整体总结与研究展望": f"{citation_rule}；总结核心结论、整体价值和约束边界，给出通用实践启示及与研究空白对应的可落地未来方向",
    }
