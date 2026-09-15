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

# Semantic Scholar 常用的 40 位十六进制编号。
SEMANTIC_SCHOLAR_ID_PATTERN = re.compile(r"^[a-f0-9]{40}$", re.I)


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


def citation_lookup_keys(raw: str) -> set[str]:
    """把回复里写下的一个编号展开成各种对照写法。

    中文说明：
    助手经常把 DOI 的大小写写得和工作区主键不一样，或者把 arXiv 论文写成
    10.48550/arXiv.xxxx。这里把这些写法收成同一组小写键，后面查工作区时
    对上任意一个就算同一篇。
    """

    text = str(raw or "").strip()
    if not text:
        return set()
    keys = {text, text.lower()}
    stripped = _strip_citation_wrappers(text)
    if stripped:
        keys.add(stripped)
        keys.add(stripped.lower())
    doi = normalize_doi(stripped)
    if doi.startswith("10.") and "/" in doi:
        keys.add(doi)
        keys.add(f"doi:{doi}")
        if doi.startswith(ARXIV_DOI_PREFIX):
            keys.update(_arxiv_citation_keys(doi[len(ARXIV_DOI_PREFIX) :]))
    lowered = stripped.lower()
    if lowered.startswith("arxiv:"):
        keys.update(_arxiv_citation_keys(lowered.split(":", 1)[-1]))
    elif ARXIV_ID_PATTERN.match(lowered):
        keys.update(_arxiv_citation_keys(lowered))
    openalex_url = re.search(r"openalex\.org/(W\d+)", text, flags=re.I)
    if openalex_url:
        keys.add(openalex_url.group(1))
        keys.add(openalex_url.group(1).upper())
    elif re.fullmatch(r"W\d+", stripped, flags=re.I):
        keys.add(stripped)
        keys.add(stripped.upper())
    s2_url = re.search(r"semanticscholar\.org/paper/([a-f0-9]{40})", text, flags=re.I)
    if s2_url:
        keys.add(s2_url.group(1).lower())
    elif SEMANTIC_SCHOLAR_ID_PATTERN.match(stripped):
        keys.add(stripped.lower())
    return {item for item in keys if item}


def build_citation_lookup(papers: Mapping[str, Any]) -> dict[str, str]:
    """根据工作区论文编一份「各种写法 → 主键」对照表。"""

    lookup: dict[str, str] = {}
    for paper_id, entry in papers.items():
        canonical = str(paper_id)
        paper = entry.paper if hasattr(entry, "paper") else entry
        if not isinstance(paper, Mapping):
            paper = {"paperId": canonical}
        tokens = [canonical]
        for key in ("paperId", "id", "doi", "url", "pdf_url"):
            value = str(paper.get(key) or "").strip()
            if value:
                tokens.append(value)
        metadata = paper.get("metadata") if isinstance(paper, Mapping) else None
        if isinstance(metadata, dict):
            for meta_key in ("arxiv_id", "arxivId", "semantic_scholar_id", "openalex_id", "openalexId"):
                value = str(metadata.get(meta_key) or "").strip()
                if value:
                    tokens.append(value)
        for alias in paper_aliases(paper):
            # 中文说明：标题整理键不能拿来对引用编号，避免标题里的 W1 被当成 OpenAlex 编号。
            if str(alias).startswith("title:"):
                continue
            tokens.append(alias)
        for token in tokens:
            for key in citation_lookup_keys(token):
                lookup.setdefault(key, canonical)
    return lookup


def resolve_citation_id(candidate: str, lookup: Mapping[str, str]) -> str | None:
    """方括号里的文字若能对上工作区里的某篇论文，就返回那篇的主键。"""

    for key in citation_lookup_keys(candidate):
        hit = lookup.get(key)
        if hit:
            return hit
    return None


def _strip_citation_wrappers(raw: str) -> str:
    """去掉 DOI / arXiv 网址前缀和 doi: 前缀，只留下中间的编号。"""

    text = str(raw or "").strip()
    text = re.sub(r"^https?://(?:dx\.|www\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    text = re.sub(r"^doi:", "", text, flags=re.I)
    return text.strip("/")


def _arxiv_citation_keys(arxiv_id: str) -> set[str]:
    """arXiv 编号的几种常见写法。"""

    cleaned = _strip_arxiv_version(arxiv_id)
    if not cleaned:
        return set()
    return {
        cleaned,
        f"arxiv:{cleaned}",
        f"10.48550/arxiv.{cleaned}",
        f"doi:10.48550/arxiv.{cleaned}",
    }


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
