"""综述终稿的引用整理和 Markdown 拼装。

这些函数不调用模型，也不读取工作区。流水线先准备好论文资料，
再把正文、摘要和参考文献交给这里排成一份可下载的文稿。
"""

from __future__ import annotations

import re
from typing import Any

from .contracts import JsonObject


def _has_reference_metadata(paper: JsonObject) -> bool:
    """判断论文资料是否至少包含可展示的题名。移植自 writing_node._has_reference_metadata。"""

    return bool(str(paper.get("title") or "").strip())


def _build_references(paper_ids: list[str], metadata_by_id: dict[str, JsonObject]) -> list[JsonObject]:
    """按正文首次引用顺序生成 GB/T 7714 参考文献条目。移植自 writing_node._build_references。"""

    references: list[JsonObject] = []
    for paper_id in paper_ids:
        metadata = dict(metadata_by_id.get(paper_id.lower()) or {})
        # 没有题名的资料不生成参考文献，不能用 paperId 代替论文题名。
        if not _has_reference_metadata(metadata):
            continue
        references.append(
            {
                "index": len(references) + 1,
                "paperId": paper_id,
                "citation": _format_gbt7714_reference(paper_id, metadata),
                "metadata": metadata,
            }
        )
    return references


def _format_gbt7714_reference(paper_id: str, paper: JsonObject) -> str:
    """使用论文元数据生成常见的 GB/T 7714 顺序编码制格式。

    移植自 writing_node._format_gbt7714_reference，格式保持不变。
    """

    title = str(paper.get("title") or paper_id).strip()
    extra_metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    authors = _format_reference_authors(paper.get("authors") or paper.get("author"))
    resource_type = _reference_resource_type(paper)
    container = str(
        paper.get("journal_conference")
        or paper.get("journal/conference")
        or paper.get("journal")
        or paper.get("venue")
        or extra_metadata.get("journal")
        or ""
    ).strip()
    year = str(paper.get("year") or paper.get("publication_date") or "").strip()[:4]
    volume = str(paper.get("volume") or "").strip()
    issue = str(paper.get("issue") or "").strip()
    pages = str(
        paper.get("pages")
        or paper.get("page_range")
        or extra_metadata.get("pages")
        or (
            f"{paper.get('page_start')}-{paper.get('page_end')}"
            if paper.get("page_start") is not None and paper.get("page_end") is not None
            else ""
        )
    ).strip()
    doi = str(paper.get("doi") or "").strip()
    url = str(paper.get("url") or "").strip()

    citation = f"{authors + '. ' if authors else ''}{title}[{resource_type}]"
    if container:
        citation += f". {container}"
    if year:
        citation += f", {year}"
    if volume:
        citation += f", {volume}"
        if issue:
            citation += f"({issue})"
    elif issue:
        citation += f", ({issue})"
    if pages:
        citation += f": {pages}"
    citation += "."
    if doi:
        citation += f" DOI: {doi}."
    elif url:
        citation += f" {url}."
    return citation


def _format_reference_authors(value: Any) -> str:
    """整理作者字段，超过三位时按 GB/T 7714 习惯使用 et al.。移植自 writing_node._format_reference_authors。"""

    if isinstance(value, str):
        authors = [item.strip() for item in re.split(r"[,;，；]", value) if item.strip()]
    elif isinstance(value, list):
        authors = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("author") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                authors.append(name)
    else:
        authors = []
    if len(authors) > 3:
        suffix = "等" if any("一" <= character <= "鿿" for character in authors[0]) else "et al"
        return ", ".join(authors[:3]) + ("，" if suffix == "等" else ", ") + suffix
    return ", ".join(authors)


def _reference_resource_type(paper: JsonObject) -> str:
    """根据元数据推断参考文献类型，缺少信息时按期刊论文处理。移植自 writing_node._reference_resource_type。"""

    type_text = " ".join(
        str(paper.get(key) or "")
        for key in ("type", "document_type", "publication_type", "source")
    ).lower()
    if "conference" in type_text or "proceedings" in type_text:
        return "C"
    if "thesis" in type_text or "dissertation" in type_text:
        return "D"
    if "book" in type_text:
        return "M"
    if "arxiv" in type_text or "preprint" in type_text:
        return "EB/OL"
    return "J"


def _extract_paper_ids_from_sections(sections: list[JsonObject]) -> list[str]:
    """从小节正文的方括号引用中提取 paperId，并按首次出现顺序去重。

    移植自 writing_node._extract_paper_ids_from_sections。
    """

    declared_ids = _collect_cited_paper_ids(sections)
    declared_by_key = {paper_id.lower(): paper_id for paper_id in declared_ids}
    found: list[str] = []
    seen: set[str] = set()
    citation_pattern = re.compile(r"\[([^\[\]\r\n]+)\]")
    for section in sections:
        content = str(section.get("content") or "")
        for match in citation_pattern.finditer(content):
            candidate = match.group(1).strip().strip('"').strip("'")
            if not candidate or any(character.isspace() for character in candidate):
                continue
            # 即使模型漏掉了前面的归一化，也不能把切片编号直接生成参考文献。
            if _is_chunk_id(candidate):
                continue
            paper_id = declared_by_key.get(candidate.lower(), candidate)
            if candidate.lower() not in declared_by_key and not re.search(r"\d|[:/.]", candidate):
                continue
            key = paper_id.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(paper_id)
        # 如果模型把引用列在结构化字段里但正文没有重复写出，仍保留该引用。
        for paper_id in list(section.get("cited_paper_ids") or []):
            text = str(paper_id or "").strip()
            if _is_chunk_id(text):
                continue
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                found.append(text)
    return found


def _is_chunk_id(value: str) -> bool:
    """判断一个候选编号是否符合全文切片的页码或分段编号格式。移植自 writing_node._is_chunk_id。"""

    return bool(re.search(r":(?:p|c)\d{4}(?::s\d{4})?$", str(value or "").strip(), flags=re.IGNORECASE))


def _collect_cited_paper_ids(sections: list[JsonObject]) -> list[str]:
    """汇总所有小节实际引用到的 paperId。移植自 writing_node._collect_cited_paper_ids。"""

    seen: set[str] = set()
    result: list[str] = []
    for section in sections:
        for paper_id in list(section.get("cited_paper_ids") or []):
            text = str(paper_id or "").strip()
            if _is_chunk_id(text):
                continue
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
    return result


def _remove_unknown_paper_citations(
    sections: list[JsonObject],
    unknown_paper_ids: list[str],
) -> list[JsonObject]:
    """删除小节中没有真实论文资料支撑的引用标记。移植自 writing_node._remove_unknown_paper_citations。"""

    if not unknown_paper_ids:
        return [dict(section) for section in sections]
    unknown_keys = {paper_id.lower() for paper_id in unknown_paper_ids}
    cleaned_sections: list[JsonObject] = []
    for section in sections:
        cleaned = dict(section)
        cleaned["content"] = _remove_unknown_citation_markers(
            str(cleaned.get("content") or ""),
            unknown_paper_ids,
        )
        # 正文和 cited_paper_ids 必须同步清理。
        cleaned["cited_paper_ids"] = [
            paper_id
            for paper_id in list(cleaned.get("cited_paper_ids") or [])
            if str(paper_id or "").strip().lower() not in unknown_keys
        ]
        cleaned_sections.append(cleaned)
    return cleaned_sections


def _remove_unknown_citation_markers(content: str, unknown_paper_ids: list[str]) -> str:
    """从一段文字中删除形如 [P1] 的未知引用标记。移植自 writing_node._remove_unknown_citation_markers。"""

    if not content or not unknown_paper_ids:
        return content
    unknown_keys = {paper_id.lower() for paper_id in unknown_paper_ids}
    citation_pattern = re.compile(r"\[([^\[\]\r\n]+)\]")

    def replace(match: re.Match[str]) -> str:
        paper_id = match.group(1).strip().strip('"').strip("'")
        return "" if paper_id.lower() in unknown_keys else match.group(0)

    return citation_pattern.sub(replace, content)


def _replace_section_citations(
    sections: list[JsonObject],
    citation_index_by_paper_id: dict[str, str],
) -> list[JsonObject]:
    """把正文小节中的 [paperId] 替换为参考文献序号。移植自 writing_node._replace_section_citations。"""

    if not citation_index_by_paper_id:
        return [dict(section) for section in sections]
    replaced: list[JsonObject] = []
    for section in sections:
        item = dict(section)
        item["content"] = _replace_citation_numbers(
            str(item.get("content") or ""), citation_index_by_paper_id
        )
        replaced.append(item)
    return replaced


def _replace_citation_numbers(content: str, citation_index_by_paper_id: dict[str, str]) -> str:
    """替换一段文本中的论文编号，普通 Markdown 方括号保持不变。移植自 writing_node._replace_citation_numbers。"""

    citation_pattern = re.compile(r"\[([^\[\]\r\n]+)\]")

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(1).strip().strip('"').strip("'").strip()
        index = citation_index_by_paper_id.get(candidate.lower())
        return f"[{index}]" if index else match.group(0)

    return citation_pattern.sub(replace, content)


def _build_final_markdown(
    *,
    topic: str,
    sections: list[JsonObject],
    abstract: str,
    references: list[JsonObject],
) -> str:
    """把写作节点的全部已完成内容拼成一份可以直接保存的 Markdown 综述。

    结构移植自 reply_node._build_final_markdown：# 标题 / ## 摘要 / ## 章标题 /
    ### 节标题 / ## 参考文献。
    """

    blocks: list[str] = []
    title = topic.strip() or "文献综述"
    if title:
        blocks.append(f"# {title}")
    if abstract.strip():
        blocks.append(f"## 摘要\n\n{abstract.strip()}")

    current_chapter = ""
    for section in sections:
        if not isinstance(section, dict):
            continue
        chapter_key = str(section.get("chapter_key") or "").strip()
        chapter_title = str(section.get("chapter_title") or chapter_key).strip()
        if chapter_key and chapter_key != current_chapter:
            blocks.append(f"## {chapter_title or chapter_key}")
            current_chapter = chapter_key
        section_title = str(section.get("section_title") or section.get("section_id") or "小节").strip()
        content = str(section.get("content") or "").strip()
        if content:
            blocks.append(f"### {section_title}\n\n{content}")

    reference_lines = [
        f"[{item.get('index')}] {item.get('citation')}"
        for item in references
        if isinstance(item, dict) and str(item.get("citation") or "").strip()
    ]
    if reference_lines:
        blocks.append("## 参考文献\n\n" + "\n".join(reference_lines))
    return "\n\n".join(blocks).strip() + ("\n" if blocks else "")

