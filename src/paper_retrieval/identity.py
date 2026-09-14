"""判断两篇检索结果是不是同一篇论文。

中文说明：
以前检索合并、工作区去重、本地缓存目录各用各的编号，同一篇论文换个数据源
就会被当成新论文。这里把「这篇论文是谁」收成一套规则，三处都来问这里。

认同一篇论文的优先顺序：
1. 正规 DOI（去掉 https://doi.org/ 前缀，字母一律小写）；
2. arXiv 编号（去掉末尾的 v3 这种版本号）；
3. 把标题整理成小写、去掉标点之后的结果（两边都有年份时，年份还必须相同）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.paper_retrieval.models import PaperDocument


JsonObject = dict[str, Any]
PaperLike = PaperDocument | Mapping[str, Any]

# arXiv 给论文注册的 DOI 前缀，例如 10.48550/arxiv.2401.12345。
# 这种 DOI 不能当成普通期刊 DOI，否则和 arXiv 源返回的裸编号合不到一起。
ARXIV_DOI_PREFIX = "10.48550/arxiv."

# 2007 年之后的 arXiv 编号长相：2401.12345 或 2401.12345v3。
ARXIV_ID_PATTERN = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")


def paper_key(paper: PaperLike) -> str:
    """生成检索合并用的主编号。

    中文说明：
    一篇论文只应有一个主编号。有 DOI 就用 DOI；DOI 其实是 arXiv 自家注册的，
    就改记成 arXiv 编号；都没有再退回整理过的标题。
    """

    doi_key = _doi_key(paper)
    if doi_key:
        return doi_key
    arxiv_key = _arxiv_key_from_ids(paper)
    if arxiv_key:
        return arxiv_key
    return _title_key(paper)


def paper_aliases(paper: PaperLike) -> set[str]:
    """收集一篇论文所有能用来对上号的名字。

    中文说明：
    主编号只管「合并时留哪一条」。查找时要把这篇论文曾经用过的编号都算上：
    裸 arXiv 号、带版本号的号、DOI、工作区里存的 paperId、本地上传的哈希编号，
    以及整理过的标题。后面记忆索引和工作区去重都靠这组名字对上同一篇。
    """

    aliases: set[str] = set()
    key = paper_key(paper)
    if key:
        aliases.add(key)

    doi_key = _doi_key(paper)
    if doi_key:
        aliases.add(doi_key)

    arxiv_key = _arxiv_key_from_ids(paper)
    if arxiv_key:
        aliases.add(arxiv_key)

    title_key = _title_key(paper)
    if title_key:
        aliases.add(title_key)

    # 中文说明：工作区和缓存目录现在仍按原始 paperId 起名，查找时必须把
    # 这些原始字符串也收进来，否则旧目录对不上新检索结果。
    for raw in _raw_id_values(paper):
        aliases.add(raw)
        stripped_version = _strip_arxiv_version(raw)
        if stripped_version != raw:
            aliases.add(stripped_version)
        if ARXIV_ID_PATTERN.match(raw) or ARXIV_ID_PATTERN.match(stripped_version):
            aliases.add(f"arxiv:{_strip_arxiv_version(raw)}")
    return {item for item in aliases if item}


def papers_match(left: PaperLike, right: PaperLike) -> bool:
    """判断两份论文元数据是不是同一篇论文。

    中文说明：
    先看有没有相同的「硬编号」（DOI、arXiv、原始 paperId）。对上了就是同一篇。
    只有标题对得上时更小心：两边都写了年份、但年份不一样，就不当同一篇，
    避免两篇同名文章被误合成一条。
    """

    left_aliases = paper_aliases(left)
    right_aliases = paper_aliases(right)
    strong_left = {item for item in left_aliases if not item.startswith("title:")}
    strong_right = {item for item in right_aliases if not item.startswith("title:")}
    if strong_left & strong_right:
        return True
    title_left = {item for item in left_aliases if item.startswith("title:")}
    title_right = {item for item in right_aliases if item.startswith("title:")}
    if not (title_left & title_right):
        return False
    year_left = paper_year(left)
    year_right = paper_year(right)
    if year_left is not None and year_right is not None and year_left != year_right:
        return False
    return True


def normalize_doi(value: str) -> str:
    """把 DOI 整理成可比较的形式：去掉网址前缀，字母改小写。"""

    text = str(value or "").strip()
    if not text:
        return ""
    text = text.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    text = text.removeprefix("https://dx.doi.org/").removeprefix("http://dx.doi.org/")
    return text.lower()


def _doi_key(paper: PaperLike) -> str:
    """从 DOI 生成主编号；arXiv 自家 DOI 改记成 arXiv 编号。"""

    doi = normalize_doi(_field(paper, "doi"))
    if not doi:
        return ""
    if doi.startswith(ARXIV_DOI_PREFIX):
        arxiv_id = _strip_arxiv_version(doi[len(ARXIV_DOI_PREFIX) :])
        return f"arxiv:{arxiv_id}" if arxiv_id else ""
    return f"doi:{doi}"


def _arxiv_key_from_ids(paper: PaperLike) -> str:
    """从 paperId / id / 元数据里的 arXiv 编号生成 arxiv: 主编号。"""

    for raw in _raw_id_values(paper):
        candidate = str(raw or "").strip()
        lowered = candidate.lower()
        if lowered.startswith("arxiv:"):
            candidate = candidate.split(":", 1)[-1].strip()
        else:
            normalized = normalize_doi(candidate)
            if normalized.startswith(ARXIV_DOI_PREFIX):
                candidate = normalized[len(ARXIV_DOI_PREFIX) :]
        if ARXIV_ID_PATTERN.match(candidate):
            return f"arxiv:{_strip_arxiv_version(candidate)}"
    return ""


def _title_key(paper: PaperLike) -> str:
    """把标题整理成查找用的键：小写、去掉标点、连续空格收成一个。"""

    title = str(_field(paper, "title") or "").strip().lower()
    if not title:
        return ""
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return f"title:{title}" if title else ""


def _raw_id_values(paper: PaperLike) -> list[str]:
    """取出论文上所有「看起来像编号」的原始字符串。"""

    values: list[str] = []
    for key in ("paperId", "id", "doi"):
        text = str(_field(paper, key) or "").strip()
        if text:
            values.append(text)
    metadata = _metadata(paper)
    arxiv_id = str(metadata.get("arxiv_id") or metadata.get("arxivId") or "").strip()
    if arxiv_id:
        values.append(arxiv_id)
    return values


def paper_year(paper: PaperLike) -> int | None:
    """取出论文年份。工作区里没填年份时存的是空字符串，这里当成「没有年份」。"""

    raw = _field(paper, "year")
    if raw in (None, ""):
        return None
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return None
    return year if year > 0 else None


def _strip_arxiv_version(value: str) -> str:
    """去掉 arXiv 编号末尾的 v3 这种版本号。"""

    return re.sub(r"v\d+$", "", str(value or "").strip())


def _field(paper: PaperLike, name: str) -> Any:
    """同时支持 PaperDocument 对象和工作区里的普通字典。"""

    if isinstance(paper, PaperDocument):
        if name == "arxiv_id":
            return (paper.metadata or {}).get("arxiv_id")
        return getattr(paper, name, None)
    payload = paper if isinstance(paper, Mapping) else {}
    if name == "arxiv_id":
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return metadata.get("arxiv_id") or metadata.get("arxivId")
    return payload.get(name)


def _metadata(paper: PaperLike) -> JsonObject:
    """取出论文附带的额外信息字典。"""

    if isinstance(paper, PaperDocument):
        return dict(paper.metadata or {})
    payload = paper if isinstance(paper, Mapping) else {}
    metadata = payload.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}
