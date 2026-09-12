from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

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
