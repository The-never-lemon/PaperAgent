"""本机论文长期记忆：记住已经切过分片、并且全文精读过的论文。

中文说明：
分片本来就放在 data/paper_cache 里，各会话都能看到。精读报告以前只写在
当前会话的 papers.json 里，新开一个对话再检索到同一篇，报告就丢了。
这里做两件事：
1. 全文精读成功后，把报告另存一份到该论文的缓存目录（deep_read.json），
   并在 data/paper_memory/index.json 里记下这篇论文的所有别名；
2. 之后任意会话检索到同一篇（按 DOI / arXiv / 标题认），把报告和全文
   拷进当前会话，主 Agent 不必再精读一遍。

只有「全文精读 + 分片还有效」才进记忆。只有摘要的降级报告不记。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from src.llm.config import SystemConfig
from src.models.deep_read import DEEP_READ_SOURCE_FULLTEXT, DeepReadReport
from src.models.sessions import utc_now
from src.models.workspace import DEFAULT_SESSIONS_ROOT, WORKSPACE_FILE_NAME, sanitize_for_filename
from src.paper_retrieval.identity import PaperLike, paper_aliases, paper_key, paper_year, papers_match
from src.utils import get_logger
from src.utils.read_utils.cache import paper_cache_dir, safe_cache_name
from src.utils.read_utils.chunkers import CHUNKER_VERSION, PageChunker, load_chunks_file


if TYPE_CHECKING:
    from src.models.workspace import SessionWorkspace
    from src.repositories.sessions.base import SessionRepository


JsonObject = dict[str, Any]
logger = get_logger(__name__)

# 记忆索引文件的数据格式版本。以后字段只增不改，旧文件照样能读。
MEMORY_SCHEMA_VERSION = 1

# 索引默认放在这个目录。报告正文不放这里，仍跟论文缓存走。
DEFAULT_MEMORY_DIR = Path("data") / "paper_memory"
INDEX_FILE_NAME = "index.json"

# 全局报告文件名，和 chunk.json、paper.md 放在同一篇论文的缓存目录里。
REPORT_FILE_NAME = "deep_read.json"

# 产物类型和精读模块保持一致，召回时写入当前会话才打得开。
ARTIFACT_TYPE_REPORT = "deep_read_report"
ARTIFACT_TYPE_FULLTEXT = "paper_fulltext"
ARTIFACT_TYPE_FIGURE = "paper_figure"

# 同一进程里读写索引时加锁，避免两次精读同时把索引写坏。
_INDEX_LOCK = threading.Lock()

# 下面三个是进程内缓存：索引内容、别名到条目的对照表、是否已经从磁盘加载过。
_ENTRIES: dict[str, "PaperMemoryEntry"] = {}
_ALIAS_MAP: dict[str, "PaperMemoryEntry"] = {}
_LOADED = False


@dataclass(slots=True)
class PaperMemoryEntry:
    """索引里一篇已经记住的论文。

    Attributes:
        memory_id: 这篇论文的主编号（和检索去重用的 paper_key 相同）。
        aliases: 所有能对上号的名字，查找时从这里走。
        cache_dir: 缓存在 data/paper_cache 下的目录名，不是完整路径。
        title: 论文标题，方便日志查看。
        year: 发表年份，标题兜底匹配时用来降低误认。
        has_chunks: 登记时分片是完整可用的。
        recorded_at: 记入记忆的时间。
    """

    memory_id: str
    aliases: list[str] = field(default_factory=list)
    cache_dir: str = ""
    title: str = ""
    year: int | None = None
    has_chunks: bool = False
    recorded_at: str = field(default_factory=utc_now)

    def to_dict(self) -> JsonObject:
        """把一条记忆转成普通字典，写进索引文件。"""

        return {
            "memory_id": self.memory_id,
            "aliases": list(self.aliases),
            "cache_dir": self.cache_dir,
            "title": self.title,
            "year": self.year,
            "has_chunks": self.has_chunks,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PaperMemoryEntry | None":
        """从索引文件里的字典还原一条记忆。字段缺失或损坏时返回空。"""

        if not isinstance(data, dict):
            return None
        memory_id = str(data.get("memory_id") or "").strip()
        cache_dir = str(data.get("cache_dir") or "").strip()
        if not memory_id or not cache_dir:
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
        return cls(
            memory_id=memory_id,
            aliases=aliases or [memory_id],
            cache_dir=cache_dir,
            title=str(data.get("title") or ""),
            year=year if year and year > 0 else None,
            has_chunks=bool(data.get("has_chunks")),
            recorded_at=str(data.get("recorded_at") or "") or utc_now(),
        )


@dataclass(slots=True)
class MemoryImportResult:
    """把一篇记忆导入当前会话之后的结果。"""

    recalled: bool
    has_chunks: bool
    report: DeepReadReport | None = None


def lookup_memory(paper: PaperLike | str) -> PaperMemoryEntry | None:
    """按论文元数据或一个编号字符串查找本机记忆。找不到返回空。"""

    with _INDEX_LOCK:
        _ensure_loaded_locked()
        return _lookup_unlocked(paper)


def resolve_paper_cache_dir(base_dir: str | Path, paper: PaperLike | str) -> Path:
    """找出这篇论文真正的缓存目录。

    中文说明：
    以前目录名跟着当时的 paperId 走，后来换个数据源编号变了，就会去一个空目录。
    这里先查记忆索引里记下的目录名；没有记录再退回按当前编号起名。
    """

    root = Path(base_dir)
    entry = lookup_memory(paper)
    if entry and entry.cache_dir:
        candidate = root / entry.cache_dir
        if candidate.is_dir():
            return candidate
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
    """全文精读成功后，把报告写入论文缓存并登记到长期记忆。

    分片不完整、或报告其实是摘要降级时，什么都不写，返回 False。
    """

    if report.source != DEEP_READ_SOURCE_FULLTEXT:
        return False
    cache_root = _cache_root()
    cache_dir = _locate_cache_dir_on_disk(paper, cache_root)
    if cache_dir is None or not chunks_are_usable(cache_dir):
        logger.info("精读完成但分片不可用，不写入长期记忆", extra={"memory_id": paper_key(paper)})
        return False
    stored = DeepReadReport.from_dict(report.to_dict())
    # 中文说明：会话产物编号只在当时那个对话里有效，写进全局报告会把
    # 以后的会话带到已经不存在的文件上。这里清掉，召回时再给当前会话新写一份。
    stored.artifact_id = ""
    stored.fulltext_artifact_id = ""
    _write_json(cache_dir / REPORT_FILE_NAME, stored.to_dict())
    entry = _build_entry(paper, cache_dir, has_chunks=True)
    with _INDEX_LOCK:
        _ensure_loaded_locked()
        _upsert_entry_locked(entry, paper)
        _save_index_locked()
    logger.info(
        "已把全文精读写入长期记忆",
        extra={"memory_id": entry.memory_id, "cache_dir": entry.cache_dir},
    )
    return True


def import_memory_into_session(
    *,
    paper_id: str,
    paper: JsonObject,
    workspace: "SessionWorkspace",
    repo: "SessionRepository",
    session_key: str,
) -> MemoryImportResult:
    """若本机已有这篇论文的全文精读，就拷进当前会话工作区。

    当前会话已经有一份全文精读时不覆盖，只告诉调用方「已经有报告」。
    只有摘要的旧报告会被全文记忆替换掉。
    """

    existing = workspace.get_paper(paper_id)
    if existing is not None and existing.deep_read is not None:
        if existing.deep_read.source == DEEP_READ_SOURCE_FULLTEXT:
            has_chunks = lookup_memory(paper) is not None or existing.fulltext_cached
            return MemoryImportResult(recalled=False, has_chunks=has_chunks, report=existing.deep_read)

    entry = lookup_memory(paper)
    if entry is None or not entry.has_chunks:
        return MemoryImportResult(recalled=False, has_chunks=False)
    cache_dir = _cache_root() / entry.cache_dir
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


def _ensure_loaded_locked() -> None:
    """加载索引；文件还不存在时，从旧会话和缓存里补建一份。"""

    global _LOADED
    if _LOADED:
        return
    index_path = _index_path()
    if index_path.is_file():
        _load_index_locked(index_path)
    else:
        _rebuild_locked()
    _LOADED = True


def _lookup_unlocked(paper: PaperLike | str) -> PaperMemoryEntry | None:
    """在已经加载的索引里查找，调用方必须已经拿着锁。"""

    if isinstance(paper, str):
        cleaned = paper.strip()
        if not cleaned:
            return None
        return _ALIAS_MAP.get(cleaned)

    strong_hit: PaperMemoryEntry | None = None
    title_hit: PaperMemoryEntry | None = None
    for alias in paper_aliases(paper):
        entry = _ALIAS_MAP.get(alias)
        if entry is None:
            continue
        if alias.startswith("title:"):
            if title_hit is None:
                title_hit = entry
            continue
        strong_hit = entry
        break
    if strong_hit is not None:
        return strong_hit
    if title_hit is None:
        return None
    year = paper_year(paper)
    if year is not None and title_hit.year is not None and year != title_hit.year:
        return None
    return title_hit


def _rebuild_locked() -> None:
    """扫描本机已有的缓存和会话，把「全文精读 + 有效分片」补进索引。"""

    logger.info("长期记忆索引为空，开始从本机已有精读补建")
    cache_root = _cache_root()
    # 第一步：缓存目录里已经同时有分片和全局报告的，直接登记。
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
            _upsert_entry_locked(_build_entry(paper, directory, has_chunks=True), paper)

    # 第二步：各个旧会话的 papers.json 里还有全文报告，缓存里有分片的，
    # 把报告拷进缓存目录再登记。这样升级前精读过的论文，新会话也能召回。
    sessions_root = DEFAULT_SESSIONS_ROOT
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
                _upsert_entry_locked(_build_entry(paper, cache_dir, has_chunks=True), paper)

    _save_index_locked()
    logger.info("长期记忆补建完成", extra={"entry_count": len(_ENTRIES)})


def _load_index_locked(path: Path) -> None:
    """从磁盘读索引文件，填进进程内对照表。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("长期记忆索引读失败，按空索引处理", extra={"path": str(path)})
        return
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        return
    for item in raw_entries:
        entry = PaperMemoryEntry.from_dict(item)
        if entry is None:
            continue
        _ENTRIES[entry.memory_id] = entry
        for alias in entry.aliases:
            _ALIAS_MAP[alias] = entry


def _save_index_locked() -> None:
    """把进程内的索引原子写入磁盘。"""

    directory = _memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "updated_at": utc_now(),
        "entries": [entry.to_dict() for entry in _ENTRIES.values()],
    }
    _write_json(directory / INDEX_FILE_NAME, payload)


def _upsert_entry_locked(entry: PaperMemoryEntry, paper: PaperLike) -> None:
    """把一条记忆写进进程内对照表；同一篇已有记录时合并别名。"""

    existing = _ENTRIES.get(entry.memory_id)
    if existing is None:
        # 主编号不同、但别名能对上时，合并到已有那条，避免同篇论文出现两条记忆。
        for alias in entry.aliases:
            hit = _ALIAS_MAP.get(alias)
            if hit is not None and papers_match(_alias_paper_stub(hit), paper):
                existing = hit
                break
    if existing is not None:
        merged_aliases = sorted(set(existing.aliases) | set(entry.aliases))
        existing.aliases = merged_aliases
        if entry.cache_dir:
            existing.cache_dir = entry.cache_dir
        if entry.title:
            existing.title = entry.title
        if entry.year is not None:
            existing.year = entry.year
        existing.has_chunks = existing.has_chunks or entry.has_chunks
        existing.recorded_at = entry.recorded_at or existing.recorded_at
        entry = existing
    _ENTRIES[entry.memory_id] = entry
    for alias in entry.aliases:
        _ALIAS_MAP[alias] = entry


def _build_entry(paper: PaperLike, cache_dir: Path, *, has_chunks: bool) -> PaperMemoryEntry:
    """根据论文元数据和缓存目录拼一条索引记录。"""

    title = ""
    if isinstance(paper, dict):
        title = str(paper.get("title") or "")
    else:
        title = str(getattr(paper, "title", "") or "")
    return PaperMemoryEntry(
        memory_id=paper_key(paper) or cache_dir.name,
        aliases=sorted(paper_aliases(paper) | {cache_dir.name}),
        cache_dir=cache_dir.name,
        title=title,
        year=paper_year(paper),
        has_chunks=has_chunks,
        recorded_at=utc_now(),
    )


def _alias_paper_stub(entry: PaperMemoryEntry) -> JsonObject:
    """用索引条目拼一份最小论文字典，只够拿去判断是不是同一篇。"""

    return {"title": entry.title, "year": entry.year or "", "paperId": entry.memory_id}


def _locate_cache_dir_on_disk(paper: PaperLike, cache_root: Path) -> Path | None:
    """在还没有索引、或索引里没有这篇时，到磁盘上找缓存目录。"""

    if not cache_root.is_dir():
        return None
    from src.paper_retrieval.models import PaperDocument

    named = paper_cache_dir(cache_root, paper) if isinstance(paper, PaperDocument) else None
    if named is None:
        doc = _paper_document(paper) if not isinstance(paper, str) else None
        if doc is not None:
            named = paper_cache_dir(cache_root, doc)
    if named is not None and named.is_dir() and (named / "chunk.json").is_file():
        return named

    aliases = paper_aliases(paper) if not isinstance(paper, str) else {paper}
    for directory in cache_root.iterdir():
        if not directory.is_dir():
            continue
        if directory.name in aliases:
            return directory
        cached_paper = _paper_from_cache_dir(directory)
        if cached_paper is None:
            continue
        if not isinstance(paper, str) and papers_match(cached_paper, paper):
            return directory
        if isinstance(paper, str) and paper in paper_aliases(cached_paper):
            return directory
    return named if named is not None and named.is_dir() else None


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


def _memory_dir() -> Path:
    """长期记忆索引所在的目录。"""

    return DEFAULT_MEMORY_DIR


def _index_path() -> Path:
    """索引文件的完整路径。"""

    return _memory_dir() / INDEX_FILE_NAME


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
