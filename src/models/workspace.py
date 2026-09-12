from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.models.deep_read import DeepReadReport
from src.models.read_models import MATCH_LEVEL_FIELDS, calculate_relevance_score, normalize_match_levels
from src.models.sessions import utc_now
from src.utils import get_logger


JsonObject = dict[str, Any]
logger = get_logger(__name__)

# 工作区数据格式版本号。以后 papers.json 结构有新增字段时把它加一，
# 读取端对旧版本数据保持宽容（缺失字段给默认值、未知字段直接忽略）。
# 版本 3：新增 removed_papers 归档字典（被移除的论文不再真删，而是挪进归档留后路）。
WORKSPACE_SCHEMA_VERSION = 4

# 工作区文件的固定名字。每个会话一份，存放这个会话调研过程中的全部论文状态。
WORKSPACE_FILE_NAME = "papers.json"

# 会话数据的默认根目录。和 SQLiteSessionRepository 的默认存储位置保持一致，
# 即 data/sessions/{session_key}/...；调用方也可以显式传入别的根目录。
DEFAULT_SESSIONS_ROOT = Path("data") / "sessions"

# 论文在工作区里的三种状态，供 list_papers 工具按状态筛选：
# - "new"：刚检索进来，还没有评价过；
# - "evaluated"：已经跑过三维评分；
# - "deep_read"：已经生成过精读报告。
PAPER_STATUS_NEW = "new"
PAPER_STATUS_EVALUATED = "evaluated"
PAPER_STATUS_DEEP_READ = "deep_read"

# 论文标题在列表筛选时的摘要截断长度（list_papers 返回精简字段用）。
TITLE_PREVIEW_MAX_CHARS = 200

# Windows 文件名不允许出现的字符集合。产物文件名里带 paper_id 时必须先把它们去掉，
# 否则在 Windows 上写文件会直接报错（工程规范 8.4）。
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_for_filename(raw_id: str) -> str:
    """把论文编号整理成可以安全放进文件名的形式。

    做法很简单：把 Windows 不允许的字符（\\ / : * ? " < > |）统一替换成下划线，
    再去掉首尾空白和点号。如果整理完变成空字符串，就用 "unknown" 兜底。
    """

    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", str(raw_id or "")).strip().strip(".")
    return cleaned or "unknown"


@dataclass(slots=True)
class PaperEvaluation:
    """一篇论文的三维评分结果。

    评分规则复用 read_models 里的固定表：模型只判断三个维度是
    "匹配 / 部分匹配 / 不匹配"，总分由程序按 50/30/20 的权重算出来，
    不允许模型直接给总分。

    Attributes:
        match_levels: 三个维度的匹配程度，键固定为 MATCH_LEVEL_FIELDS 里的三个字段名。
        score: 程序算出来的总分（0-100）。
        reason: 模型给出的一句话评分理由。
        research_topic: 这次评分针对的研究主题（换了主题重新评分时会更新）。
        evaluated_at: 评分时间（UTC ISO 字符串）。
    """

    match_levels: dict[str, str] = field(
        default_factory=lambda: {name: "not_match" for name in MATCH_LEVEL_FIELDS}
    )
    score: int = 0
    reason: str = ""
    research_topic: str = ""
    evaluated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> JsonObject:
        """把评分结果转换成普通字典，方便写进 papers.json。"""

        return {
            "match_levels": dict(self.match_levels),
            "score": self.score,
            "reason": self.reason,
            "research_topic": self.research_topic,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PaperEvaluation":
        """从普通字典还原评分结果，非法或缺失的值都会被修正。"""

        payload = data if isinstance(data, dict) else {}
        match_levels = normalize_match_levels(payload.get("match_levels"))
        try:
            score = int(payload.get("score"))
        except (TypeError, ValueError):
            score = 0
        return cls(
            match_levels=match_levels,
            # 分数以固定表重新计算为准，避免文件里被写入不一致的数字。
            score=calculate_relevance_score(match_levels) if score <= 0 else max(0, min(100, score)),
            reason=str(payload.get("reason") or ""),
            research_topic=str(payload.get("research_topic") or ""),
            evaluated_at=str(payload.get("evaluated_at") or "") or utc_now(),
        )


@dataclass(slots=True)
class WorkspacePaperEntry:
    """工作区里一篇论文的全部状态。

    Attributes:
        paper: 论文元数据字典（PaperDocument.to_dict() 的结果，原样保存）。
        evaluation: 三维评分结果；还没评价过就是 None。
        deep_read: 精读报告；还没精读过就是 None。
        fulltext_cached: 全文是否已经下载到本地缓存（下载过就不再重复下载）。
        added_at: 论文进入工作区的时间（UTC ISO 字符串）。
    """

    paper: JsonObject = field(default_factory=dict)
    evaluation: PaperEvaluation | None = None
    deep_read: DeepReadReport | None = None
    fulltext_cached: bool = False
    # 包 4 新增的用户标注字段（schema_version 2）。旧 papers.json 缺少这些字段时
    # from_dict 给默认值，不会因升级崩溃（工程规范 8.2：字段只增不改不删）。
    starred: bool = False
    tags: list[str] = field(default_factory=list)
    note: str = ""
    added_at: str = field(default_factory=utc_now)

    def status(self) -> str:
        """返回这篇论文当前处于哪个阶段（new / evaluated / deep_read）。"""

        if self.deep_read is not None:
            return PAPER_STATUS_DEEP_READ
        if self.evaluation is not None:
            return PAPER_STATUS_EVALUATED
        return PAPER_STATUS_NEW

    def to_dict(self) -> JsonObject:
        """把单篇论文状态转换成普通字典。"""

        return {
            "paper": dict(self.paper),
            "evaluation": self.evaluation.to_dict() if self.evaluation is not None else None,
            "deep_read": self.deep_read.to_dict() if self.deep_read is not None else None,
            "fulltext_cached": self.fulltext_cached,
            "added_at": self.added_at,
            "starred": self.starred,
            "tags": list(self.tags),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "WorkspacePaperEntry":
        """从普通字典还原单篇论文状态，缺失字段一律用默认值。"""

        payload = data if isinstance(data, dict) else {}
        evaluation_data = payload.get("evaluation")
        deep_read_data = payload.get("deep_read")
        return cls(
            paper=dict(payload.get("paper") or {}),
            evaluation=PaperEvaluation.from_dict(evaluation_data) if isinstance(evaluation_data, dict) else None,
            deep_read=DeepReadReport.from_dict(deep_read_data) if isinstance(deep_read_data, dict) else None,
            fulltext_cached=bool(payload.get("fulltext_cached")),
            added_at=str(payload.get("added_at") or "") or utc_now(),
        )


@dataclass(slots=True)
class SearchHistoryEntry:
    """检索历史记录（schema_version 4 新增）。

    中文说明：
    记录主 Agent 每次 search_papers 工具调用的关键信息（topic、概念组、命中数、新增数等），
    用于注入主 Agent 系统提示词的"当前工作区状态"段，防止主 Agent 跨轮次重复检索白烧轮次。
    只保留最近 20 条（超出时丢弃最老的）。

    Attributes:
        topic: 检索时的主题（自然语言）。
        concept_groups_summary: 概念组的简短摘要（每组取第一个同义词作代表），
            便于在系统提示词里展示而不会撑爆 token。
        hits: 命中论文数（去重前）。
        added: 新增到工作区的论文数（去重后）。
        relaxed: 是否经过了自动放宽（D4 策略）。
        timestamp: 检索发生时间（UTC ISO 格式）。
    """

    topic: str = ""
    concept_groups_summary: str = ""
    hits: int = 0
    added: int = 0
    relaxed: bool = False
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> JsonObject:
        return {
            "topic": self.topic,
            "concept_groups_summary": self.concept_groups_summary,
            "hits": self.hits,
            "added": self.added,
            "relaxed": self.relaxed,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SearchHistoryEntry":
        if not isinstance(data, dict):
            return cls()
        return cls(
            topic=str(data.get("topic") or ""),
            concept_groups_summary=str(data.get("concept_groups_summary") or ""),
            hits=int(data.get("hits") or 0),
            added=int(data.get("added") or 0),
            relaxed=bool(data.get("relaxed")),
            timestamp=str(data.get("timestamp") or "") or utc_now(),
        )


@dataclass(slots=True)
class SessionWorkspace:
    """一个会话的调研工作区：保存研究主题和所有检索进来的论文状态。

    对应磁盘上的 data/sessions/{session_key}/workspace/papers.json。
    使用方式（实施方案第四节）：
    1. 一次 run 开始时用 load() 把文件读进内存；
    2. 工具每做一次改动（新增论文、写评分、写精读报告……），改动方法内部
       会立刻调用 save() 落盘，保证任何时刻中断都不丢已完成的进度；
    3. save() 采用"先写临时文件、再原子替换"的方式，就算写一半进程被杀掉，
       原来的 papers.json 也不会损坏。

    Attributes:
        session_key: 所属会话的编号。
        sessions_root: 会话数据根目录，默认 data/sessions。
        schema_version: 数据格式版本号。
        research_topic: 用户当前的研究主题（评价论文时作为打分依据）。
        updated_at: 最近一次落盘时间。
        papers: 论文编号 -> 论文状态 的字典。
        removed_papers: 被移除论文的归档（论文编号 -> 移除时的完整快照字典，
            额外带一个 removed_at 记录移除时间）。归档会随 papers.json 一起落盘，
            用户后悔时可以从工作区文件里把数据找回来。
    """

    session_key: str
    sessions_root: Path = DEFAULT_SESSIONS_ROOT
    schema_version: int = WORKSPACE_SCHEMA_VERSION
    research_topic: str = ""
    updated_at: str = field(default_factory=utc_now)
    papers: dict[str, WorkspacePaperEntry] = field(default_factory=dict)
    # 被移除论文的归档（schema_version 3 新增字段）。旧 papers.json 没有这个键，
    # 加载时保持默认空字典即可（字段只增不改不删，读取端对旧数据宽容）。
    removed_papers: dict[str, JsonObject] = field(default_factory=dict)
    # 检索历史（schema_version 4 新增字段）。用于注入主 Agent 系统提示词，
    # 防止主 Agent 跨轮次重复检索白烧轮次。只保留最近 20 条。
    search_history: list[SearchHistoryEntry] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 文件读写
    # ------------------------------------------------------------------

    @property
    def workspace_dir(self) -> Path:
        """返回这个会话工作区所在的目录。"""

        return self.sessions_root / self.session_key / "workspace"

    @property
    def workspace_path(self) -> Path:
        """返回 papers.json 的完整路径。"""

        return self.workspace_dir / WORKSPACE_FILE_NAME

    @classmethod
    def load(cls, session_key: str, sessions_root: Path | str | None = None) -> "SessionWorkspace":
        """从磁盘加载一个会话的工作区。

        文件不存在时返回一个空工作区（新会话第一次使用就是这种情况）；
        文件存在但内容损坏时记录日志并返回空工作区，绝不让整个 run 因为
        一个坏文件而起不来。
        """

        root = Path(sessions_root) if sessions_root is not None else DEFAULT_SESSIONS_ROOT
        workspace = cls(session_key=session_key, sessions_root=root)
        path = workspace.workspace_path
        if not path.is_file():
            return workspace
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 文件读不出来或不是合法 JSON：保守起见当作空工作区重新开始，
            # 坏文件保留在磁盘上不动，方便用户或开发者事后排查。
            logger.warning("工作区文件读取失败，按空工作区处理", extra={"session_key": session_key, "path": str(path)})
            return workspace
        workspace._apply_dict(payload)
        logger.debug(
            "工作区加载完成",
            extra={"session_key": session_key, "paper_count": len(workspace.papers)},
        )
        return workspace

    def save(self) -> None:
        """把工作区当前状态落盘（先写临时文件，再原子替换）。

        原子替换的意思是：新内容先完整写进同一个目录下的临时文件，
        写完后用 os.replace 一步换成正式的 papers.json。
        这样即使写到一半断电或进程被杀，磁盘上要么是完整的旧文件，
        要么是完整的新文件，绝不会出现半截 JSON。
        """

        self.updated_at = utc_now()
        directory = self.workspace_dir
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=1)
        # 临时文件必须和正式文件在同一个目录（同一块磁盘），os.replace 才是原子操作。
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=WORKSPACE_FILE_NAME + ".",
            suffix=".tmp",
            delete=False,
        )
        tmp_path = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(tmp_path, self.workspace_path)
        except OSError:
            # 写盘失败时先清理掉可能残留的临时文件，再把异常抛给调用方处理。
            handle.close()
            tmp_path.unlink(missing_ok=True)
            raise

    def to_dict(self) -> JsonObject:
        """把整个工作区转换成普通字典（papers.json 的文件内容）。"""

        return {
            "schema_version": self.schema_version,
            "research_topic": self.research_topic,
            "updated_at": self.updated_at,
            "papers": {paper_id: entry.to_dict() for paper_id, entry in self.papers.items()},
            # 被移除论文的归档也一起落盘，删错的论文才能从文件里找回来。
            "removed_papers": {paper_id: dict(data) for paper_id, data in self.removed_papers.items()},
            # 检索历史（schema_version 4 新增），只保留最近 20 条。
            "search_history": [entry.to_dict() for entry in self.search_history[-20:]],
        }

    def _apply_dict(self, payload: Any) -> None:
        """把 papers.json 读出来的字典套用到当前工作区对象上。

        保持宽容：未知字段忽略、缺失字段给默认值、单篇论文数据损坏时
        跳过那一篇并记录日志，不影响其余论文。
        """

        if not isinstance(payload, dict):
            return
        try:
            self.schema_version = int(payload.get("schema_version") or WORKSPACE_SCHEMA_VERSION)
        except (TypeError, ValueError):
            self.schema_version = WORKSPACE_SCHEMA_VERSION
        self.research_topic = str(payload.get("research_topic") or "")
        self.updated_at = str(payload.get("updated_at") or "") or utc_now()
        # 归档字典（schema_version 3 新增）：旧文件没有这个键时保持空归档，
        # 值不是字典的坏数据直接跳过，不让归档问题影响正常论文的加载。
        removed = payload.get("removed_papers")
        if isinstance(removed, dict):
            self.removed_papers = {
                str(paper_id): dict(entry_data)
                for paper_id, entry_data in removed.items()
                if isinstance(entry_data, dict)
            }
        papers = payload.get("papers")
        if not isinstance(papers, dict):
            return
        for paper_id, entry_data in papers.items():
            try:
                self.papers[str(paper_id)] = WorkspacePaperEntry.from_dict(entry_data)
            except Exception:
                # 单篇数据坏掉不应该拖垮整个工作区，跳过并留日志即可。
                logger.warning(
                    "工作区里有一篇论文数据无法解析，已跳过",
                    extra={"session_key": self.session_key, "paper_id": str(paper_id)},
                )
        # 中文说明：检索历史（schema_version 4 新增）。旧文件没有这个键时保持空列表。
        history_raw = payload.get("search_history")
        if isinstance(history_raw, list):
            self.search_history = [
                SearchHistoryEntry.from_dict(entry_data)
                for entry_data in history_raw
                if isinstance(entry_data, dict)
            ][-20:]  # 中文说明：只保留最近 20 条，避免无限膨胀。

    # ------------------------------------------------------------------
    # 论文增删改查（每次改动都会立刻落盘）
    # ------------------------------------------------------------------

    def record_search_history(self, entry: SearchHistoryEntry) -> None:
        """记录一次检索历史，并立刻落盘。

        中文说明：
        每次 search_papers 工具调用完成后，handler 调这里记录本次检索的摘要信息
        （主题、概念组、命中数、新增数、是否放宽、时间戳）。
        列表只保留最近 20 条（超出时丢弃最老的），避免无限膨胀。
        """

        self.search_history.append(entry)
        self.search_history = self.search_history[-20:]
        self.updated_at = utc_now()
        self.save()

    def upsert_paper(self, paper: JsonObject) -> tuple[str, bool]:
        """把一篇检索到的论文放进工作区，返回 (论文编号, 是否是新增)。"""

        return self.upsert_papers([paper])[0]

    def upsert_papers(self, papers: list[JsonObject]) -> list[tuple[str, bool]]:
        """把一批检索到的论文放进工作区，最后只落盘一次。

        去重规则和检索服务保持一致：优先用 paperId，其次 id、doi，
        最后用标题兜底。已经存在的论文不会重复添加，只会用新拿到的
        元数据补齐原来缺失的字段（已有的评分和精读报告不动）。

        Returns:
            与输入顺序一致的列表，每项是 (论文编号, 是否是新增论文)；
            连标题都没有的无效数据会得到 ("", False)。
        """

        outcomes: list[tuple[str, bool]] = []
        changed = False
        for paper in papers:
            paper_id = paper_identity(paper)
            if not paper_id:
                # 连标题都没有的数据没法建档，直接丢弃，避免产生空壳条目。
                outcomes.append(("", False))
                continue
            existing = self.papers.get(paper_id)
            if existing is not None:
                # 已存在：只把原来为空的元数据字段补上，评分/精读等状态保持原样。
                merged = dict(existing.paper)
                for key, value in paper.items():
                    if not merged.get(key) and value:
                        merged[key] = value
                if merged != existing.paper:
                    existing.paper = merged
                    changed = True
                outcomes.append((paper_id, False))
                continue
            self.papers[paper_id] = WorkspacePaperEntry(paper=dict(paper))
            changed = True
            outcomes.append((paper_id, True))
        # 整批改动完成后统一落盘一次，避免每篇论文都重写一遍整个文件。
        if changed:
            self.save()
        return outcomes

    def get_paper(self, paper_id: str) -> WorkspacePaperEntry | None:
        """按论文编号读取工作区里的论文状态，不存在时返回 None。"""

        return self.papers.get(str(paper_id or "").strip())

    def remove_papers(self, paper_ids: list[str]) -> int:
        """从工作区移除若干篇论文（移入归档，不是真删），返回实际移除的数量。

        中文注释：
        删论文是破坏性操作——主 Agent 或用户都可能删错，而论文条目上往往挂着
        已经花钱跑出来的评分和精读报告，真删加立即落盘之后就再也找不回来了。
        所以这里改成"归档式软删除"：被移除的论文整体挪进 removed_papers 归档
        （随 papers.json 一起持久化），活跃论文列表里立刻消失（list_papers 等
        查询行为不变），但原始数据还完整保留在工作区文件里，需要时可以恢复。
        """

        removed = 0
        for paper_id in paper_ids:
            cleaned = str(paper_id or "").strip()
            entry = self.papers.pop(cleaned, None)
            if entry is None:
                continue
            # 归档内容就是这篇论文的完整快照，再补记一个移除时间方便事后追查。
            archived = entry.to_dict()
            archived["removed_at"] = utc_now()
            self.removed_papers[cleaned] = archived
            removed += 1
        if removed:
            self.save()
        return removed

    def set_research_topic(self, topic: str) -> None:
        """更新当前研究主题（用户改变调研方向时由主 Agent 调用）。"""

        cleaned = str(topic or "").strip()
        if cleaned and cleaned != self.research_topic:
            self.research_topic = cleaned
            self.save()

    def set_evaluation(self, paper_id: str, evaluation: PaperEvaluation) -> bool:
        """给一篇论文写入评分结果，成功返回 True。"""

        entry = self.get_paper(paper_id)
        if entry is None:
            return False
        entry.evaluation = evaluation
        self.save()
        return True

    def set_evaluations(self, evaluations: dict[str, PaperEvaluation]) -> int:
        """批量写入多篇论文的评分结果，最后只落盘一次，返回实际写入的数量。

        评价工具会并发评完一批论文后统一调用这里，避免每篇都重写一遍整个文件。
        工作区里不存在的论文编号会被跳过（可能刚被用户删掉）。
        """

        written = 0
        for paper_id, evaluation in evaluations.items():
            entry = self.get_paper(paper_id)
            if entry is None:
                continue
            entry.evaluation = evaluation
            written += 1
        if written:
            self.save()
        return written

    def set_deep_read(self, paper_id: str, report: DeepReadReport) -> bool:
        """给一篇论文写入精读报告，成功返回 True。"""

        entry = self.get_paper(paper_id)
        if entry is None:
            return False
        entry.deep_read = report
        self.save()
        return True

    def set_fulltext_cached(self, paper_id: str, cached: bool) -> bool:
        """标记一篇论文的全文是否已经下载到本地缓存。"""

        entry = self.get_paper(paper_id)
        if entry is None:
            return False
        entry.fulltext_cached = bool(cached)
        self.save()
        return True

    def update_paper_annotations(
        self, paper_id: str, *, starred: bool | None = None, tags: list[str] | None = None, note: str | None = None
    ) -> bool:
        """更新论文的用户标注（加星、标签、笔记）。

        中文注释：
        这些标注只影响用户视图，不影响 Agent 的工作流。改完立即落盘。
        返回 True 表示成功，False 表示论文不存在。
        """

        entry = self.papers.get(paper_id)
        if entry is None:
            return False
        if starred is not None:
            entry.starred = starred
        if tags is not None:
            entry.tags = list(tags)
        if note is not None:
            entry.note = note
        self.updated_at = utc_now()
        self.save()
        return True


    def query_papers(
        self,
        *,
        keyword: str = "",
        year: int | None = None,
        min_score: int | None = None,
        source: str = "",
        status: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[tuple[str, WorkspacePaperEntry]]]:
        """按条件筛选工作区里的论文，返回 (符合条件的总数, 当前页的论文列表)。

        各条件之间是"并且"的关系；传空值表示不按这个条件过滤。
        keyword 会同时在标题和摘要里做不区分大小写的模糊匹配。
        """

        keyword_cleaned = str(keyword or "").strip().lower()
        source_cleaned = str(source or "").strip().lower()
        status_cleaned = str(status or "").strip().lower()
        matched: list[tuple[str, WorkspacePaperEntry]] = []
        for paper_id, entry in self.papers.items():
            paper = entry.paper
            if keyword_cleaned:
                title = str(paper.get("title") or "").lower()
                abstract = str(paper.get("abstract") or "").lower()
                if keyword_cleaned not in title and keyword_cleaned not in abstract:
                    continue
            if year is not None and _optional_int(paper.get("year")) != year:
                continue
            if min_score is not None:
                score = entry.evaluation.score if entry.evaluation is not None else None
                if score is None or score < min_score:
                    continue
            if source_cleaned and str(paper.get("source") or "").lower() != source_cleaned:
                continue
            if status_cleaned and entry.status() != status_cleaned:
                continue
            matched.append((paper_id, entry))
        # 用进入工作区的时间排序，保证分页结果稳定（后检索的排在后面）。
        matched.sort(key=lambda pair: pair[1].added_at)
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 20))
        return len(matched), matched[start:end]


def paper_identity(paper: JsonObject) -> str:
    """从论文元数据字典里取出稳定的论文编号。

    优先级和检索服务的去重键一致：paperId > id > doi > 标题。
    编号会去掉首尾空白；什么都没有时返回空字符串。
    """

    for key in ("paperId", "id", "doi", "title"):
        value = str(paper.get(key) or "").strip()
        if value:
            return value
    return ""


def _optional_int(value: Any) -> int | None:
    """把输入安全转换成整数，转换不了返回 None。"""

    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
