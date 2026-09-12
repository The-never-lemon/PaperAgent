"""工作区导出服务（包 4 新增）。

把会话工作区的论文清单导出成三种格式：
- BibTeX：可直接导入 Zotero / JabRef
- Markdown：可读的论文清单
- CSV：可在 Excel 打开

纯投影逻辑：读工作区 → 选字段 → 拼字符串。不启动 run，不消耗模型。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any

from src.models.workspace import SessionWorkspace


JsonObject = dict[str, Any]


def export_workspace(
    workspace: SessionWorkspace,
    *,
    format: str = "markdown",
    paper_ids: list[str] | None = None,
) -> tuple[str, str, str]:
    """导出工作区论文清单。

    Returns:
        (content_type, filename, content) 三元组。
    """

    # 选出要导出的论文。
    if paper_ids:
        entries = [(pid, workspace.papers[pid]) for pid in paper_ids if pid in workspace.papers]
    else:
        entries = sorted(workspace.papers.items(), key=lambda pair: pair[1].added_at)

    if format == "bibtex":
        content = _export_bibtex(entries)
        return "application/x-bibtex; charset=utf-8", "papers.bib", content
    elif format == "csv":
        content = _export_csv(entries)
        return "text/csv; charset=utf-8", "papers.csv", content
    else:  # markdown
        content = _export_markdown(entries, workspace.research_topic)
        return "text/markdown; charset=utf-8", "papers.md", content


def _export_bibtex(entries: list[tuple[str, Any]]) -> str:
    """导出 BibTeX 格式。"""

    lines: list[str] = []
    for paper_id, entry in entries:
        paper = entry.paper
        # 生成 entry key：第一作者姓_年份_标题首词。
        authors = list(paper.get("authors") or [])
        first_author = _ascii_slug(authors[0].split()[-1]) if authors else "unknown"
        year = str(paper.get("year") or "nodate")
        title_words = str(paper.get("title") or "").split()
        first_title = _ascii_slug(title_words[0]) if title_words else "paper"
        key = f"{first_author}{year}{first_title}"

        # 判定 entry type。
        venue = str(paper.get("venue") or paper.get("journal_conference") or "")
        if "journal" in venue.lower() or "transactions" in venue.lower():
            entry_type = "article"
        elif "conference" in venue.lower() or "proceedings" in venue.lower() or "symposium" in venue.lower():
            entry_type = "inproceedings"
        else:
            entry_type = "misc"

        lines.append(f"@{entry_type}{{{key},")
        lines.append(f"  title = {{{paper.get('title', '')}}},")
        if authors:
            lines.append(f"  author = {{{' and '.join(authors)}}},")
        if paper.get("year"):
            lines.append(f"  year = {{{paper['year']}}},")
        if venue:
            if entry_type == "article":
                lines.append(f"  journal = {{{venue}}},")
            elif entry_type == "inproceedings":
                lines.append(f"  booktitle = {{{venue}}},")
            else:
                lines.append(f"  howpublished = {{{venue}}},")
        if paper.get("doi"):
            lines.append(f"  doi = {{{paper['doi']}}},")
        if paper.get("url"):
            lines.append(f"  url = {{{paper['url']}}},")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def _export_csv(entries: list[tuple[str, Any]]) -> str:
    """导出 CSV 格式。"""

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["paper_id", "title", "authors", "year", "venue", "source", "score", "status", "url"])
    for paper_id, entry in entries:
        paper = entry.paper
        authors = list(paper.get("authors") or [])
        writer.writerow([
            paper_id,
            paper.get("title", ""),
            "; ".join(authors),
            paper.get("year", ""),
            paper.get("venue", paper.get("journal_conference", "")),
            paper.get("source", ""),
            entry.evaluation.score if entry.evaluation else "",
            entry.status(),
            paper.get("url", ""),
        ])
    return output.getvalue()


def _export_markdown(entries: list[tuple[str, Any]], topic: str) -> str:
    """导出 Markdown 格式。"""

    lines = [f"# 论文清单：{topic}", "", f"共 {len(entries)} 篇论文。", ""]
    for i, (paper_id, entry) in enumerate(entries, start=1):
        paper = entry.paper
        title = paper.get("title", "")
        authors = list(paper.get("authors") or [])
        year = paper.get("year", "")
        venue = paper.get("venue", paper.get("journal_conference", ""))
        url = paper.get("url", "")

        author_str = ", ".join(authors[:3])
        if len(authors) > 3:
            author_str += " 等"

        lines.append(f"## {i}. {title}")
        meta_parts = []
        if author_str:
            meta_parts.append(f"**作者**：{author_str}")
        if year:
            meta_parts.append(f"**年份**：{year}")
        if venue:
            meta_parts.append(f"**出处**：{venue}")
        if entry.evaluation:
            meta_parts.append(f"**评分**：{entry.evaluation.score}")
        meta_parts.append(f"**状态**：{entry.status()}")
        lines.append(" | ".join(meta_parts))
        if url:
            lines.append(f"**链接**：{url}")
        lines.append("")
    return "\n".join(lines)


def _ascii_slug(text: str) -> str:
    """把文本转成 ASCII 友好的 slug（用于 BibTeX entry key）。"""

    # NFKD 归一化，去掉重音符号。
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    # 只保留字母数字。
    text = re.sub(r"[^a-zA-Z0-9]", "", text)
    return text.lower()[:20]  # 截断到 20 字符，避免 key 太长
