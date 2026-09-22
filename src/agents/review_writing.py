"""综述的写作步骤：按小节取证、写正文、审查，再写摘要。

这里不是独立子 Agent，也没有自己的流程图。一节内部用普通循环完成
「取证或写草稿 → 审查 → 不通过再改」。外层综述每写完一节才存一次进度，
所以这一节写到一半中断的话，接着写时会从这一节开头重来。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, TypedDict

from src.llm import ProviderSnapshot
from src.llm.base import normalize_token_usage
from src.services.paper_memory import resolve_paper_cache_dir
from src.utils import get_logger
from src.utils.read_utils.chunkers import TextChunk, load_chunks_file

from .contracts import JsonObject
from .Prompts import WRITING_ABSTRACT_SYSTEM_PROMPT, WRITING_AGENT_SYSTEM_PROMPT, WRITING_REVIEW_SYSTEM_PROMPT


logger = get_logger(__name__)


WritingAction = Literal["tool", "draft"]


class SectionLoopState(TypedDict, total=False):
    """单个小节写作循环内部使用的状态。

    中文注释：
    主工作流的 State 很大，里面有检索、阅读、分析、大纲等很多字段。
    单个小节写作只需要其中一小部分，所以这里单独放一个小状态，避免节点之间
    互相传一大包用不上的数据。
    """

    section_id: str
    task: str
    evidence_map: list[Any]
    previous_sections: list[JsonObject]
    word_count: int
    read_results: list[JsonObject]
    cache_dir: str
    available_paper_ids: list[str]
    tool_results: list[JsonObject]
    raw_model_outputs: list[str]
    revision_suggestions: list[str]
    draft: str
    cited_paper_ids: list[str]
    action_type: WritingAction
    tool_name: str
    tool_arguments: JsonObject
    tool_call_count: int
    revision_count: int
    review: JsonObject
    completed: bool
    warnings: list[str]


# 一节里最多向资料工具要几次原文。次数用尽后必须按手头材料写完。
MAX_TOOL_CALLS = 5

# 审查不通过时最多改几轮。改完仍不通过就保留当前正文，不让这一节死循环。
MAX_REVISION_ROUNDS = 2


async def write_section(
    *,
    section_id: str,
    task: str,
    evidence_map: list[Any],
    previous_sections: list[JsonObject],
    word_count: int,
    read_results: list[JsonObject],
    cache_dir: str,
    available_paper_ids: list[str] | None = None,
    progress_callback: Any | None = None,
    llm: ProviderSnapshot | None,
) -> JsonObject:
    """写作单个小节，并返回正文、引用和审查结果。

    中文说明：外层按大纲一节一节调用。这一节内部是普通循环：先决定要不要补资料，
    写完再审查；审查不通过就带着意见再改。循环中途没有单独的检查点，外层要等
    整节写完才记一笔，所以中断后续跑会从这一节开头重来。
    """

    state: SectionLoopState = {
        "section_id": section_id,
        "task": task,
        "evidence_map": list(evidence_map),
        "previous_sections": list(previous_sections),
        "word_count": max(100, int(word_count or 800)),
        "read_results": list(read_results),
        "cache_dir": cache_dir,
        "available_paper_ids": _deduplicate_strings(list(available_paper_ids or [])),
        "tool_results": [],
        "raw_model_outputs": [],
        "revision_suggestions": [],
        "draft": "",
        "cited_paper_ids": [],
        "action_type": "draft",
        "tool_name": "",
        "tool_arguments": {},
        "tool_call_count": 0,
        "revision_count": 0,
        "review": {},
        "completed": False,
        "warnings": [],
    }
    # 走向和原来的内层流程图一样：要工具就去取资料再回来；否则进入审查。
    # 只有审查说「这节可以收了」才停。写草稿时即使标了完成，也要先过一遍审查。
    while True:
        state = await plan_or_write(state, llm=llm, progress_callback=progress_callback)
        if state.get("action_type") == "tool":
            state = await run_tool(state)
            continue
        state = await review_draft(state, llm=llm, progress_callback=progress_callback)
        if state.get("completed"):
            break
    section_result = {
        "section_id": section_id,
        "task": task,
        "word_count": word_count,
        "content": str(state.get("draft") or "").strip(),
        "cited_paper_ids": _deduplicate_strings(list(state.get("cited_paper_ids") or [])),
        "tool_results": list(state.get("tool_results") or []),
        "review": dict(state.get("review") or {}),
        "revision_count": int(state.get("revision_count") or 0),
        "completed": bool(state.get("completed")),
        "warnings": list(state.get("warnings") or []),
    }
    # 中文说明：结构化摘要里的来源标记和切片检索返回的 chunkId 都是“证据位置”，
    # 不能直接作为正文引用。这里在小节离开前统一换成真正的 paperId，
    # 这样后面的参考文献解析只需要处理一种引用格式。
    return normalize_writing_section_citations(
        section_result,
        read_results=list(read_results),
        cache_dir=Path(cache_dir),
    )

async def write_abstract(
    *,
    topic: str,
    sections: list[JsonObject],
    word_count: int = 300,
    usage_callback: Any | None = None,
    llm: ProviderSnapshot | None,
) -> tuple[str, str]:
    """根据已经完成的正文生成摘要。

    中文说明：摘要必须建立在最终正文之上，所以只在所有小节写完后调用。
    返回摘要文本和状态说明；模型不可用时仍返回一段根据正文拼出的保守摘要，保证
    写作产物结构完整。
    """

    if llm is None:
        return _fallback_abstract(topic, sections), "未配置摘要写作模型，已使用保守摘要"

    try:
        response = await llm.provider.chat(
            _abstract_messages(topic=topic, sections=sections, word_count=word_count),
            temperature=0.2,
            reasoning_effort="medium",
        )
    except Exception as exc:
        return _fallback_abstract(topic, sections), f"摘要模型调用失败，已使用保守摘要：{exc}"

    _report_usage(response, usage_callback)

    raw_output = str(getattr(response, "content", "") or "")
    if not getattr(response, "ok", False):
        return _fallback_abstract(topic, sections), f"摘要模型返回失败，已使用保守摘要：{raw_output}"

    parsed = _extract_json_object(raw_output)
    abstract = str(parsed.get("content") or parsed.get("abstract") or "").strip() if parsed else ""
    if not abstract:
        abstract = raw_output.strip()
    if not abstract:
        return _fallback_abstract(topic, sections), "摘要模型没有返回正文，已使用保守摘要"
    return abstract, "ok"


async def plan_or_write(
    state: SectionLoopState,
    *,
    llm: ProviderSnapshot | None,
    progress_callback: Any | None,
) -> SectionLoopState:
    """让模型决定当前是补资料还是直接写正文。"""

    _notify_section_progress(progress_callback, "正在撰写小节正文")
    if llm is None:
        draft = _fallback_draft(state)
        return {
            **state,
            "action_type": "draft",
            "draft": draft,
            "cited_paper_ids": _paper_ids_from_any([state.get("evidence_map"), draft]),
            "completed": True,
            "warnings": [*list(state.get("warnings") or []), "未配置可用的写作模型，已生成保守正文草稿。"],
        }

    response = await llm.provider.chat(
        _write_messages(state),
        temperature=0.2,
        reasoning_effort="medium",
    )
    _notify_section_usage(progress_callback, response)
    raw_output = str(getattr(response, "content", "") or "")
    raw_outputs = [*list(state.get("raw_model_outputs") or []), raw_output]
    if not response.ok:
        draft = _fallback_draft(state)
        return {
            **state,
            "action_type": "draft",
            "draft": draft,
            "cited_paper_ids": _paper_ids_from_any([state.get("evidence_map"), draft]),
            "raw_model_outputs": raw_outputs,
            "warnings": [*list(state.get("warnings") or []), f"写作模型调用失败，已生成保守正文草稿：{raw_output}"],
        }

    parsed = _extract_json_object(raw_output)
    if parsed is None:
        draft = raw_output.strip() or _fallback_draft(state)
        # 中文注释：这里是一条静默降级路径——模型没按约定返回结构化结果，
        # 我们退而求其次把它的自由文本当成正文草稿。降级本身是设计好的，
        # 但以前只写进 warnings 列表，而 warnings 既不落日志也不进产物元数据，
        # 等于「提示词有没有被稀释到模型不再守格式」这件事完全看不见。
        # 补一条日志，让这条路径可统计。
        logger.warning(
            "写作模型没有返回可解析的结构化结果，已把输出文本当作草稿使用",
            extra={
                "section_id": str(state.get("section_id") or ""),
                "output_chars": len(raw_output),
            },
        )
        return {
            **state,
            "action_type": "draft",
            "draft": draft,
            "cited_paper_ids": _paper_ids_from_any([state.get("evidence_map"), draft]),
            "raw_model_outputs": raw_outputs,
            "warnings": [*list(state.get("warnings") or []), "写作模型没有返回 JSON，已把模型文本当作草稿使用。"],
        }

    action = str(parsed.get("action") or "draft").strip().lower()
    if action == "tool" and int(state.get("tool_call_count") or 0) < MAX_TOOL_CALLS:
        return {
            **state,
            "action_type": "tool",
            "tool_name": str(parsed.get("tool_name") or "").strip(),
            "tool_arguments": dict(parsed.get("arguments") or {}) if isinstance(parsed.get("arguments"), dict) else {},
            "raw_model_outputs": raw_outputs,
        }

    draft = str(parsed.get("content") or "").strip()
    if not draft:
        draft = _fallback_draft(state)
    paper_ids = _deduplicate_strings(
        [
            *_string_list(parsed.get("paperIds")),
            *_string_list(parsed.get("paper_ids")),
            *_paper_ids_from_any([state.get("evidence_map"), state.get("tool_results"), draft]),
        ]
    )
    warnings = list(state.get("warnings") or [])
    if action == "tool":
        warnings.append("工具调用次数已达到上限，已要求根据现有资料完成写作。")
    return {
        **state,
        "action_type": "draft",
        "draft": draft,
        "cited_paper_ids": paper_ids,
        "raw_model_outputs": raw_outputs,
        "warnings": warnings,
    }


async def run_tool(state: SectionLoopState) -> SectionLoopState:
    """执行模型请求的工具，并把结果放回循环状态。"""

    result = execute_writing_tool(
        tool_name=str(state.get("tool_name") or ""),
        arguments=dict(state.get("tool_arguments") or {}),
        read_results=list(state.get("read_results") or []),
        cache_dir=Path(str(state.get("cache_dir") or "data/paper_cache")),
    )
    return {
        **state,
        "tool_results": [*list(state.get("tool_results") or []), result],
        "tool_call_count": int(state.get("tool_call_count") or 0) + 1,
        "tool_name": "",
        "tool_arguments": {},
    }


async def review_draft(
    state: SectionLoopState,
    *,
    llm: ProviderSnapshot | None,
    progress_callback: Any | None,
) -> SectionLoopState:
    """审查正文是否逻辑通顺、语言是否足够学术化。"""

    _notify_section_progress(progress_callback, "正在审查写作内容")
    draft = str(state.get("draft") or "").strip()
    if llm is None:
        return {
            **state,
            "review": {"passed": True, "suggestions": [], "message": "未配置审查模型，已跳过模型审查。"},
            "completed": True,
        }
    if not draft:
        return {
            **state,
            "review": {"passed": False, "suggestions": ["正文为空，需要先生成小节正文。"]},
            "completed": False,
        }

    response = await llm.provider.chat(
        _review_messages(state),
        temperature=0,
        reasoning_effort="medium",
    )
    _notify_section_usage(progress_callback, response)
    raw_output = str(getattr(response, "content", "") or "")
    raw_outputs = [*list(state.get("raw_model_outputs") or []), raw_output]
    if not response.ok:
        return {
            **state,
            "raw_model_outputs": raw_outputs,
            "review": {"passed": True, "suggestions": [], "message": "审查模型调用失败，保留当前正文。"},
            "completed": True,
            "warnings": [*list(state.get("warnings") or []), f"审查模型调用失败：{raw_output}"],
        }

    parsed = _extract_json_object(raw_output) or {}
    passed = bool(parsed.get("passed"))
    suggestions = _string_list(parsed.get("suggestions"))
    if passed or int(state.get("revision_count") or 0) >= MAX_REVISION_ROUNDS:
        message = str(parsed.get("message") or ("审查通过" if passed else "修改次数已达到上限，保留当前正文")).strip()
        if not passed:
            # 中文注释：审查一直不通过、修改次数用尽，只好保留当前正文收尾。
            # 这条也要记日志——它同样是「写手没写到位」的量化信号，
            # 只写进 warnings 的话事后根本查不到发生过几次。
            logger.warning(
                "小节审查反复未通过，修改次数已达上限，保留当前正文",
                extra={
                    "section_id": str(state.get("section_id") or ""),
                    "revision_count": int(state.get("revision_count") or 0),
                    "suggestions": suggestions[:5],
                },
            )
        return {
            **state,
            "raw_model_outputs": raw_outputs,
            "review": {"passed": passed, "suggestions": suggestions, "message": message},
            "completed": True,
        }
    return {
        **state,
        "raw_model_outputs": raw_outputs,
        "review": {"passed": False, "suggestions": suggestions, "message": str(parsed.get("message") or "")},
        "revision_suggestions": suggestions,
        "revision_count": int(state.get("revision_count") or 0) + 1,
        "completed": False,
    }


def _report_usage(response: object, usage_callback: Any | None) -> None:
    """把摘要这次模型调用的真实用量交给流水线，不按文字长度估算。"""

    if not callable(usage_callback):
        return
    usage_callback(normalize_token_usage(getattr(response, "usage", None)))


def _notify_section_progress(progress_callback: Any | None, message: str) -> None:
    """把小节当前阶段交给外层；没有回调时保持原来的静默行为。

    中文说明：外层回调目前只累加用量，进度文字不会再往对话里推一条。
    用户看到的仍是「正在写作第几节」这一条。
    """

    if callable(progress_callback):
        progress_callback(message)


def _notify_section_usage(progress_callback: Any | None, response: object) -> None:
    """把小节内部每一次模型调用的真实用量交给外层。"""

    if callable(progress_callback):
        progress_callback("模型调用完成", normalize_token_usage(getattr(response, "usage", None)))


def get_extraction(
    paper_ids: list[str],
    *,
    read_results: list[JsonObject] | None = None,
    cache_dir: str | Path = "data/paper_cache",
) -> list[JsonObject]:
    """根据 paperId 获取论文结构化摘要。

    中文说明：先看这次综述已经整理好的阅读结果。那里没有时，再按本机论文目录表
    找到缓存文件夹，读里面的 extraction.json。
    """

    results: list[JsonObject] = []
    for paper_id in _deduplicate_strings(paper_ids):
        extraction = _find_extraction_in_read_results(paper_id, list(read_results or []))
        source = "state"
        if extraction is None:
            extraction = _find_extraction_in_cache(paper_id, Path(cache_dir))
            source = "paper_cache"
        if extraction is None:
            results.append({"paperId": paper_id, "status": "missing", "extraction": {}, "source": ""})
        else:
            results.append({"paperId": paper_id, "status": "ok", "extraction": extraction, "source": source})
    return results


def search_section(
    requests: list[JsonObject],
    *,
    cache_dir: str | Path = "data/paper_cache",
) -> list[JsonObject]:
    """根据 paperId 和 chunkId 获取论文原文片段。

    中文注释：
    这个工具不做复杂搜索，只做“按编号取原文”。模型如果已经知道需要哪几个
    chunkId，就可以用它把 chunk.json 里的原文片段取出来。
    """

    found: list[JsonObject] = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("paperId") or item.get("paper_id") or "").strip()
        chunk_ids = _string_list(item.get("chunkIds") or item.get("chunk_ids") or item.get("chunkId") or item.get("chunk_id"))
        if not paper_id or not chunk_ids:
            found.append({"paperId": paper_id, "status": "invalid_arguments", "chunks": []})
            continue
        chunks_path = _find_chunks_path(paper_id, Path(cache_dir))
        if chunks_path is None:
            found.append({"paperId": paper_id, "status": "missing_chunks", "chunks": []})
            continue
        chunks_by_id = {chunk.chunk_id: chunk for chunk in load_chunks_file(chunks_path)}
        selected = [_chunk_to_markdown(chunks_by_id[chunk_id]) for chunk_id in chunk_ids if chunk_id in chunks_by_id]
        found.append({"paperId": paper_id, "status": "ok" if selected else "missing_chunk_ids", "chunks": selected})
    return found


def normalize_writing_section_citations(
    section: JsonObject,
    *,
    read_results: list[JsonObject],
    cache_dir: Path,
) -> JsonObject:
    """把小节里可能出现的切片引用统一改成对应的论文编号。

    中文说明：全文结构化摘要会把证据位置写成 `[论文编号:p0001]`，切片工具返回的
    结果还会同时出现 `paperId` 和 `chunkId`。模型有时会把后者误写进正文，因此这里
    统一查找“切片编号 -> 论文编号”的关系，再处理正文和 cited_paper_ids 两个字段。
    """

    normalized = dict(section)
    chunk_to_paper = _build_chunk_to_paper_map(
        [*read_results, section.get("tool_results") or []],
        cache_dir=cache_dir,
    )
    content = _replace_chunk_citations(str(normalized.get("content") or ""), chunk_to_paper)
    cited_paper_ids: list[str] = []
    for value in list(normalized.get("cited_paper_ids") or []):
        paper_id = _resolve_citation_paper_id(str(value or ""), chunk_to_paper)
        if paper_id:
            cited_paper_ids.append(paper_id)

    normalized["content"] = content.strip()
    normalized["cited_paper_ids"] = _deduplicate_strings(cited_paper_ids)
    return normalized


def _build_chunk_to_paper_map(payloads: list[Any], *, cache_dir: Path) -> dict[str, str]:
    """从工具结果、阅读结果和本地切片缓存建立编号映射。"""

    mapping: dict[str, str] = {}
    _collect_chunk_mappings(payloads, mapping)

    # 中文说明：结构化摘要里只有正文中的 chunkId，未必会把 chunks_used 一起传过来。
    # 因此再根据结果里出现过的 paperId 读取对应缓存，补齐摘要引用所需的映射。
    paper_ids = _collect_payload_paper_ids(payloads)
    for paper_id in paper_ids:
        chunks_path = _find_chunks_path(paper_id, cache_dir)
        if chunks_path is None:
            continue
        for chunk in load_chunks_file(chunks_path):
            _register_chunk_mapping(mapping, chunk.chunk_id, chunk.paperId or paper_id)
    return mapping


def _collect_chunk_mappings(value: Any, mapping: dict[str, str], inherited_paper_id: str = "") -> None:
    """递归读取 payload 中显式提供的 paperId、chunkId 和 chunks_used。"""

    if isinstance(value, dict):
        nested_paper = value.get("paper")
        nested_paper_id = ""
        if isinstance(nested_paper, dict):
            nested_paper_id = str(nested_paper.get("paperId") or nested_paper.get("id") or "").strip()
        paper_id = str(value.get("paperId") or value.get("paper_id") or nested_paper_id or inherited_paper_id).strip()
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in {"chunkId", "chunk_id"}:
                _register_chunk_mapping(mapping, str(item or ""), paper_id)
                continue
            if normalized_key in {"chunkIds", "chunk_ids", "chunks_used"}:
                for chunk_id in _string_list(item):
                    _register_chunk_mapping(mapping, chunk_id, paper_id)
                continue
            _collect_chunk_mappings(item, mapping, paper_id)
        return
    if isinstance(value, list):
        for item in value:
            _collect_chunk_mappings(item, mapping, inherited_paper_id)


def _collect_payload_paper_ids(value: Any) -> list[str]:
    """只从结构化字段收集真实 paperId，不把正文里的方括号内容当成编号。"""

    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in {"paperId", "paper_id"}:
                text = str(item or "").strip()
                if text:
                    found.append(text)
            elif normalized_key in {"paperIds", "paper_ids"}:
                found.extend(_string_list(item))
            else:
                found.extend(_collect_payload_paper_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_payload_paper_ids(item))
    return _deduplicate_strings(found)


def _register_chunk_mapping(mapping: dict[str, str], chunk_id: str, paper_id: str) -> None:
    """登记完整 chunkId，并登记去掉末尾分段号后的短格式。"""

    chunk_text = str(chunk_id or "").strip()
    paper_text = str(paper_id or "").strip()
    if not chunk_text or not paper_text:
        return
    key = chunk_text.lower()
    mapping.setdefault(key, paper_text)
    # 中文说明：切片通常形如 `paper:p0001:s0001`，模型在摘要中常会省略最后的分段号，
    # 所以 `paper:p0001` 也必须能映射回同一篇论文。
    short_key = re.sub(r":s\d+$", "", key, flags=re.IGNORECASE)
    mapping.setdefault(short_key, paper_text)


def _resolve_citation_paper_id(value: str, chunk_to_paper: dict[str, str]) -> str:
    """把一个引用候选值解析为论文编号；无法解析时保留原值。"""

    text = str(value or "").strip().strip('"').strip("'").strip()
    if not text:
        return ""
    key = text.lower()
    mapped = chunk_to_paper.get(key)
    if mapped:
        return mapped
    # 中文说明：如果缓存中暂时没有对应 chunk.json，切片编号仍然保留了
    # `paperId:p0001` 的前缀。直接取前缀可以避免把切片编号继续传播到正文。
    prefix_match = re.match(r"^(?P<paper>.+):(?:p|c)\d{4}(?::s\d{4})?$", text, flags=re.IGNORECASE)
    return prefix_match.group("paper").strip() if prefix_match else text


def _replace_chunk_citations(content: str, chunk_to_paper: dict[str, str]) -> str:
    """只替换能确认是切片编号的方括号内容，避免破坏普通 Markdown。"""

    if not content:
        return content

    citation_pattern = re.compile(r"\[([^\[\]\r\n]+)\]")

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(1).strip().strip('"').strip("'").strip()
        resolved = _resolve_citation_paper_id(candidate, chunk_to_paper)
        return f"[{resolved}]" if resolved != candidate else match.group(0)

    return citation_pattern.sub(replace, content)


def get_chunk_by_embed(query: str):
    """通过向量检索全知识库中的相关 chunk。

    中文注释：
    需求明确说这个工具先提供给 Agent，但暂时不实现，所以这里保留 pass。
    后续接入向量库时，只需要替换这个函数内部逻辑即可。
    """

    pass


def execute_writing_tool(
    *,
    tool_name: str,
    arguments: JsonObject,
    read_results: list[JsonObject],
    cache_dir: Path,
) -> JsonObject:
    """执行写作时模型点名的资料工具。"""

    if tool_name == "get_extraction":
        paper_ids = _string_list(arguments.get("paperIds") or arguments.get("paper_ids") or arguments.get("paperId"))
        return {
            "tool": tool_name,
            "arguments": {"paperIds": paper_ids},
            "result": get_extraction(
                paper_ids,
                read_results=read_results,
                cache_dir=cache_dir,
            ),
        }
    if tool_name == "search_section":
        requests = arguments.get("requests") or arguments.get("sections") or arguments.get("items") or []
        if not isinstance(requests, list):
            requests = []
        return {"tool": tool_name, "arguments": {"requests": requests}, "result": search_section(requests, cache_dir=cache_dir)}
    if tool_name == "get_chunk_by_embed":
        query = str(arguments.get("query") or "").strip()
        result = get_chunk_by_embed(query)
        return {"tool": tool_name, "arguments": {"query": query}, "result": result, "message": "get_chunk_by_embed 暂未实现"}
    return {"tool": tool_name, "arguments": arguments, "result": None, "message": "未知工具，未执行"}


def _write_messages(state: SectionLoopState) -> list[JsonObject]:
    """构造写作提示词，让模型用 JSON 表达下一步动作。"""

    # 中文说明：每轮写作都复用统一规则，确保工具调用和正文输出格式稳定。
    system_prompt = WRITING_AGENT_SYSTEM_PROMPT
    user_prompt = json.dumps(
        {
            "section_id": state.get("section_id"),
            "小节任务": state.get("task"),
            "计划字数": state.get("word_count"),
            "全局分析提供的证据": state.get("evidence_map") or [],
            "已经写好的前置小节": state.get("previous_sections") or [],
            "已调用工具得到的资料": state.get("tool_results") or [],
            "允许引用的真实论文编号": state.get("available_paper_ids") or [],
            "当前草稿": state.get("draft") or "",
            "审查整改建议": state.get("revision_suggestions") or [],
            "可用工具": [
                "get_extraction(List[paperId])：获取论文结构化摘要",
                "search_section(List[{paperId, List[chunkId]}])：按 chunkId 获取论文原文片段",
                "get_chunk_by_embed(query)：通过向量检索相关 chunk，当前暂未实现",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]


def _abstract_messages(*, topic: str, sections: list[JsonObject], word_count: int) -> list[JsonObject]:
    """构造摘要提示词，只把已经完成的小节正文交给模型。"""

    # 中文说明：摘要只读取已完成的小节正文，系统提示词不在这里重复维护。
    system_prompt = WRITING_ABSTRACT_SYSTEM_PROMPT
    body = [
        {
            "小节标题": str(section.get("section_title") or section.get("section_id") or ""),
            "正文": str(section.get("content") or "").strip(),
        }
        for section in sections
        if isinstance(section, dict) and str(section.get("content") or "").strip()
    ]
    user_prompt = json.dumps(
        {"用户主题": topic, "摘要建议字数": max(100, int(word_count or 300)), "已完成正文": body},
        ensure_ascii=False,
        indent=2,
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]


def _fallback_abstract(topic: str, sections: list[JsonObject]) -> str:
    """模型不可用时从各小节开头拼出一段保守摘要。"""

    summaries: list[str] = []
    for section in sections:
        content = re.sub(r"\s+", " ", str(section.get("content") or "")).strip()
        if not content:
            continue
        first_sentence = re.split(r"(?<=[。！？.!?])\s*", content, maxsplit=1)[0].strip()
        summaries.append(first_sentence or content[:180])
        if len(summaries) >= 4:
            break
    if not summaries:
        return f"本文围绕“{topic}”梳理相关研究，并总结现有工作的主要进展、研究不足与后续方向。"
    return f"本文围绕“{topic}”梳理相关研究。" + "".join(summaries)


def _review_messages(state: SectionLoopState) -> list[JsonObject]:
    """构造审查提示词，只检查逻辑和语言。"""

    # 中文说明：审查范围保持窄而明确，避免模型把审查变成重新设计全文。
    system_prompt = WRITING_REVIEW_SYSTEM_PROMPT
    user_prompt = json.dumps(
        {
            "section_id": state.get("section_id"),
            "小节任务": state.get("task"),
            "计划字数": state.get("word_count"),
            "正文草稿": state.get("draft") or "",
        },
        ensure_ascii=False,
        indent=2,
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]


def _fallback_draft(state: SectionLoopState) -> str:
    """模型不可用时返回一个明确的失败占位，不生成伪正文。

    中文注释：
    改造前这个函数会生成一段「像正文的文字」并贴上第一篇论文的引用。
    这是程序确定性伪造归因——看起来像正文但实际没有证据支撑，且引用编号
    会流进参考文献。现在改为返回一个明确的失败占位，上层在综述里留一行说明。
    """

    task = state.get("task") or "当前小节任务"
    evidence_text = _compact_text(state.get("evidence_map") or [])
    previous_hint = _compact_text([item.get("section_id") for item in state.get("previous_sections") or [] if isinstance(item, dict)])
    parts = [
        f"[本节写作失败：写作模型不可用]",
        f"任务：{task}",
        f"已有证据摘要：{evidence_text or '当前大纲尚未提供足够的细化证据'}",
    ]
    if previous_hint:
        parts.append(f"写作衔接：需要承接 {previous_hint} 的论述。")
    parts.append("请后续人工补充或重新运行写作模型。")
    return "\n\n".join(parts)


def _find_extraction_in_read_results(paper_id: str, read_results: list[JsonObject]) -> JsonObject | None:
    """优先从阅读节点结果里查找结构化摘要。"""

    for item in read_results:
        if not isinstance(item, dict):
            continue
        paper = dict(item.get("paper") or {})
        candidates = {str(paper.get("paperId") or ""), str(paper.get("id") or ""), str(item.get("paperId") or "")}
        if paper_id not in candidates:
            continue
        extraction = item.get("extraction")
        if isinstance(extraction, dict) and any(str(value).strip() for value in extraction.values()):
            return dict(extraction)
    return None


def _find_extraction_in_cache(paper_id: str, cache_dir: Path) -> JsonObject | None:
    """从论文缓存目录中的 extraction.json 查找结构化摘要。

    中文说明：目录位置只查本机论文目录表，不再自己扫盘或读 metadata.json。
    """

    directory = resolve_paper_cache_dir(cache_dir, paper_id)
    path = directory / "extraction.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    extraction = payload.get("extraction") if isinstance(payload, dict) else None
    if isinstance(extraction, dict):
        return dict(extraction)
    return None


def _find_chunks_path(paper_id: str, cache_dir: Path) -> Path | None:
    """定位某篇论文缓存目录下的 chunk.json。

    中文说明：目录位置只查本机论文目录表，编号对不上时由目录表里记下的文件夹名来对。
    """

    directory = resolve_paper_cache_dir(cache_dir, paper_id)
    chunks_path = directory / "chunk.json"
    if chunks_path.exists():
        return chunks_path
    return None


def _chunk_to_markdown(chunk: TextChunk) -> JsonObject:
    """把 chunk 整理成写正文时容易阅读的 Markdown 片段。"""

    header = f"### {chunk.paperId} / {chunk.chunk_id}"
    if chunk.section:
        header += f" / {chunk.section}"
    return {
        "paperId": chunk.paperId,
        "chunkId": chunk.chunk_id,
        "markdown": f"{header}\n\n{chunk.content.strip()}",
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
    }


def _extract_json_object(text: str) -> JsonObject | None:
    """从模型输出中提取 JSON 对象。"""

    stripped = text.strip()
    if not stripped:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    candidates.append(stripped)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _string_list(value: Any) -> list[str]:
    """把字符串或字符串数组整理成干净数组。"""

    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _deduplicate_strings(values: list[str]) -> list[str]:
    """按原顺序给字符串去重。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _paper_ids_from_any(value: Any) -> list[str]:
    """从任意嵌套数据里尽量提取 paperId。"""

    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in {"paperId", "paper_id"} and str(item).strip():
                found.append(_clean_paper_id_candidate(str(item)))
            elif str(key) in {"paperIds", "paper_ids"}:
                found.extend(_clean_paper_id_candidate(text) for text in _string_list(item))
            else:
                found.extend(_paper_ids_from_any(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_paper_ids_from_any(item))
    elif isinstance(value, str):
        found.extend(_clean_paper_id_candidate(match) for match in re.findall(r"\[([^\[\]]+)\]", value) if match.strip())
    return _deduplicate_strings(found)


def _clean_paper_id_candidate(value: str) -> str:
    """清理从正文或证据里提取到的 paperId 候选值。"""

    return value.strip().strip('"').strip("'").strip()


def _compact_text(value: Any, *, max_chars: int = 500) -> str:
    """把证据或前置小节压成短文本，供兜底正文使用。"""

    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
