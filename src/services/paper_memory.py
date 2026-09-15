"""本机论文长期记忆：记住已经切过分片、并且全文精读过的论文。

中文说明：
分片和全文放在 data/paper_cache 里。以前用 data/paper_memory/index.json 记
「这篇论文是谁」，精读报告另存一份到缓存目录。现在身份和别名改记在会话库
同一个 SQLite 里，并记下每个会话工作区引用了哪一篇。

只有「全文精读 + 分片还有效」才当作可以召回的记忆。只有摘要的降级报告不记。
工作区删论文只取消当前会话的引用；清掉 paper_cache 时卡片还在，已精读变回可精读。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from src.llm.config import SystemConfig
from src.models.deep_read import DEEP_READ_SOURCE_FULLTEXT, DeepReadReport
from src.models.sessions import utc_now
from src.models.workspace import WORKSPACE_FILE_NAME, SessionWorkspace, sanitize_for_filename
from src.paper_retrieval.identity import (
    PaperLike,
    citation_lookup_keys,
    paper_aliases,
    paper_key,
    paper_year,
    papers_match,
)
from src.utils import get_logger
from src.utils.read_utils.cache import paper_cache_dir, safe_cache_name
from src.utils.read_utils.chunkers import CHUNKER_VERSION, PageChunker, load_chunks_file


if TYPE_CHECKING:
    from src.repositories.sessions.base import SessionRepository
    from src.repositories.sessions.sqlite import SessionStoreBackend


JsonObject = dict[str, Any]
logger = get_logger(__name__)

# 旧索引文件名。启动时如果目录表还是空的，会从这里迁进 SQLite，之后不再当主存储。
INDEX_FILE_NAME = "index.json"

# 全局报告文件名，和 chunk.json、paper.md 放在同一篇论文的缓存目录里。
REPORT_FILE_NAME = "deep_read.json"

# 产物类型和精读模块保持一致，召回时写入当前会话才打得开。
ARTIFACT_TYPE_REPORT = "deep_read_report"
ARTIFACT_TYPE_FULLTEXT = "paper_fulltext"
ARTIFACT_TYPE_FIGURE = "paper_figure"

# 同一进程里迁目录、清缓存时加锁，避免两次写入互相踩。
# 用可重入锁：迁目录过程中还会再去查目录，不能把自己卡住。
_INDEX_LOCK = threading.RLock()

# 会话库里的论文目录后端。应用启动时由会话仓储注入；测试可换成临时库。
_CATALOG_BACKEND: SessionStoreBackend | None = None
_MIGRATED = False
_MIGRATING = False


@dataclass(slots=True)
class PaperMemoryEntry:
    """本机已经记住的一篇论文。"""

    memory_id: str
    aliases: list[str] = field(default_factory=list)
    cache_dir: str = ""
    title: str = ""
    year: int | None = None
    has_chunks: bool = False
    cache_present: bool = False
    recorded_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: Any) -> "PaperMemoryEntry | None":
        """从旧索引文件或目录表字典还原一条记忆。字段缺失时返回空。"""

        if not isinstance(data, dict):
            return None
        memory_id = str(data.get("memory_id") or data.get("id") or "").strip()
        cache_dir = str(data.get("cache_dir") or "").strip()
        if not memory_id:
            return None
        year_raw = data.get("year")
        year: int | None
        try:
            year = int(year_raw) if year_raw not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        aliases = [
            str(item).strip()
            for item in (data.get("aliases") or [])
            if str(item).strip()
        ]
        cache_present = data.get("cache_present")
        if cache_present is None:
            cache_present = bool(cache_dir)
        return cls(
            memory_id=memory_id,
            aliases=aliases or [memory_id],
            cache_dir=cache_dir,
            title=str(data.get("title") or ""),
            year=year if year and year > 0 else None,
            has_chunks=bool(data.get("has_chunks")),
            cache_present=bool(cache_present),
            recorded_at=str(data.get("recorded_at") or "") or utc_now(),
        )


@dataclass(slots=True)
class MemoryImportResult:
    """把一篇记忆导入当前会话之后的结果。"""

    recalled: bool
    has_chunks: bool
    report: DeepReadReport | None = None


def configure_paper_catalog(backend: SessionStoreBackend | None) -> None:
    """指定本机论文目录用哪一个 SQLite 后端。"""

    global _CATALOG_BACKEND, _MIGRATED, _MIGRATING
    _CATALOG_BACKEND = backend
    _MIGRATED = False
    _MIGRATING = False


def migrate_paper_catalog_if_needed() -> None:
    """目录表还空时，把旧的 index.json 和本机已有精读迁进 SQLite。"""

    global _MIGRATED, _MIGRATING
    if _MIGRATED or _MIGRATING:
        return
    with _INDEX_LOCK:
        if _MIGRATED or _MIGRATING:
            return
        _MIGRATING = True
        try:
            backend = _catalog()
            if not backend.paper_catalog_is_empty():
                _MIGRATED = True
                return
            index_path = _index_path()
            if index_path.is_file():
                _import_index_file(backend, index_path)
            elif _is_default_storage(backend):
                _rebuild_from_disk(backend)
            _backfill_paper_sessions(backend)
            _MIGRATED = True
            logger.info("本机论文目录已就绪", extra={"storage_root": str(backend.storage_root)})
        finally:
            _MIGRATING = False


def lookup_memory(paper: PaperLike | str) -> PaperMemoryEntry | None:
    """按论文元数据或一个编号字符串查找本机记忆。找不到返回空。

    中文说明：
    写作工具往往只拿到工作区里的编号字符串，比如 DOI 原文，而目录表里存的
    可能是 doi: 前缀或缓存目录名。字符串不能只做精确匹配，要按同一套编号
    规则展开后再查。
    """

    migrate_paper_catalog_if_needed()
    backend = _catalog()
    if isinstance(paper, str):
        text = paper.strip()
        if not text:
            return None
        payload = backend.lookup_paper_by_alias(text)
        if payload:
            return PaperMemoryEntry.from_dict(payload)
        names = _names_from_id_string(text)
        hits = backend.lookup_papers_by_aliases(list(names))
        return _pick_catalog_entry(hits, names)

    names = _lookup_names(paper)
    hits = backend.lookup_papers_by_aliases(list(names))
    return _pick_catalog_entry(hits, names, year=paper_year(paper))


def resolve_paper_cache_dir(base_dir: str | Path, paper: PaperLike | str) -> Path:
    """找出这篇论文真正的缓存目录。

    中文说明：
    以前目录名跟着当时的 paperId 走，后来换个数据源编号变了，就会去一个空目录。
    这里先查目录表里记下的目录名；没有记录再退回按当前编号起名。
    表里说有缓存、磁盘上却没了，就按「缓存已失效」处理。
    """

    root = Path(base_dir)
    entry = lookup_memory(paper)
    if entry and entry.cache_dir:
        candidate = root / entry.cache_dir
        if candidate.is_dir():
            return candidate
        if entry.cache_present or entry.has_chunks:
            _invalidate_missing_cache(entry)
    if isinstance(paper, str):
        return root / safe_cache_name(paper)
    from src.paper_retrieval.models import PaperDocument

    if isinstance(paper, PaperDocument):
        return paper_cache_dir(root, paper)
    doc = _paper_document(paper)
    if doc is None:
        return root / safe_cache_name(str(paper.get("paperId") or paper.get("id") or "paper"))
    return paper_cache_dir(root, doc)


def record_fulltext_deep_read(paper: PaperLike, report: DeepReadReport) -> bool:
    """全文精读成功后，把报告写入论文缓存并登记到长期记忆。"""

    if report.source != DEEP_READ_SOURCE_FULLTEXT:
        return False
    migrate_paper_catalog_if_needed()
    cache_root = _cache_root()
    cache_dir = _locate_cache_dir_on_disk(paper, cache_root)
    if cache_dir is None or not chunks_are_usable(cache_dir):
        logger.info("精读完成但分片不可用，不写入长期记忆", extra={"memory_id": paper_key(paper)})
        return False
    stored = DeepReadReport.from_dict(report.to_dict())
    stored.artifact_id = ""
    stored.fulltext_artifact_id = ""
    _write_json(cache_dir / REPORT_FILE_NAME, stored.to_dict())
    catalog_id = ensure_bound("", "", paper, cache_dir=cache_dir.name, has_chunks=True, cache_present=True)
    logger.info(
        "已把全文精读写入长期记忆",
        extra={"memory_id": catalog_id or paper_key(paper), "cache_dir": cache_dir.name},
    )
    return True


def ensure_bound(
    session_key: str,
    workspace_paper_id: str,
    paper: PaperLike | str,
    *,
    cache_dir: str = "",
    has_chunks: bool = False,
    cache_present: bool = False,
) -> str:
    """把一篇论文写进本机目录，并（如有会话编号）记下当前工作区引用了它。"""

    migrate_paper_catalog_if_needed()
    backend = _catalog()
    if isinstance(paper, str):
        paper_id = paper.strip()
        # 中文说明：只传编号字符串时也要展开成 DOI / arXiv / 目录名，
        # 否则目录表里已有 doi: 前缀那条时，这里会再插一条重复记录。
        aliases = sorted(_names_from_id_string(paper_id))
        title = ""
        year = None
        key = paper_id
    else:
        key = paper_key(paper) or str(workspace_paper_id or "").strip()
        aliases = sorted(_lookup_names(paper) | {str(workspace_paper_id or "").strip()})
        title = str(paper.get("title") if isinstance(paper, dict) else getattr(paper, "title", "") or "")
        year = paper_year(paper)
        paper_id = key or str(workspace_paper_id or "").strip()
    if cache_dir:
        aliases.append(cache_dir)
    aliases = [item for item in dict.fromkeys(aliases) if item]
    if not paper_id:
        return ""
    catalog_id = backend.upsert_paper_catalog(
        paper_id=paper_id,
        cache_dir=cache_dir,
        title=title,
        year=year,
        has_chunks=has_chunks,
        cache_present=cache_present,
        aliases=aliases,
    )
    if session_key and workspace_paper_id and catalog_id:
        backend.bind_paper_session(session_key, workspace_paper_id, catalog_id)
    return catalog_id


def unbind_session_papers(session_key: str, workspace_paper_ids: list[str]) -> None:
    """取消当前会话对若干论文的引用。本机缓存和其他会话都不动。"""

    migrate_paper_catalog_if_needed()
    _catalog().unbind_paper_session(session_key, workspace_paper_ids)


def import_memory_into_session(
    *,
    paper_id: str,
    paper: JsonObject,
    workspace: "SessionWorkspace",
    repo: "SessionRepository",
    session_key: str,
) -> MemoryImportResult:
    """若本机已有这篇论文的全文精读，就拷进当前会话工作区。"""

    ensure_bound(session_key, paper_id, paper)
    existing = workspace.get_paper(paper_id)
    if existing is not None and existing.deep_read is not None:
        if existing.deep_read.source == DEEP_READ_SOURCE_FULLTEXT:
            memory = lookup_memory(paper)
            has_chunks = bool(
                existing.fulltext_cached
                or (memory is not None and memory.has_chunks and memory.cache_present)
            )
            return MemoryImportResult(recalled=False, has_chunks=has_chunks, report=existing.deep_read)

    entry = lookup_memory(paper)
    if entry is None or not entry.has_chunks or not entry.cache_present:
        return MemoryImportResult(recalled=False, has_chunks=False)
    cache_dir = _cache_root() / entry.cache_dir
    if not cache_dir.is_dir():
        _invalidate_missing_cache(entry)
        return MemoryImportResult(recalled=False, has_chunks=False)
    if not chunks_are_usable(cache_dir):
        return MemoryImportResult(recalled=False, has_chunks=False)
    report = _load_report_file(cache_dir / REPORT_FILE_NAME)
    if report is None or report.source != DEEP_READ_SOURCE_FULLTEXT:
        return MemoryImportResult(recalled=False, has_chunks=True)

    markdown_text = _read_text(cache_dir / "paper.md")
    assets_dir = cache_dir / "assets"
    if markdown_text:
        markdown_text = _copy_figure_artifacts(
            repo=repo,
            session_key=session_key,
            paper_id=paper_id,
            assets_dir=assets_dir if assets_dir.is_dir() else None,
            markdown_text=markdown_text,
        )

    report.paper_id = paper_id
    report.artifact_id = ""
    report.fulltext_artifact_id = ""
    safe = sanitize_for_filename(paper_id)
    if markdown_text:
        fulltext_record = repo.write_artifact(
            session_key,
            ARTIFACT_TYPE_FULLTEXT,
            f"paper_{safe}.md",
            markdown_text,
            relative_path=f"artifacts/paper_{safe}.md",
            metadata={"paper_id": paper_id, "recalled": True},
        )
        report.fulltext_artifact_id = str(fulltext_record.get("id") or "")
    report_record = repo.write_artifact(
        session_key,
        ARTIFACT_TYPE_REPORT,
        f"deep_read_{safe}.json",
        json.dumps(report.to_dict(), ensure_ascii=False, indent=1),
        relative_path=f"artifacts/deep_read_{safe}.json",
        metadata={"paper_id": paper_id, "recalled": True},
    )
    report.artifact_id = str(report_record.get("id") or "")
    workspace.set_deep_read(paper_id, report)
    workspace.set_fulltext_cached(paper_id, True)
    logger.info(
        "已从长期记忆召回精读报告",
        extra={"session_key": session_key, "paper_id": paper_id, "memory_id": entry.memory_id},
    )
    return MemoryImportResult(recalled=True, has_chunks=True, report=report)


def purge_paper_cache(
    *,
    session_key: str,
    workspace_paper_id: str,
    paper: JsonObject | None = None,
) -> JsonObject:
    """删掉一篇论文的本机缓存，所有会话里的卡片留下、已精读改回可精读。"""

    migrate_paper_catalog_if_needed()
    backend = _catalog()
    catalog_id = backend.resolve_session_paper_id(session_key, workspace_paper_id)
    entry = PaperMemoryEntry.from_dict(backend.get_paper_catalog(catalog_id)) if catalog_id else None
    if entry is None and paper:
        entry = lookup_memory(paper)
    if entry is None:
        entry = lookup_memory(workspace_paper_id)
    if entry is None:
        # 中文说明：目录表还没记下这篇时，也要把当前会话里的精读状态改回去，
        # 并尽量删掉磁盘上对得上的缓存文件夹。
        updated = 0
        sessions_root = Path(backend.sessions_dir)
        try:
            workspace = SessionWorkspace.load(session_key, sessions_root)
        except Exception:
            workspace = None
        if workspace is not None and workspace.invalidate_local_fulltext(workspace_paper_id):
            updated += 1
        backend.delete_artifacts_for_paper(session_key, workspace_paper_id)
        cache_dir = _locate_cache_dir_on_disk(paper, _cache_root()) if paper else None
        if cache_dir is None:
            named = _cache_root() / safe_cache_name(workspace_paper_id)
            cache_dir = named if named.is_dir() else None
        deleted = False
        if cache_dir is not None and cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            deleted = not cache_dir.exists()
        return {
            "paper_id": workspace_paper_id,
            "workspace_paper_id": workspace_paper_id,
            "cache_deleted": deleted,
            "sessions_updated": updated,
        }

    cache_dir = _cache_root() / entry.cache_dir if entry.cache_dir else None
    deleted = False
    if cache_dir is not None and cache_dir.is_dir():
        shutil.rmtree(cache_dir, ignore_errors=True)
        deleted = not cache_dir.exists()
        if not deleted and cache_dir.is_dir():
            logger.warning("删除论文缓存目录失败", extra={"cache_dir": str(cache_dir)})
    backend.mark_paper_cache(entry.memory_id, has_chunks=False, cache_present=False)
    updated = _invalidate_paper_in_workspaces(entry, extra_ids=[(session_key, workspace_paper_id)])
    logger.info(
        "已清除本机论文缓存",
        extra={"paper_id": entry.memory_id, "sessions_updated": updated, "cache_deleted": deleted},
    )
    return {
        "paper_id": entry.memory_id,
        "workspace_paper_id": workspace_paper_id,
        "cache_deleted": deleted or cache_dir is None or not cache_dir.exists(),
        "sessions_updated": updated,
    }


def sync_workspace_missing_cache(workspace: SessionWorkspace) -> int:
    """打开工作区时检查：标记有全文或已精读，但缓存目录没了，就改回可精读。

    中文说明：
    这里只动当前这个会话。目录表里把「本地还有全文」关掉，当前卡片的精读
    报告清掉。不去扫别的会话文件夹，打开工作区才不会越来越慢。其它会话
    下次打开、或再检索/精读/追问这篇时，会再走同一套检查。
    """

    migrate_paper_catalog_if_needed()
    updated = 0
    cache_root = _cache_root()
    backend = _catalog()
    for paper_id, item in workspace.papers.items():
        if not item.fulltext_cached and item.deep_read is None:
            continue
        entry = lookup_memory(item.paper) or lookup_memory(paper_id)
        if entry is None:
            if workspace.invalidate_local_fulltext(paper_id):
                updated += 1
            continue
        # 中文说明：还没记下缓存目录时，不能把缓存根目录本身当成这篇论文的文件夹。
        if not entry.cache_dir:
            continue
        cache_dir = cache_root / entry.cache_dir
        if cache_dir.is_dir():
            continue
        if workspace.invalidate_local_fulltext(paper_id):
            updated += 1
        backend.mark_paper_cache(entry.memory_id, has_chunks=False, cache_present=False)
        backend.delete_artifacts_for_paper(workspace.session_key, paper_id)
        if entry.memory_id and entry.memory_id != paper_id:
            backend.delete_artifacts_for_paper(workspace.session_key, entry.memory_id)
    return updated


def chunks_are_usable(cache_dir: Path) -> bool:
    """判断缓存目录里的分片还能不能用：文件在、版本对、没有切坏的超长块。"""

    chunks_path = cache_dir / "chunk.json"
    if not chunks_path.is_file():
        return False
    try:
        payload = json.loads(chunks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get("version") != CHUNKER_VERSION:
        return False
    chunks = load_chunks_file(chunks_path)
    if not chunks:
        return False
    return all(len(chunk.content) <= PageChunker.max_atomic_characters for chunk in chunks)


def _catalog():
    """取出当前的论文目录后端；还没注入时用默认的 data 目录。"""

    global _CATALOG_BACKEND
    if _CATALOG_BACKEND is not None:
        return _CATALOG_BACKEND
    from src.repositories.sessions.sqlite import SessionStoreBackend

    _CATALOG_BACKEND = SessionStoreBackend(Path("data"))
    return _CATALOG_BACKEND


def _is_default_storage(backend: Any) -> bool:
    """是不是正在用项目默认的 data 目录。测试用的临时库不要去扫正式缓存。"""

    try:
        return Path(backend.storage_root).resolve() == Path("data").resolve()
    except OSError:
        return False


def _index_path() -> Path:
    """旧索引文件的路径：跟会话库同一个根目录下的 paper_memory/index.json。"""

    return _catalog().storage_root / "paper_memory" / INDEX_FILE_NAME


def _import_index_file(backend: Any, path: Path) -> None:
    """把旧的 index.json 迁进目录表。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("长期记忆索引读失败，按空索引处理", extra={"path": str(path)})
        return
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        return
    imported = 0
    for item in raw_entries:
        entry = PaperMemoryEntry.from_dict(item)
        if entry is None:
            continue
        present = bool(entry.cache_dir) and (_cache_root() / entry.cache_dir).is_dir()
        backend.upsert_paper_catalog(
            paper_id=entry.memory_id,
            cache_dir=entry.cache_dir,
            title=entry.title,
            year=entry.year,
            has_chunks=entry.has_chunks and present,
            cache_present=present,
            recorded_at=entry.recorded_at,
            aliases=entry.aliases,
        )
        imported += 1
    logger.info("已把旧论文索引迁进数据库", extra={"entry_count": imported, "path": str(path)})


def _rebuild_from_disk(backend: Any) -> None:
    """扫描本机已有的缓存和会话，把「全文精读 + 有效分片」补进目录表。"""

    logger.info("本机论文目录为空，开始从已有精读补建")
    cache_root = _cache_root()
    if cache_root.is_dir():
        for directory in cache_root.iterdir():
            if not directory.is_dir():
                continue
            report = _load_report_file(directory / REPORT_FILE_NAME)
            if report is None or report.source != DEEP_READ_SOURCE_FULLTEXT:
                continue
            if not chunks_are_usable(directory):
                continue
            paper = _paper_from_cache_dir(directory)
            if paper is None:
                continue
            backend.upsert_paper_catalog(
                paper_id=paper_key(paper) or directory.name,
                cache_dir=directory.name,
                title=str(paper.get("title") or ""),
                year=paper_year(paper),
                has_chunks=True,
                cache_present=True,
                aliases=sorted(paper_aliases(paper) | {directory.name}),
            )

    sessions_root = Path(backend.sessions_dir)
    if sessions_root.is_dir():
        for session_dir in sessions_root.iterdir():
            papers_path = session_dir / "workspace" / WORKSPACE_FILE_NAME
            if not papers_path.is_file():
                continue
            try:
                payload = json.loads(papers_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            papers = payload.get("papers") if isinstance(payload, dict) else None
            if not isinstance(papers, dict):
                continue
            for entry_data in papers.values():
                if not isinstance(entry_data, dict):
                    continue
                paper = dict(entry_data.get("paper") or {})
                report = DeepReadReport.from_dict(entry_data.get("deep_read"))
                if report.source != DEEP_READ_SOURCE_FULLTEXT:
                    continue
                cache_dir = _locate_cache_dir_on_disk(paper, cache_root)
                if cache_dir is None or not chunks_are_usable(cache_dir):
                    continue
                report_path = cache_dir / REPORT_FILE_NAME
                if not report_path.is_file():
                    stored = DeepReadReport.from_dict(report.to_dict())
                    stored.artifact_id = ""
                    stored.fulltext_artifact_id = ""
                    _write_json(report_path, stored.to_dict())
                backend.upsert_paper_catalog(
                    paper_id=paper_key(paper) or cache_dir.name,
                    cache_dir=cache_dir.name,
                    title=str(paper.get("title") or ""),
                    year=paper_year(paper),
                    has_chunks=True,
                    cache_present=True,
                    aliases=sorted(paper_aliases(paper) | {cache_dir.name}),
                )
    logger.info("本机论文目录补建完成")


def _backfill_paper_sessions(backend: Any) -> None:
    """给现有会话工作区里的论文补上目录引用。"""

    sessions_root = Path(backend.sessions_dir)
    if not sessions_root.is_dir():
        return
    for session_dir in sessions_root.iterdir():
        if not session_dir.is_dir():
            continue
        papers_path = session_dir / "workspace" / WORKSPACE_FILE_NAME
        if not papers_path.is_file():
            continue
        try:
            workspace = SessionWorkspace.load(session_dir.name, sessions_root)
        except Exception:
            continue
        for paper_id, item in workspace.papers.items():
            cache_name = ""
            present = False
            has_chunks = False
            entry = lookup_memory(item.paper) or lookup_memory(paper_id)
            if entry is not None:
                cache_name = entry.cache_dir
                present = entry.cache_present
                has_chunks = entry.has_chunks
            ensure_bound(
                session_dir.name,
                paper_id,
                item.paper,
                cache_dir=cache_name,
                has_chunks=has_chunks,
                cache_present=present,
            )


def _invalidate_missing_cache(entry: PaperMemoryEntry) -> None:
    """目录表说有缓存、磁盘上却没了：标记失效，并同步所有会话。"""

    backend = _catalog()
    backend.mark_paper_cache(entry.memory_id, has_chunks=False, cache_present=False)
    _invalidate_paper_in_workspaces(entry)
    logger.info("发现论文缓存目录缺失，已把各会话精读状态改回可精读", extra={"paper_id": entry.memory_id})


def _invalidate_paper_in_workspaces(
    entry: PaperMemoryEntry,
    extra_ids: list[tuple[str, str]] | None = None,
) -> int:
    """把所有引用这篇本机论文的工作区条目改回可精读，并删掉会话里的报告产物。"""

    backend = _catalog()
    refs = {(item["session_id"], item["workspace_paper_id"]) for item in backend.list_paper_sessions(entry.memory_id)}
    for session_id, workspace_id in extra_ids or []:
        if session_id and workspace_id:
            refs.add((session_id, workspace_id))
    stub = {"title": entry.title, "year": entry.year or "", "paperId": entry.memory_id}
    sessions_root = Path(backend.sessions_dir)
    if sessions_root.is_dir():
        for session_dir in sessions_root.iterdir():
            papers_path = session_dir / "workspace" / WORKSPACE_FILE_NAME
            if not papers_path.is_file():
                continue
            try:
                workspace = SessionWorkspace.load(session_dir.name, sessions_root)
            except Exception:
                continue
            for paper_id, item in workspace.papers.items():
                if (session_dir.name, paper_id) in refs:
                    continue
                if papers_match(item.paper, stub) or paper_id in set(entry.aliases):
                    refs.add((session_dir.name, paper_id))
    updated = 0
    for session_id, workspace_id in refs:
        try:
            workspace = SessionWorkspace.load(session_id, sessions_root)
        except Exception:
            continue
        if workspace.invalidate_local_fulltext(workspace_id):
            updated += 1
        backend.delete_artifacts_for_paper(session_id, workspace_id)
        if entry.memory_id and entry.memory_id != workspace_id:
            backend.delete_artifacts_for_paper(session_id, entry.memory_id)
    return updated


def _lookup_names(paper: PaperLike) -> set[str]:
    """收集查找时用得上的编号。"""

    names = set(paper_aliases(paper))
    if isinstance(paper, dict):
        for key in ("paperId", "id", "doi"):
            value = str(paper.get(key) or "").strip()
            if value:
                names.add(value)
    else:
        for attr in ("paperId", "id", "doi"):
            value = str(getattr(paper, attr, "") or "").strip()
            if value:
                names.add(value)
    return {item for item in names if item}


def _names_from_id_string(value: str) -> set[str]:
    """把一个编号字符串展开成目录表里可能存过的各种写法。"""

    text = str(value or "").strip()
    if not text:
        return set()
    names = {text, safe_cache_name(text)}
    names.update(citation_lookup_keys(text))
    names.update(_lookup_names({"paperId": text, "id": text, "doi": text}))
    return {item for item in names if item}


def _pick_catalog_entry(
    hits: list[JsonObject],
    alias_set: set[str],
    *,
    year: int | None = None,
) -> PaperMemoryEntry | None:
    """从一批目录记录里挑最靠得住的一条：硬编号优先，标题键其次。"""

    strong: PaperMemoryEntry | None = None
    title_hit: PaperMemoryEntry | None = None
    for payload in hits:
        entry = PaperMemoryEntry.from_dict(payload)
        if entry is None:
            continue
        matched = {item for item in entry.aliases if item in alias_set}
        if any(not item.startswith("title:") for item in matched):
            strong = entry
            break
        if any(item.startswith("title:") for item in matched):
            title_hit = title_hit or entry
    if strong is not None:
        return strong
    if title_hit is None:
        return None
    if year is not None and title_hit.year is not None and year != title_hit.year:
        return None
    return title_hit


def _locate_cache_dir_on_disk(paper: PaperLike, cache_root: Path) -> Path | None:
    """在目录表还没有记下目录时，到磁盘上找缓存文件夹。

    中文说明：
    这是目录表缺目录名时的补救，只给本模块内部用。工具和 Agent 不要自己扫盘。
    一旦找到文件夹，马上把目录名写回目录表，下次就不用再扫。
    """

    if not cache_root.is_dir():
        return None
    from src.paper_retrieval.models import PaperDocument

    named = paper_cache_dir(cache_root, paper) if isinstance(paper, PaperDocument) else None
    if named is None:
        doc = _paper_document(paper) if not isinstance(paper, str) else None
        if doc is not None:
            named = paper_cache_dir(cache_root, doc)
        elif isinstance(paper, str):
            named = cache_root / safe_cache_name(paper)
    if named is not None and named.is_dir() and (named / "chunk.json").is_file():
        return _remember_located_cache(paper, named)

    aliases = _names_from_id_string(paper) if isinstance(paper, str) else set(paper_aliases(paper))
    for directory in cache_root.iterdir():
        if not directory.is_dir():
            continue
        if directory.name in aliases:
            return _remember_located_cache(paper, directory)
        cached_paper = _paper_from_cache_dir(directory)
        if cached_paper is None:
            continue
        if not isinstance(paper, str) and papers_match(cached_paper, paper):
            return _remember_located_cache(paper, directory)
        if isinstance(paper, str) and (aliases & paper_aliases(cached_paper)):
            return _remember_located_cache(paper, directory)
    if named is not None and named.is_dir():
        return _remember_located_cache(paper, named)
    return None


def _remember_located_cache(paper: PaperLike | str, cache_dir: Path) -> Path:
    """把扫盘找到的缓存目录名写回目录表。"""

    ensure_bound("", "", paper, cache_dir=cache_dir.name, cache_present=True)
    return cache_dir


def _paper_from_cache_dir(directory: Path) -> JsonObject | None:
    """从缓存目录的 metadata.json 读出论文元数据。"""

    path = directory / "metadata.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    paper = payload.get("paper")
    if isinstance(paper, dict) and paper:
        return dict(paper)
    paper_id = str(payload.get("paperId") or "").strip()
    if not paper_id:
        return None
    return {"paperId": paper_id, "id": paper_id, "title": paper_id}


def _paper_document(paper: PaperLike) -> Any:
    """把工作区字典尽量还原成 PaperDocument，还原不了就返回空。"""

    from dataclasses import fields as dataclass_fields

    from src.paper_retrieval.models import PaperDocument

    if isinstance(paper, PaperDocument):
        return paper
    if not isinstance(paper, dict):
        return None
    valid_names = {item.name for item in dataclass_fields(PaperDocument)}
    kwargs = {key: value for key, value in paper.items() if key in valid_names}
    if kwargs.get("year") == "":
        kwargs["year"] = None
    if not kwargs.get("id") and not kwargs.get("title"):
        return None
    if not kwargs.get("id"):
        kwargs["id"] = str(kwargs.get("paperId") or kwargs.get("title") or "paper")
    if not kwargs.get("title"):
        kwargs["title"] = str(kwargs.get("id") or "untitled")
    try:
        return PaperDocument(**kwargs)
    except TypeError:
        return None


def _load_report_file(path: Path) -> DeepReadReport | None:
    """读取缓存目录里的全局精读报告。文件没有或不是全文精读时返回空。"""

    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    report = DeepReadReport.from_dict(payload)
    if report.source != DEEP_READ_SOURCE_FULLTEXT:
        return None
    return report


def _copy_figure_artifacts(
    *,
    repo: "SessionRepository",
    session_key: str,
    paper_id: str,
    assets_dir: Path | None,
    markdown_text: str,
) -> str:
    """把缓存里的配图拷进当前会话，并把正文里的图片地址改成当前会话能打开的。"""

    if assets_dir is None or not assets_dir.is_dir():
        return markdown_text
    updated = markdown_text
    for figure_path in sorted(assets_dir.iterdir()):
        if not figure_path.is_file():
            continue
        try:
            record = repo.write_artifact(
                session_key,
                ARTIFACT_TYPE_FIGURE,
                figure_path.name,
                figure_path.read_bytes(),
                relative_path=f"artifacts/assets/{figure_path.name}",
                metadata={"paper_id": paper_id, "recalled": True},
            )
        except Exception as exc:
            logger.warning(
                "召回精读时配图写入失败，正文里会保留原来的相对地址",
                extra={"paper_id": paper_id, "figure": figure_path.name, "reason": str(exc)},
            )
            continue
        figure_url = f"/api/sessions/{quote(session_key, safe='')}/artifacts/{record.get('id') or ''}"
        updated = updated.replace(f"](assets/{figure_path.name})", f"]({figure_url})")
    return updated


def _cache_root() -> Path:
    """读配置里的论文缓存根目录。"""

    return Path(SystemConfig.load().read.paper_cache_dir)


def _read_text(path: Path) -> str:
    """读一个文本文件，读不到就返回空字符串。"""

    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_json(path: Path, payload: JsonObject) -> None:
    """先写临时文件再替换，避免写到一半留下半截 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_path, path)
    except OSError:
        handle.close()
        tmp_path.unlink(missing_ok=True)
        raise
