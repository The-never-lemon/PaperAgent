"""工作区和论文数据读取工具。

这个模块为 L1/L2 评估提供统一的工作区读取接口，包括：
- 从磁盘读取会话工作区和制品信息
- 检查论文的去重正确性（按 DOI / arxiv / 标题规范化后去重）
- 检查论文字段完整性
- 从文本中提取方括号引用并检查有效性
- 读取综述制品内容
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


# ==================== 规范化函数 ====================

def normalize_doi(doi: str) -> str:
    """将 DOI 规范化为小写并去掉前缀。

    例如 "https://doi.org/10.1234/test" → "10.1234/test"
    """
    if not doi:
        return ""

    # 去掉 https://doi.org/ 或 http://doi.org/ 前缀
    result = doi.strip()
    result = result.removeprefix("https://doi.org/").removeprefix("http://doi.org/")

    # 转小写便于比较
    return result.lower()


def normalize_arxiv_id(raw: str) -> str:
    """将 arXiv ID 规范化为标准格式（去掉版本号）。

    例如 "2401.12345v3" 或 "https://arxiv.org/abs/2401.12345" → "2401.12345"
    """
    if not raw:
        return ""

    raw = raw.strip()

    # 如果是 URL 形式，从中提取 ID
    if "arxiv.org" in raw:
        # 提取最后一段（可能是 2401.12345 或 2401.12345v3）
        raw = raw.split("/")[-1]

    # 去掉版本号（v1、v2 等）
    result = re.sub(r"v\d+$", "", raw)

    return result.lower()


def normalize_title(title: str) -> str:
    """将标题规范化为小写、去标点、压空白。

    用于标题级别的论文去重。
    """
    if not title:
        return ""

    # 转小写
    result = title.lower()

    # 去掉所有非字母数字字符（保留空格稍后处理）
    result = re.sub(r"[^\w\s]", "", result)

    # 把多个空白折叠为一个
    result = re.sub(r"\s+", " ", result).strip()

    return result


# ==================== 去重检查 ====================

def check_duplicates(papers: list[dict]) -> list[dict]:
    """检查论文列表中的重复项。

    按三个规范化键进行去重：
    1. DOI 规范化后比较
    2. arXiv ID 规范化后比较
    3. 标题规范化后比较

    返回违规明细列表。如果没有重复，返回空列表。

    Args:
        papers: PaperDocument.to_dict() 的论文列表

    Returns:
        [{"key_type": "doi|arxiv|title", "value": 规范化值, "paper_ids": [...]}, ...]
    """
    violations = []

    # 键类型 1: DOI
    doi_groups: dict[str, list[str]] = {}
    for paper in papers:
        doi = paper.get("doi", "")
        if doi:
            normalized = normalize_doi(doi)
            if normalized:
                doi_groups.setdefault(normalized, []).append(paper.get("paperId", "") or paper.get("id", ""))

    for normalized_doi, paper_ids in doi_groups.items():
        if len(paper_ids) > 1:
            violations.append({
                "key_type": "doi",
                "value": normalized_doi,
                "paper_ids": paper_ids,
            })

    # 键类型 2: arXiv ID
    arxiv_groups: dict[str, list[str]] = {}
    for paper in papers:
        paper_id = paper.get("paperId", "")
        if paper_id:
            normalized = normalize_arxiv_id(paper_id)
            # 只有看起来像 arXiv ID 的才用这个键（格式 YYMM.NNNNN）
            if re.match(r"^\d{4}\.\d{4,5}$", normalized):
                arxiv_groups.setdefault(normalized, []).append(paper.get("paperId", "") or paper.get("id", ""))

    for normalized_arxiv, paper_ids in arxiv_groups.items():
        if len(paper_ids) > 1:
            violations.append({
                "key_type": "arxiv",
                "value": normalized_arxiv,
                "paper_ids": paper_ids,
            })

    # 键类型 3: 标题
    title_groups: dict[str, list[str]] = {}
    for paper in papers:
        title = paper.get("title", "")
        if title:
            normalized = normalize_title(title)
            if normalized:
                title_groups.setdefault(normalized, []).append(paper.get("paperId", "") or paper.get("id", ""))

    for normalized_title, paper_ids in title_groups.items():
        if len(paper_ids) > 1:
            violations.append({
                "key_type": "title",
                "value": normalized_title,
                "paper_ids": paper_ids,
            })

    return violations


# ==================== 字段完整性检查 ====================

def check_field_completeness(papers: list[dict]) -> dict:
    """检查论文列表的字段完整性。

    返回各字段非空占比（0~1）。

    Args:
        papers: PaperDocument.to_dict() 的论文列表

    Returns:
        {
            "total": 论文数,
            "title": 非空占比,
            "year": 非空占比,
            "abstract": 非空占比,
            "id_doi_or_paperid": DOI 或 paperId 非空占比
        }
    """
    if not papers:
        return {
            "total": 0,
            "title": 0,
            "year": 0,
            "abstract": 0,
            "id_doi_or_paperid": 0,
        }

    total = len(papers)
    title_count = sum(1 for p in papers if p.get("title", "").strip())
    year_count = sum(1 for p in papers if p.get("year"))
    abstract_count = sum(1 for p in papers if p.get("abstract", "").strip())
    id_count = sum(1 for p in papers if p.get("doi", "").strip() or p.get("paperId", "").strip())

    return {
        "total": total,
        "title": title_count / total if total > 0 else 0,
        "year": year_count / total if total > 0 else 0,
        "abstract": abstract_count / total if total > 0 else 0,
        "id_doi_or_paperid": id_count / total if total > 0 else 0,
    }


# ==================== 论文引用提取和验证 ====================

# 用于从文本中提取方括号引用的正则
# \[([^\[\]]+)\] 匹配 [...] 内的内容
# (?!\() 负向前瞻，确保后面不紧跟 (，以排除 Markdown 链接 [...](...)
_BRACKET_CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\](?!\()")

# 论文 ID 的合法形式（只包含字母、数字、下划线、连字符、点、斜杠、冒号）
_PAPER_ID_SHAPE_PATTERN = re.compile(r"^[A-Za-z0-9_\-./:]+$")

# 占位词不算作论文引用
_CITATION_PLACEHOLDER_WORDS = {"todo", "note", "fixme"}


def extract_cited_paper_ids(text: str) -> list[str]:
    """从文本中提取方括号内的论文 ID 引用。

    按 researchAgent 的规范，提取形如 [paper_id] 的引用。
    过滤规则：
    1. 排除 Markdown 链接 [...](...)
    2. 只保留符合编号长相的（字母/数字/下划线/连字符/点/斜杠/冒号）
    3. 排除占位词（[TODO]、[NOTE]、[FIXME]）
    4. 至少 2 个字符且不是纯数字

    Args:
        text: 原始文本

    Returns:
        [paper_id, ...] 去重保序
    """
    if not text:
        return []

    # 提取所有方括号引用
    matches = _BRACKET_CITATION_PATTERN.findall(text)
    candidate_ids = []

    for match in matches:
        candidate = match.strip()

        # 过滤：太短或纯数字不算论文编号
        if len(candidate) < 2 or candidate.isdigit():
            continue

        # 过滤：不符合编号长相的不算
        if not _PAPER_ID_SHAPE_PATTERN.match(candidate):
            continue

        # 过滤：占位词不算
        if candidate.lower() in _CITATION_PLACEHOLDER_WORDS:
            continue

        candidate_ids.append(candidate)

    # 去重保序
    seen = set()
    result = []
    for pid in candidate_ids:
        if pid not in seen:
            seen.add(pid)
            result.append(pid)

    return result


def check_citation_validity(text: str, workspace_papers: dict) -> dict:
    """检查文本中引用的论文 ID 在工作区中是否有效。

    Args:
        text: 回复文本
        workspace_papers: 工作区论文字典 {paper_id: paper_data, ...}

    Returns:
        {
            "cited": 引用数,
            "valid": 有效引用数,
            "invalid_ids": [无效的 paper_id 列表],
            "valid_rate": 有效率（0~1）
        }
    """
    cited_ids = extract_cited_paper_ids(text)

    if not cited_ids:
        return {
            "cited": 0,
            "valid": 0,
            "invalid_ids": [],
            "valid_rate": 1.0,  # 没有引用，视为无问题
        }

    # 工作区中实际存在的论文 ID
    known_ids = set(workspace_papers.keys())

    # 找出无效的引用
    invalid_ids = [pid for pid in cited_ids if pid not in known_ids]
    valid_count = len(cited_ids) - len(invalid_ids)
    valid_rate = valid_count / len(cited_ids) if cited_ids else 0

    return {
        "cited": len(cited_ids),
        "valid": valid_count,
        "invalid_ids": invalid_ids,
        "valid_rate": valid_rate,
    }


# ==================== 工作区和制品读取 ====================

def load_session_workspace(session_key: str, sessions_root: Path | None = None) -> dict:
    """读取会话的工作区论文数据。

    从 data/sessions/{key}/workspace/papers.json 读取，文件不存在返回空字典。

    Args:
        session_key: 会话 ID
        sessions_root: 会话根目录，默认为 data/sessions

    Returns:
        工作区论文字典 {paper_id: paper_data, ...}
    """
    if sessions_root is None:
        repo_root = Path(__file__).resolve().parents[2]  # eval/lib/workspace_reader.py 往上 2 级
        sessions_root = repo_root / "data" / "sessions"

    workspace_path = sessions_root / session_key / "workspace" / "papers.json"

    if not workspace_path.exists():
        return {}

    try:
        with open(workspace_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_session_artifact(session_key: str, artifact_id: str) -> dict | None:
    """读取会话的单个制品文件。

    从 artifacts/{artifact_id}.json 读取，容错处理。

    Args:
        session_key: 会话 ID（未在本函数中使用，保留作接口兼容）
        artifact_id: 制品 ID

    Returns:
        制品内容字典，或 None（文件不存在或解析失败）
    """
    repo_root = Path(__file__).resolve().parents[2]
    artifact_path = repo_root / "artifacts" / f"{artifact_id}.json"

    if not artifact_path.exists():
        return None

    try:
        with open(artifact_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_review_artifact_text(session_key: str, artifact_id: str) -> str:
    """读取综述制品的文本内容。

    综述可能是 .md 格式或 .json 格式，自动检测并返回正文。

    Args:
        session_key: 会话 ID
        artifact_id: 制品 ID

    Returns:
        正文文本，文件不存在或解析失败返回空字符串
    """
    repo_root = Path(__file__).resolve().parents[2]

    # 尝试读 JSON 格式
    artifact_dict = load_session_artifact(session_key, artifact_id)
    if artifact_dict:
        # 尝试从 content 字段提取
        if isinstance(artifact_dict, dict):
            if "content" in artifact_dict:
                return str(artifact_dict["content"])
            # 或者直接返回整个字典的字符串化版本
            return json.dumps(artifact_dict, ensure_ascii=False)

    # 尝试读 .md 格式
    md_path = repo_root / "artifacts" / f"{artifact_id}.md"
    if md_path.exists():
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass

    return ""
