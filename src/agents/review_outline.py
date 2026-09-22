"""综述的大纲步骤：把分析报告收成章节和小节任务。

这里不是独立子 Agent。模型由综述流水线传入，本模块只负责提示词、
解析和把大纲整理成固定字段。
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.llm import ProviderSnapshot
from src.llm.base import normalize_token_usage

from .contracts import JsonObject
from .Prompts import WRITING_OUTLINE_AGENT_SYSTEM_PROMPT


# 中文说明：evidence-map 只能引用这八个全局分析字段。把字段集中放在这里，
# 可以保证大纲生成、兜底大纲和正文写作使用同一套名称。
OVERALL_ANALYSIS_FIELDS = (
    "领域整体研究概况",
    "领域全域共性研究共识",
    "领域核心研究争议与矛盾体系",
    "领域系统性研究空白与局限",
    "领域研究时序演化脉络",
    "领域技术与研究方法迭代脉络",
    "各子主题横向差异对比分析",
    "领域整体总结与研究展望",
)


def _report_usage(response: object, usage_callback: Any | None) -> None:
    """把这次模型调用的真实用量交给流水线，不按文字长度估算。"""

    if not callable(usage_callback):
        return
    usage_callback(normalize_token_usage(getattr(response, "usage", None)))


async def generate_outline(
    *,
    topic: str,
    analysis_report: JsonObject,
    llm: ProviderSnapshot | None,
    usage_callback: Any | None = None,
) -> tuple[JsonObject | None, str, str]:
    """调用大模型生成大纲。

    中文说明：只接收综述主题和分析报告，不读整份流程图状态。

    返回值说明：
    1. 第一个值是解析后的大纲；如果为 None，说明模型不可用或输出格式不对；
    2. 第二个值是模型原始输出，方便排查问题；
    3. 第三个值是简单状态说明，方便调用方写入诊断信息。
    """

    if llm is None:
        return None, "", "未配置可用的写作大纲模型"
    try:
        response = await llm.provider.chat(
            _outline_messages(topic=topic, analysis_report=analysis_report),
            temperature=0.2,
            reasoning_effort="medium",
        )
    except Exception as exc:
        return None, "", f"写作大纲模型调用失败：{exc}"
    _report_usage(response, usage_callback)

    raw_model_output = str(getattr(response, "content", "") or "")
    if not getattr(response, "ok", False):
        reason = raw_model_output or str(getattr(response, "error_kind", "") or "写作大纲模型调用失败")
        return None, raw_model_output, reason

    parsed = _extract_json_object(raw_model_output)
    if parsed is None:
        return None, raw_model_output, "模型没有返回可解析的 JSON 大纲"
    return _normalize_outline(parsed), raw_model_output, "ok"


def _outline_messages(*, topic: str, analysis_report: JsonObject) -> list[JsonObject]:
    """把综述主题和分析报告整理成模型容易理解的提示词。"""

    analysis_report = dict(analysis_report or {})
    overall_framework = str(analysis_report.get("overall_framework") or "").strip()
    overall_analysis = _compact_overall_analysis(dict(analysis_report.get("overall_analysis") or {}))
    subtopic_analyses = _compact_subtopic_analyses(list(analysis_report.get("subtopic_analyses") or []))

    # 中文说明：大纲约束集中管理，避免大纲格式与后续写作节点的输入约定不一致。
    system_prompt = WRITING_OUTLINE_AGENT_SYSTEM_PROMPT
    user_prompt = json.dumps(
        {
            "用户综述主题": topic,
            "任务": "根据 overall_framework 生成章节和小节级别的写作大纲",
            "overall_framework": overall_framework,
            "综合分析节点输出": overall_analysis,
            "可使用的子主题分析": subtopic_analyses,
            # 中文说明：这里只给结构，标题和说明一律用占位符，不要写具体的章节名。
            # 以前这里写的是「相关研究现状」「主要研究方向」这种真实标题，等于在用户
            # 消息里暗示了一套固定骨架；而系统提示词自己的示例用的是 title1 / task1
            # 这样的占位符（见 Prompts.py 的 WRITING_OUTLINE_AGENT_SYSTEM_PROMPT），
            # 两处口径不一致。另外系统提示词末尾还挂了一段「章节该按什么逻辑组织」
            # 的方法论，如果这里继续递一个具体骨架，模型会照着这个骨架写，那段方法论
            # 就等于被压住了——出了问题时也分不清是方法论没生效还是被这里的示例带偏。
            "输出示例": {
                "Chapter1": {
                    "title": "title1",
                    "description": "description1",
                    "Sections": {
                        "section1": {
                            "title": "title2",
                            "task": "task1",
                            "evidence-map": [],
                            "ref-sections": [],
                            "word-count": 600,
                        }
                    },
                }
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _compact_overall_analysis(overall_analysis: JsonObject) -> JsonObject:
    """只保留综合分析节点规定的八个字段，作为大纲的证据来源。"""

    # 中文说明：综合分析节点的结果固定为这八个字段。
    # 这里明确列出字段名，避免把执行信息或其他无关内容误当成写作证据。
    return {
        field: str(overall_analysis.get(field) or "").strip()
        for field in OVERALL_ANALYSIS_FIELDS
    }


def _compact_subtopic_analyses(subtopic_analyses: list[Any]) -> list[JsonObject]:
    """只保留写大纲需要看的字段，避免一次性把分析报告全部塞给模型。"""

    compact: list[JsonObject] = []
    for item in subtopic_analyses:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "subtopic": item.get("subtopic") or "",
                "paperIds": item.get("paperIds") or [],
                "研究现状": item.get("研究现状") or "",
                "consensus": item.get("一致点") or [],
                "矛盾点": item.get("矛盾点") or "",
                "研究空白": item.get("研究空白") or "",
                "时间线演化": item.get("时间线演化") or "",
                "技术方法栈演变": item.get("技术方法栈演变") or "",
            }
        )
    return compact


def _extract_json_object(text: str) -> JsonObject | None:
    """从模型输出中取出 JSON 对象。

    中文说明：
    有些模型会偷偷包一层 ```json 代码块，这里会先去掉代码块再解析。
    如果模型前后加了说明文字，也会尝试截取第一个大括号到最后一个大括号之间的内容。
    """

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


def _normalize_outline(value: JsonObject) -> JsonObject:
    """把模型大纲整理成固定字段，减少后续节点读取时的判断。"""

    raw_chapters = value.get("outline") if isinstance(value.get("outline"), dict) else value
    outline: JsonObject = {}
    chapter_index = 1
    for key, chapter in raw_chapters.items():
        if not isinstance(chapter, dict):
            continue
        chapter_key = str(key or "").strip() or f"Chapter{chapter_index}"
        if not chapter_key.lower().startswith("chapter"):
            chapter_key = f"Chapter{chapter_index}"
        chapter_title = str(chapter.get("title") or chapter.get("name") or "").strip() or chapter_key
        chapter_description = str(chapter.get("description") or "").strip()
        # 中文说明：即使模型误生成了这些部分，也在进入写作节点前删掉，保证结果只包含正文。
        # 这里只检查标题，避免章节说明中出现“不包含摘要”这类否定句时被误删。
        if _is_non_body_part(chapter_title, ""):
            continue
        outline[chapter_key] = {
            "title": chapter_title,
            "description": chapter_description,
            "Sections": _normalize_sections(chapter.get("Sections") or chapter.get("sections")),
        }
        chapter_index += 1
    return outline


def _normalize_sections(value: Any) -> JsonObject:
    """整理每章下面的小节，保证每个小节都有固定的四个字段。"""

    if isinstance(value, dict):
        raw_sections = list(value.items())
    elif isinstance(value, list):
        raw_sections = [(f"section{index}", item) for index, item in enumerate(value, start=1)]
    else:
        raw_sections = []

    sections: JsonObject = {}
    for index, (key, section) in enumerate(raw_sections, start=1):
        if not isinstance(section, dict):
            continue
        section_key = str(key or "").strip() or f"section{index}"
        if not section_key.lower().startswith("section"):
            section_key = f"section{index}"
        section_title = str(section.get("title") or section.get("name") or "").strip() or section_key
        section_task = str(section.get("task") or "").strip()
        if _is_non_body_part(section_title, ""):
            continue
        sections[section_key] = {
            "title": section_title,
            "task": section_task,
            "evidence-map": _normalize_evidence_map(section.get("evidence-map")),
            "ref-sections": _list_value(section.get("ref-sections")),
            "word-count": _positive_int(section.get("word-count"), default=800),
        }
    return sections


def _is_non_body_part(title: str, description: str) -> bool:
    """判断标题或说明是否误指向摘要、引言或参考文献等非正文部分。"""

    text = f"{title} {description}".lower()
    excluded_markers = (
        "摘要",
        "abstract",
        "引言",
        "绪论",
        "introduction",
        "参考文献",
        "references",
        "bibliography",
    )
    return any(marker in text for marker in excluded_markers)


def _list_value(value: Any) -> list[Any]:
    """把模型返回的数组字段整理成数组。"""

    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _normalize_evidence_map(value: Any) -> list[str]:
    """只保留全局分析中存在的字段名，避免无效内容进入正文写作。"""

    fields: list[str] = []
    for item in _list_value(value):
        field = str(item or "").strip()
        if field in OVERALL_ANALYSIS_FIELDS and field not in fields:
            fields.append(field)
    return fields


def _positive_int(value: Any, *, default: int) -> int:
    """把字数字段整理成正整数；模型没写对时使用默认值。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
