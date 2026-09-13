"""从 PDF 第一页里认出标题、作者、年份和摘要。

中文说明：
用户上传的本地 PDF 只有文件本身，检索链路带回来的标题、作者、摘要一概没有。
这份文件负责"只读第一页"，靠字的大小和文字排在哪里，把这几样尽量认出来，
让上传的论文在列表里显示得像一篇正常论文，也能参与综述写作。

为什么只读第一页：标题、作者、摘要基本都印在第一页，读一页只要几十毫秒；
整篇转一遍代价大得多，而且精读流程后面会自己做，不该在"上传"这一步就干重活。

认不出来就老老实实返回空值，绝不硬猜：调用方拿不到标题时会退回去用文件名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# arXiv 水印行的长相，例如 "arXiv:2401.00001v1  [q-fin.PM]  18 Nov 2023"。
# 中文注释：这一行的字常常比标题还大（实测 20.0，标题才 15.9），而且出现的位置
# 很不固定——有时在标题上方，有时夹在摘要正文中间。所以只能靠文字特征把它挑出来，
# 靠字大小是挑不出来的。
_ARXIV_WATERMARK = re.compile(r"^\s*arXiv:\s*\d", re.IGNORECASE)

# 摘要的开头。中文注释：三种常见写法都要认——"Abstract" 单独占一行、
# "Abstract— In this paper..." 和正文连在同一行、以及中文的"摘要"。
_ABSTRACT_START = re.compile(r"^\s*(abstract|摘要)\b\s*[—–\-:.…]*\s*", re.IGNORECASE)

# 摘要讲完了的标志之一：出现这几个词就说明摘要部分结束了。
_ABSTRACT_END = re.compile(r"^\s*(index terms|keywords|关键字|关键词)", re.IGNORECASE)

# 摘要讲完了的标志之二：下一节的标题，长相是 "1 Introduction" 或 "I. INTRODUCTION"。
# 中文注释：这里故意不忽略大小写。忽略的话，正文里的 "v. Smith"、"i. e." 这类
# 普通句子也会被当成章节标题，摘要会被提前截断。
_SECTION_HEADING = re.compile(r"^\s*(\d+\.?\s+[A-Z]|[IVX]+\.\s+[A-Z])")

# 明显不是人名的词。作者行里混进这些词，说明这一行其实是机构名或者别的什么。
_NOT_AUTHOR_WORDS = (
    "university", "institute", "department", "laboratory", "school", "college",
    "academy", "preprint", "abstract", "keywords", "email", "arxiv",
)

# 人名后面挂着的上标序号和各种符号（例如 "Janghyun Cho1,∗" 里的 "1" 和 "∗"）。
_AUTHOR_MARKS = re.compile(r"[0-9*†‡§¶∗#]+")

# 标题两行的字大小允许差多少。中文注释：实测同一篇论文的两行标题字大小会差 0.1
# （17.1 和 17.2），所以不能要求完全相等。
_TITLE_SIZE_TOLERANCE = 0.6

# 标题的第二行最多能离第一行多远，按"字大小的几倍"算。
# 中文注释：这是为了防止把远处一个碰巧同样大的小标题也当成标题的一部分。
_TITLE_GAP_RATIO = 1.6

# 在标题下方多大范围里找作者行，按"标题字大小的几倍"算。
_AUTHOR_WINDOW_RATIO = 4.0

# 上下差几个点以内算"同一排"。中文注释：双栏排版的论文，作者会分左右两列出现，
# 它们的上下位置差不多，要当成同一排合起来看。
_SAME_ROW_TOLERANCE = 3.0

# 作者最多保留几个。
_MAX_AUTHORS = 10

# 页面最上面和最下面各留多大比例不算正文。
# 中文注释：这两条边上通常是页码、期刊名之类的页眉页脚，不该参与标题判断。
_HEADER_FOOTER_RATIO = 0.05

# 摘要最多保留多少个字符。中文注释：综述那条链路自己会截短，这里留宽一点，
# 前端点开论文信息时也能看到完整摘要。
ABSTRACT_MAX_CHARS = 2000


@dataclass(slots=True)
class PdfFirstPageInfo:
    """从 PDF 第一页读到的东西，读不到就是空值。"""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    page_count: int | None = None
    # 中文注释：True 说明能读出文字；False 说明这一页一个字都没读出来，多半是扫描版
    # 或者整页都是图片；None 表示判断不了（比如这台机器上没装读 PDF 的库）。
    # 千万不要把 None 当成 False 用，那会把正常论文说成扫描版。
    has_text_layer: bool | None = None


@dataclass(slots=True)
class _TextRow:
    """第一页里的一行文字，外加判断位置用的几个数字。"""

    text: str
    size: float
    top: float
    bottom: float
    left: float
    watermark: bool


def extract_first_page_info(pdf_path: Path, *, abstract_max_chars: int = ABSTRACT_MAX_CHARS) -> PdfFirstPageInfo:
    """只读 PDF 的第一页，尽量认出标题、作者、年份和摘要。

    中文注释：扫描版（整页是图片、没有文字）时读不出任何一行字，返回的结果里
    只有页数，has_text_layer 是 False，其余字段全空。调用方看到 False 就应该
    提前告诉用户"这份 PDF 精读不了全文"。

    这里从头到尾都做了兜底：认不出来、文件坏了、没装读 PDF 的库，都只是返回
    一份空结果，绝不会往外抛异常——上传功能不能因为"认不出标题"就失败。
    """

    try:
        import pymupdf
    except Exception:
        # 中文注释：没装 PyMuPDF 就不猜。has_text_layer 保持 None（判断不了），
        # 标题由调用方退回去用文件名。
        return PdfFirstPageInfo()

    document = None
    try:
        document = pymupdf.open(str(pdf_path))
        info = PdfFirstPageInfo(page_count=int(document.page_count), year=_year_from_creation_date(document))
        if document.page_count <= 0:
            return info

        page = document[0]
        rows = _read_rows(page)
        info.has_text_layer = bool(rows)
        if not rows:
            # 中文注释：一行字都没读出来，就是扫描版那一类，后面不用再算了。
            return info

        title_rows = _pick_title_rows(rows)
        if title_rows:
            info.title = _join_rows(title_rows)
        # 中文注释：下面两件事都以标题的位置为起点：作者在标题正下方，摘要在标题更下面。
        title_size = max((row.size for row in title_rows), default=0.0)
        title_bottom = max((row.bottom for row in title_rows), default=0.0)

        info.authors = _pick_authors(rows, title_bottom=title_bottom, title_size=title_size)
        info.year = info.year or _year_from_watermark(rows)
        info.abstract = _pick_abstract(rows, start_below=title_bottom, max_chars=abstract_max_chars)
        return info
    except Exception:
        return PdfFirstPageInfo()
    finally:
        if document is not None:
            document.close()


def _read_rows(page) -> list[_TextRow]:
    """把第一页的文字按"行"读出来，顺便记下每行的字大小和上下位置。

    中文注释：读 PDF 的库给出的结构是"块 → 行 → 片段"，一小段同样格式的文字
    就是一个片段。我们按"行"来用，这一行的字大小取行里最大的那个片段。
    """

    page_height = float(page.rect.height)
    rows: list[_TextRow] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            # 中文注释：type 不是 0 的块是图片之类的非文字内容，跳过。
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"]).strip()
            if not text:
                continue
            top = float(line["bbox"][1])
            bottom = float(line["bbox"][3])
            if top < page_height * _HEADER_FOOTER_RATIO or bottom > page_height * (1 - _HEADER_FOOTER_RATIO):
                # 中文注释：落在最上面或最下面那条边上的，是页眉页脚，不是正文。
                continue
            rows.append(
                _TextRow(
                    text=text,
                    size=max(float(span["size"]) for span in line["spans"]),
                    top=top,
                    bottom=bottom,
                    left=float(line["bbox"][0]),
                    watermark=bool(_ARXIV_WATERMARK.match(text)),
                )
            )
    return rows


def _pick_title_rows(rows: list[_TextRow]) -> list[_TextRow]:
    """挑出标题那一组行。

    中文注释：标题的字是全页第二大的——最大的是 arXiv 水印行，前面已经把它标出来了，
    这里先排除掉。标题可能排成两行，两行的字大小会差一点点，所以比较时留了余量；
    而且只把"上下紧挨着"的行算作同一组，免得把下面某个碰巧一样大的小标题也拉进来。
    """

    candidates = [row for row in rows if not row.watermark]
    if not candidates:
        return []
    biggest = max(row.size for row in candidates)
    same_size = sorted(
        (row for row in candidates if biggest - row.size <= _TITLE_SIZE_TOLERANCE),
        key=lambda row: (row.top, row.left),
    )
    block = [same_size[0]]
    for row in same_size[1:]:
        if row.top - block[-1].bottom <= biggest * _TITLE_GAP_RATIO:
            block.append(row)
        else:
            # 中文注释：离得太远了，说明后面这些只是碰巧一样大，不属于标题。
            break
    return block


def _join_rows(rows: list[_TextRow]) -> str:
    """把一组行按从上到下的顺序拼成一句话。

    中文注释：必须按上下位置排序再拼。实测有的 PDF 里两行标题的读取顺序是反的，
    直接照读取顺序拼，会拼出 "EXPLORATION LATENT SPACE REINFORCEMENT..." 这种怪标题。
    """

    ordered = sorted(rows, key=lambda item: (item.top, item.left))
    return re.sub(r"\s+", " ", " ".join(row.text for row in ordered)).strip()


def _pick_authors(rows: list[_TextRow], *, title_bottom: float, title_size: float) -> list[str]:
    """在标题下方找作者行。

    中文注释：作者紧挨在标题下面、字比标题小。但这一带还混着机构名、
    "A PREPRINT" 之类的字样，所以找到一行之后还要检查它"像不像一串人名"，
    不像就往下看下一排。宁可为空，也不要把机构名当人名显示给用户——
    实测有一篇论文标题正下方就是 "Department of Aerospace Engineering"。
    """

    if title_size <= 0:
        return []
    window_bottom = title_bottom + title_size * _AUTHOR_WINDOW_RATIO
    candidates = sorted(
        (
            row
            for row in rows
            if title_bottom <= row.top <= window_bottom and row.size < title_size and not row.watermark
        ),
        key=lambda row: (row.top, row.left),
    )
    for group in _group_same_row(candidates):
        # 中文注释：同一排上可能有左右好几块文字（作者分栏排版很常见）。要一块一块
        # 分开看，不能先把它们拼成一整句——两块并列的人名拼起来会粘成一个名字，
        # 实测 "Sriram Rajasekar" 和 "Ashwini Ratnoo" 并排时就被粘成过一个人。
        names: list[str] = []
        for row in group:
            part = _split_authors(row.text)
            if not part:
                # 中文注释：这一块不像人名（是机构名、"A PREPRINT" 之类），
                # 说明这一排整个都不是作者行，丢掉它，往下看下一排。
                names = []
                break
            names.extend(part)
        if names:
            return names[:_MAX_AUTHORS]
    return []


def _group_same_row(rows: list[_TextRow]) -> list[list[_TextRow]]:
    """把已经排好序的行按上下位置分组，同一排的归到一组。"""

    groups: list[list[_TextRow]] = []
    for row in rows:
        if groups and abs(row.top - groups[-1][0].top) <= _SAME_ROW_TOLERANCE:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _split_authors(text: str) -> list[str]:
    """把一排文字切成一个个作者名；只要有一段不像人名，整排就作废。

    中文注释：作废是故意的。"Runjia Yang1 and Beining Shi2" 能切出两个好名字；
    而 "1University of California, Davis" 切出来第一段就带着 University，
    整排直接丢掉，比留下一半垃圾强。

    切出来的空片段（比如 "Janghyun Cho1,∗, Jimmy Chiun2,∗" 里被符号占住的那些）
    直接跳过，不算"不像人名"——否则整排会因为一个符号而白丢。
    """

    names: list[str] = []
    for part in re.split(r",|;|&|、|\band\b", text):
        cleaned = _AUTHOR_MARKS.sub("", part).strip(" .·-")
        if not cleaned:
            # 中文注释：这一段清掉符号后什么都不剩，跳过它，继续看后面的名字。
            continue
        if not _looks_like_person_name(cleaned):
            return []
        names.append(cleaned)
    return names[:_MAX_AUTHORS]


def _looks_like_person_name(text: str) -> bool:
    """判断一小段文字像不像一个人名。"""

    if not 2 <= len(text) <= 60:
        return False
    if any(word in text.lower() for word in _NOT_AUTHOR_WORDS):
        return False
    if any(character.isdigit() for character in text):
        return False
    if not any(character.isalpha() for character in text):
        return False
    # 中文注释：人名一般是 1~5 个词（"Guillaume Sartoretti" 是 2 个词）。
    return 1 <= len(text.split()) <= 5


def _pick_abstract(rows: list[_TextRow], *, start_below: float, max_chars: int) -> str:
    """从第一页里截出摘要正文。

    中文注释：摘要有两种写法（"Abstract" 单独一行、"Abstract— 正文"连在一行），
    这里统一按开头几个字来找。找到之后一直往下收，遇到 "Index Terms" 或者下一节
    的标题就停。

    水印行要一行一行地跳过——实测有一篇论文的 arXiv 水印正好夹在摘要正中间，
    不跳过的话，摘要里会被塞进一句 "arXiv:2502.20217v1 [cs.RO] 27 Feb 2025"。
    """

    pieces: list[str] = []
    started = False
    for row in sorted(rows, key=lambda item: (item.top, item.left)):
        if row.watermark:
            continue
        if not started:
            if row.top < start_below:
                # 中文注释：还在标题那一带，摘要不会在这里，继续往下找。
                continue
            matched = _ABSTRACT_START.match(row.text)
            if matched is None:
                continue
            started = True
            # 中文注释："Abstract— In this paper..." 这种写法，正文和开头在同一行，
            # 所以要把开头那几个字之后的剩余部分接着收进来。
            remainder = row.text[matched.end():].strip()
            if remainder:
                pieces.append(remainder)
            continue
        if _ABSTRACT_END.match(row.text) or _SECTION_HEADING.match(row.text):
            break
        pieces.append(row.text)
    return re.sub(r"\s+", " ", " ".join(pieces)).strip()[:max_chars]


def _year_from_creation_date(document) -> int | None:
    """从 PDF 自带的创建时间里取年份，形如 "D:20240102012921Z"。"""

    raw = str((document.metadata or {}).get("creationDate") or "")
    matched = re.search(r"(?:D:)?((?:19|20)\d{2})", raw)
    return int(matched.group(1)) if matched else None


def _year_from_watermark(rows: list[_TextRow]) -> int | None:
    """从 arXiv 水印行里取年份（水印形如 "... 27 Feb 2025"）。

    中文注释：很多 PDF 自带的创建时间要么是空的，要么是"导出文件那一刻"的时间，
    不可靠。水印里的日期反而是现成的、比较准的年份来源。
    """

    for row in rows:
        if not row.watermark:
            continue
        matched = re.search(r"((?:19|20)\d{2})", row.text)
        if matched:
            return int(matched.group(1))
    return None
