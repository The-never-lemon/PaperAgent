from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import re

from src.models.sessions import utc_now


JsonObject = dict[str, Any]

# 精读报告的数据格式版本号。
# 以后如果给报告增加新字段，就把这个数字加一；读取旧文件时不会因为版本号不同而报错，
# 只是缺失的字段会用默认值补上（旧数据永远不会因为升级而打不开）。
DEEP_READ_SCHEMA_VERSION = 1

# 报告来源只有两种合法取值：
# - "fulltext"：成功下载到论文全文，报告是通读全文后写出来的；
# - "abstract_fallback"：拿不到全文，只能根据标题和摘要写一份降级报告。
DEEP_READ_SOURCE_FULLTEXT = "fulltext"
DEEP_READ_SOURCE_ABSTRACT = "abstract_fallback"

# 每个维度打分的合法范围。模型偶尔会给出超出范围的数字，这里统一修正回范围内。
DIMENSION_SCORE_MIN = 0
DIMENSION_SCORE_MAX = 100

# 四个评分维度的中文名和先后顺序。
# 顺序和前端精读抽屉里从上往下排的一样，保证"网页上看到什么，下载的文件里就是什么"。
_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("relevance", "相关性"),
    ("novelty", "创新性"),
    ("rigor", "严谨性"),
    ("clarity", "清晰度"),
)

# 报告依据的材料对应的中文说法（和前端抽屉上那个来源小标签一致）。
_SOURCE_LABELS: dict[str, str] = {
    DEEP_READ_SOURCE_FULLTEXT: "全文精读",
    DEEP_READ_SOURCE_ABSTRACT: "摘要降级",
}

# 这些列表字段是按叙述顺序展开的，下载成 Markdown 时用有序列表，
# 和「数据集」「局限」那种并列项区分开。
_NUMBERED_HEADINGS: frozenset[str] = frozenset({"方法", "贡献", "主要结果"})

# 实验设置在技能文档里约定写成「标签：完整句子」四行。模型却经常把四块
# 糊进同一段 JSON 字符串，所以组装和展示时都要按标签切开，不能只认行首。
_SETUP_HEADING = "实验设置"
_SETUP_LABELS: tuple[str, ...] = ("数据集：", "评价指标：", "骨干与超参：", "训练细节：")
_SETUP_ALIAS_TO_CANONICAL: dict[str, str] = {
    "数据集": "数据集",
    "训练数据": "数据集",
    "评价指标": "评价指标",
    "评测指标": "评价指标",
    "评估指标": "评价指标",
    "骨干与超参": "骨干与超参",
    "骨干网络": "骨干与超参",
    "模型与超参": "骨干与超参",
    "超参数": "骨干与超参",
    "骨干": "骨干与超参",
    "训练细节": "训练细节",
    "训练设置": "训练细节",
    "训练过程": "训练细节",
}
_SETUP_SPLIT_PREFIX = frozenset(" \t\n。；;，,、")

# 分段阅读笔记会在句末标 [paperId:p0001:s0003]。这是内部定位，不能进给人看的报告。
_BRACKET_CHUNK_RE = re.compile(
    r"\[\s*[^\[\]]*:(?:p|c)\d{4}(?::s\d{4})?\s*\]",
    re.IGNORECASE,
)
_BARE_CHUNK_RE = re.compile(
    r"(?<![\w.])[\w.\-]+:(?:p|c)\d{4}(?::s\d{4})?(?![\w])",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DimensionScore:
    """单个评价维度的分数和打分理由。

    Attributes:
        score: 0 到 100 之间的整数分数。
        rationale: 模型给出的打分理由，一两句话说清楚为什么是这个分。
    """

    score: int = 0
    rationale: str = ""

    def to_dict(self) -> JsonObject:
        """把维度分数转换成普通字典，方便写进 JSON 文件。"""

        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "DimensionScore":
        """从普通字典还原维度分数。

        读取时保持宽容：字段缺失就用默认值，分数越界就修正到合法范围，
        保证旧文件或者模型输出不规范时程序也不会崩溃。
        """

        payload = data if isinstance(data, dict) else {}
        return cls(
            score=_clamp_score(payload.get("score")),
            rationale=str(payload.get("rationale") or ""),
        )


@dataclass(slots=True)
class DeepReadReport:
    """一篇论文精读完成后生成的结构化报告。

    这份报告是"精读 + 可追问"能力的记忆载体：
    1. 追问子 Agent 会把它和论文全文一起放进上下文，回答用户的细节问题；
    2. 前端精读抽屉直接展示这里的字段；
    3. 报告会以 JSON 文件形式存进会话产物（artifact），保证刷新页面后还能打开。

    字段约定（工程规范 8.2）：这份报告的结构只允许增加新字段，不允许修改或删除
    已有字段，这样旧会话里保存的报告文件永远可以继续被打开。

    Attributes:
        schema_version: 数据格式版本号，见 DEEP_READ_SCHEMA_VERSION。
        paper_id: 对应论文在工作区里的编号。
        title: 论文标题。
        source: 报告依据的材料，"fulltext"（全文）或 "abstract_fallback"（只有摘要）。
        main_question: 论文要解决的核心问题。
        methods: 论文使用的方法列表。
        datasets: 论文使用的数据集列表。
        contributions: 论文的主要贡献列表。
        limitations: 论文的局限性列表。
        main_results: 论文的主要实验结果列表。
        short_summary: 一段话的简短总结。
        experimental_setup: 实验设置的描述（基线、环境、评测指标等）。
        conclusions: 论文结论部分的描述。
        relevance: 与研究主题的相关性打分。
        novelty: 创新性打分。
        rigor: 严谨性打分。
        clarity: 表达清晰度打分。
        overall_score: 综合总分（0-100）。
        overall_comment: 综合评语。
        created_at: 报告生成时间（UTC ISO 字符串）。
        artifact_id: 报告 JSON 文件在会话产物里的编号（前端下载/回看用）。
        fulltext_artifact_id: 论文全文 Markdown 在会话产物里的编号（追问子 Agent 加载全文用）；
            摘要降级精读时为空字符串。
    """

    schema_version: int = DEEP_READ_SCHEMA_VERSION
    paper_id: str = ""
    title: str = ""
    source: str = DEEP_READ_SOURCE_ABSTRACT
    main_question: str = ""
    methods: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    contributions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    main_results: list[str] = field(default_factory=list)
    short_summary: str = ""
    experimental_setup: str = ""
    conclusions: str = ""
    relevance: DimensionScore = field(default_factory=DimensionScore)
    novelty: DimensionScore = field(default_factory=DimensionScore)
    rigor: DimensionScore = field(default_factory=DimensionScore)
    clarity: DimensionScore = field(default_factory=DimensionScore)
    overall_score: int = 0
    overall_comment: str = ""
    created_at: str = field(default_factory=utc_now)
    artifact_id: str = ""
    fulltext_artifact_id: str = ""

    def __post_init__(self) -> None:
        """落盘和展示前把内部定位号剥掉，并把实验设置切成可扫读的几行。

        中文说明：
        分段阅读必须在笔记里标 [chunk_id]，汇总模型又常把这些编号抄进报告。
        实验设置又是一个字符串字段，模型几乎不会真的写出换行。这两件事都不能
        指望提示词单独约束，所以在报告对象一创建就处理掉。旧文件走 from_dict
        也会进这里，打开历史报告时同样干净。
        """

        self.main_question = strip_chunk_markers(self.main_question)
        self.short_summary = strip_chunk_markers(self.short_summary)
        self.conclusions = strip_chunk_markers(self.conclusions)
        self.overall_comment = strip_chunk_markers(self.overall_comment)
        self.methods = _cleaned_string_list(self.methods)
        self.datasets = _cleaned_string_list(self.datasets)
        self.contributions = _cleaned_string_list(self.contributions)
        self.limitations = _cleaned_string_list(self.limitations)
        self.main_results = _cleaned_string_list(self.main_results)
        self.experimental_setup = normalize_experimental_setup(self.experimental_setup)
        for dimension in (self.relevance, self.novelty, self.rigor, self.clarity):
            dimension.rationale = strip_chunk_markers(dimension.rationale)

    def to_dict(self) -> JsonObject:
        """把整份报告转换成普通字典，方便写进 JSON 文件或推给前端。"""

        return {
            "schema_version": self.schema_version,
            "paper_id": self.paper_id,
            "title": self.title,
            "source": self.source,
            "main_question": self.main_question,
            "methods": list(self.methods),
            "datasets": list(self.datasets),
            "contributions": list(self.contributions),
            "limitations": list(self.limitations),
            "main_results": list(self.main_results),
            "short_summary": self.short_summary,
            "experimental_setup": self.experimental_setup,
            "conclusions": self.conclusions,
            "relevance": self.relevance.to_dict(),
            "novelty": self.novelty.to_dict(),
            "rigor": self.rigor.to_dict(),
            "clarity": self.clarity.to_dict(),
            "overall_score": self.overall_score,
            "overall_comment": self.overall_comment,
            "created_at": self.created_at,
            "artifact_id": self.artifact_id,
            "fulltext_artifact_id": self.fulltext_artifact_id,
        }

    def to_markdown(self) -> str:
        """把整份报告拼成一段 Markdown 文本，用来下载成 .md 文件。

        中文说明：
        章节顺序和前端精读抽屉里从上往下看到的一模一样——网页上看到什么，
        下载下来的文件里就是什么。某一节没有内容时会跳过它，不留下空标题。
        方法、贡献、主要结果按叙述顺序编号；数据集和局限用短横线；实验设置
        保留换行并把「数据集：」这类标签加粗，避免整份报告看起来像一段墙。
        """

        # 每一"块"内容最后用空行拼起来。开头是报告标题和一行来源说明。
        blocks: list[str] = [
            f"# {_one_line(self.title) or '精读报告'}",
            f"> {_SOURCE_LABELS.get(self.source, '摘要降级')} · 生成于 {_format_created_at(self.created_at)}",
        ]

        # 评分块：先把总分单独亮出来，再用一张表格列出四个维度的分数和打分理由。
        score_lines: list[str] = [
            "## 评分",
            "",
            f"**综合评分：{self.overall_score} / 100**",
            "",
            "| 维度 | 分数 | 打分理由 |",
            "| --- | --- | --- |",
        ]
        for attribute, label in _DIMENSIONS:
            dimension: DimensionScore = getattr(self, attribute)
            score_lines.append(f"| {label} | {dimension.score} | {_table_cell(dimension.rationale)} |")
        # 总评是一句整体评价，写在表格下面；没有就不写。
        overall_comment = _one_line(self.overall_comment)
        if overall_comment:
            score_lines += ["", f"**总评**：{overall_comment}"]
        blocks.append("\n".join(score_lines))

        # 剩下的正文章节，按抽屉里的先后顺序排好。
        # 注意这里的顺序不能按字段定义顺序来（那样"实验设置""结论"会跑到"方法"前面），
        # 必须照着前端抽屉的展示顺序。
        sections: tuple[tuple[str, str | list[str]], ...] = (
            ("一段话总结", self.short_summary),
            ("核心问题", self.main_question),
            ("方法", self.methods),
            ("数据集", self.datasets),
            ("贡献", self.contributions),
            ("主要结果", self.main_results),
            ("实验设置", self.experimental_setup),
            ("结论", self.conclusions),
            ("局限", self.limitations),
        )
        for heading, content in sections:
            section = _render_section(heading, content)
            if section:
                blocks.append(section)

        # 结尾补一个换行，让文件以空行收尾（和综述 Markdown 产物的习惯保持一致）。
        return "\n\n".join(blocks).strip() + "\n"

    def to_card_payload(
        self,
        *,
        source: str,
        fulltext_available: bool,
        fulltext_failure_reason: str | None,
        artifact_id: str,
    ) -> JsonObject:
        """拼一张对话流里的精读卡片，只带标题、一句话总结和分数。

        中文说明：
        完整报告已经写进工作区和产物文件了。卡片事件如果再塞一整份报告，
        每次打开旧会话都要把这些大段文字搬到网页里，页面会卡。
        卡片只需要让人认出是哪篇、分数多少；点「查看完整报告」再去工作区取全文。
        """

        return {
            "kind": "deep_read_report",
            "paper_id": self.paper_id,
            "source": source,
            "artifact_id": artifact_id or self.artifact_id,
            "fulltext_available": fulltext_available,
            "fulltext_failure_reason": fulltext_failure_reason or "",
            "title": self.title,
            "short_summary": self.short_summary,
            "overall_score": self.overall_score,
            "relevance": self.relevance.score,
            "novelty": self.novelty.score,
            "rigor": self.rigor.score,
            "clarity": self.clarity.score,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DeepReadReport":
        """从普通字典还原一份精读报告。

        读取时保持宽容（工程规范 8.2）：
        1. 字典里多出来的未知字段直接忽略；
        2. 缺失的字段一律用默认值补上；
        3. 数值、字符串、列表类型不对时纠正成合法类型。
        这样不管文件是哪个版本写出来的，读取端都不会崩溃。
        """

        payload = data if isinstance(data, dict) else {}
        source = str(payload.get("source") or DEEP_READ_SOURCE_ABSTRACT)
        if source not in {DEEP_READ_SOURCE_FULLTEXT, DEEP_READ_SOURCE_ABSTRACT}:
            # 来源字段出现没见过的值时，按最保守的"只有摘要"处理。
            source = DEEP_READ_SOURCE_ABSTRACT
        return cls(
            schema_version=_positive_int(payload.get("schema_version"), DEEP_READ_SCHEMA_VERSION),
            paper_id=str(payload.get("paper_id") or ""),
            title=str(payload.get("title") or ""),
            source=source,
            main_question=str(payload.get("main_question") or ""),
            methods=_string_list(payload.get("methods")),
            datasets=_string_list(payload.get("datasets")),
            contributions=_string_list(payload.get("contributions")),
            limitations=_string_list(payload.get("limitations")),
            main_results=_string_list(payload.get("main_results")),
            short_summary=str(payload.get("short_summary") or ""),
            experimental_setup=str(payload.get("experimental_setup") or ""),
            conclusions=str(payload.get("conclusions") or ""),
            relevance=DimensionScore.from_dict(payload.get("relevance")),
            novelty=DimensionScore.from_dict(payload.get("novelty")),
            rigor=DimensionScore.from_dict(payload.get("rigor")),
            clarity=DimensionScore.from_dict(payload.get("clarity")),
            overall_score=_clamp_score(payload.get("overall_score")),
            overall_comment=str(payload.get("overall_comment") or ""),
            created_at=str(payload.get("created_at") or "") or utc_now(),
            artifact_id=str(payload.get("artifact_id") or ""),
            fulltext_artifact_id=str(payload.get("fulltext_artifact_id") or ""),
        )


def slim_deep_read_card_metadata(inner: JsonObject) -> JsonObject:
    """把历史卡片事件里可能带着的整份报告，收成卡片真正要展示的那几项。

    中文说明：
    以前的会话把完整报告写进了事件表。打开旧会话时，在发给前端之前
    先收成预览字段，旧数据就不必再把整份报告送到网页里。
    """

    report = inner.get("report") if isinstance(inner.get("report"), dict) else {}
    return {
        "kind": "deep_read_report",
        "paper_id": str(inner.get("paper_id") or report.get("paper_id") or ""),
        "source": str(inner.get("source") or report.get("source") or ""),
        "artifact_id": str(inner.get("artifact_id") or report.get("artifact_id") or ""),
        "fulltext_available": bool(inner.get("fulltext_available")),
        "fulltext_failure_reason": str(inner.get("fulltext_failure_reason") or ""),
        "title": str(inner.get("title") or report.get("title") or ""),
        "short_summary": str(inner.get("short_summary") or report.get("short_summary") or ""),
        "overall_score": _clamp_score(inner.get("overall_score", report.get("overall_score"))),
        "relevance": _dimension_score_value(inner.get("relevance", report.get("relevance"))),
        "novelty": _dimension_score_value(inner.get("novelty", report.get("novelty"))),
        "rigor": _dimension_score_value(inner.get("rigor", report.get("rigor"))),
        "clarity": _dimension_score_value(inner.get("clarity", report.get("clarity"))),
    }


def _dimension_score_value(value: Any) -> int:
    """从「整数」或「带 score 的字典」里取出 0-100 的分数。"""

    if isinstance(value, dict):
        return _clamp_score(value.get("score"))
    return _clamp_score(value)


def _render_section(heading: str, content: str | list[str]) -> str:
    """把一个小节拼成 Markdown 文本；内容为空时返回空字符串。

    中文说明：
    同一份报告里，有的字段是一段话（比如"结论"），有的是好几条要点
    （比如"方法"）。一段话保留换行，实验设置还要把「数据集：」这类标签
    加粗并空开；有顺序的要点用 1. 2. 3.，并列项才用短横线。整节都没内容
    就返回空字符串，调用方看到空字符串就跳过这一节，不会留下一个孤零零的标题。
    """

    if isinstance(content, str):
        text = (
            _format_experimental_setup(content)
            if heading == _SETUP_HEADING
            else _preserve_lines(content)
        )
        return f"## {heading}\n\n{text}" if text else ""

    numbered = heading in _NUMBERED_HEADINGS
    lines: list[str] = []
    index = 1
    for item in content:
        text = _one_line(item)
        if not text:
            continue
        if numbered:
            lines.append(f"{index}. {text}")
            index += 1
        else:
            lines.append(f"- {text}")
    if not lines:
        return ""
    return f"## {heading}\n\n" + "\n".join(lines)


def _preserve_lines(text: Any) -> str:
    """保留段落换行，只把每一行内部的空白收干净。

    中文说明：
    总结、结论、实验设置是给人连着读的，模型如果已经按行切开，下载时
    不能再压成一行墙。空行丢掉，避免 Markdown 里出现大段空白。
    """

    lines: list[str] = []
    for raw in str(text or "").splitlines():
        collapsed = " ".join(raw.split())
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


def _format_experimental_setup(text: Any) -> str:
    """把实验设置切成带加粗标签的段落，行与行之间空开。

    中文说明：
    技能文档要求写成四行标签，但模型经常糊成一段。这里先按标签（或句子）
    切开，再给「数据集：」这类行首标签加粗。旧报告没有标签时，至少按句分段。
    """

    body = normalize_experimental_setup(text)
    if not body:
        return ""
    formatted: list[str] = []
    for line in body.split("\n"):
        for label in _SETUP_LABELS:
            if line.startswith(label):
                rest = line[len(label) :].lstrip()
                line = f"**{label[:-1]}**：{rest}"
                break
        formatted.append(line)
    return "\n\n".join(formatted)


def strip_chunk_markers(text: Any) -> str:
    """去掉分段笔记里的内部定位编号，例如 [1706.03762:p0001:s0003]。"""

    cleaned = _BRACKET_CHUNK_RE.sub("", str(text or ""))
    cleaned = _BARE_CHUNK_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([，。；、,.!！？])", r"\1", cleaned)
    return cleaned.strip()


def normalize_experimental_setup(text: Any) -> str:
    """把实验设置整理成一行一块：有标签按标签切，没有标签按句子切。"""

    cleaned = strip_chunk_markers(text)
    if not cleaned:
        return ""
    labeled = _split_by_setup_labels(cleaned)
    if labeled:
        lines: list[str] = []
        for label, body in labeled:
            body = " ".join(body.split()).strip()
            if not body:
                continue
            if label:
                lines.append(f"{label}：{body}")
            else:
                lines.append(body)
        if lines:
            return "\n".join(lines)
    return _break_setup_prose(cleaned)


def _split_by_setup_labels(text: str) -> list[tuple[str, str]]:
    """按「数据集：」「评价指标：」等标签切开；找不到标签就返回空列表。"""

    aliases = sorted(_SETUP_ALIAS_TO_CANONICAL, key=len, reverse=True)
    pattern = "|".join(re.escape(alias) for alias in aliases)
    regex = re.compile(rf"({pattern})[:：]")
    matches = [
        match
        for match in regex.finditer(text)
        if match.start() == 0 or text[match.start() - 1] in _SETUP_SPLIT_PREFIX
    ]
    if not matches:
        return []
    parts: list[tuple[str, str]] = []
    prefix = text[: matches[0].start()].strip()
    if prefix:
        parts.append(("", prefix))
    for index, match in enumerate(matches):
        canonical = _SETUP_ALIAS_TO_CANONICAL[match.group(1)]
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts.append((canonical, text[match.end() : end]))
    return parts


def _break_setup_prose(text: str) -> str:
    """没有标签时，按句号或分号把一段墙拆成几行。"""

    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？])", collapsed)
        if part.strip()
    ]
    if len(sentences) > 1:
        return "\n".join(sentences)
    pieces = [part.strip() for part in re.split(r"[；;]", collapsed) if part.strip()]
    if len(pieces) > 1:
        return "\n".join(pieces)
    return collapsed


def _cleaned_string_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """列表字段逐项去掉内部定位号，剥空的项丢掉。"""

    cleaned: list[str] = []
    for item in values or ():
        text = strip_chunk_markers(item)
        if text:
            cleaned.append(text)
    return cleaned


def _one_line(text: Any) -> str:
    """把一段文字压成一行：换行、制表符、连续空格全部变成一个空格。

    中文说明：
    列表里的每一条要点、标题、表格单元格都应该是单行。Markdown 里一条
    要点如果被换行劈成两行，编号列表会断掉；压成一行最省事也最稳。
    """

    return " ".join(str(text or "").split())


def _table_cell(text: Any) -> str:
    """把一段文字整理成能放进 Markdown 表格单元格的形式。

    中文说明：
    表格是靠竖线分列的，所以文字里原本的竖线要写成 \\| 转义掉，
    否则读起来会平白多出一列。换行也顺手压成空格。
    """

    return _one_line(text).replace("|", "\\|")


def _format_created_at(value: Any) -> str:
    """把报告生成时间（UTC ISO 字符串）整理成"2026-09-14 08:30 UTC"这种样子。

    中文说明：
    这里只做字符串截取，不把时间解析成对象——万一老报告里存着格式怪异的
    时间字符串，截取顶多显示得难看一点，不会让整个下载直接报错。
    """

    text = str(value or "")
    if len(text) < 19:
        return text or "未知时间"
    return text[:19].replace("T", " ") + " UTC"


def _clamp_score(value: Any) -> int:
    """把任意输入整理成 0-100 之间的整数分数，整理不了就返回 0。"""

    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return 0
    return max(DIMENSION_SCORE_MIN, min(DIMENSION_SCORE_MAX, resolved))


def _positive_int(value: Any, default: int) -> int:
    """把任意输入整理成正整数，整理不了就返回默认值。"""

    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return resolved if resolved > 0 else default


def _string_list(value: Any) -> list[str]:
    """把任意输入整理成字符串列表。

    单个字符串会被包装成只有一个元素的列表；列表里的非字符串元素会被转成字符串；
    空白元素直接丢弃。这样模型输出格式稍有偏差时数据也不会丢。
    """

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, (list, tuple)):
        return []
    cleaned: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            cleaned.append(text)
    return cleaned
