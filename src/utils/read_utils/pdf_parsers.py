from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 中文注释：TeX 排版数学公式时会换成一套专门的"数学字体"（CMMI、CMSY 这种），
# 而正文用的是 Computer Modern Roman（CMR）这类字体。所以"这一行用的字体是不是
# 数学字体"曾经是判断公式的唯一办法。
#
# 但这个办法有个治不好的毛病：期刊用的数学字体成千上万，白名单永远追不上。
# 实测一篇用 TeX Gyre Pagella 排版的论文，它的数学字体是 NewPXMI / pxsys / pxmiaX，
# 三个都不在名单里，结果全文 3308 行文字一个公式都没认出来。所以现在改成
# "看字符本身长得像不像数学符号"为主、"看字体"只用来补漏（见下面 _is_math_line）。
#
# 这里必须把 CMR / CMBX / CMSS / CMTT / CMTI 明确排除掉：它们也是 CM 开头的 TeX
# 字体，但装的是普通文字。实测三篇论文里 CMR10 分别出现 243 / 709 / 844 个字符，
# 一旦把它们误当成数学字体，整页正文都会被判定成公式，输出就全毁了。
#
# 加新字体名时有个必须守住的规矩：**只能写数学字体自己的名字，不能写正文也在用的名字**。
# 比如绝不能把 "TeXGyre" 加进来——上面那篇论文的正文正好叫 TeXGyrePagellaX-Regular，
# 加上去整篇正文都会被当成公式（实测第 6 页 51 行普通文字全部误判）。
_MATH_FONT_TOKENS = (
    "CMMI",
    "CMSY",
    "CMEX",
    "MSAM",
    "MSBM",
    "MTMI",
    "MTSY",
    "MTEX",
    "Symbol",
    "MathematicalPi",
    "STIX",
    "Euclid",
    # TeX Gyre / newpx 系列（Palatino 那一支论文常用来排数学）
    "NewPXMI",
    "NewPXSY",
    "NewPXEX",
    "pxmi",
    "pxmiaX",
    "pxsys",
    "pxex",
    # 另一支 txfonts / Times 系的数学字体
    "txmi",
    "txsys",
    "txexs",
    # Office 的数学字体，Word 排版的论文会用
    "CambriaMath",
    # 其他常见独立数学字体包
    "XITSMath",
    "LatinModernMath",
    "TeXGyrePagellaMath",
    "TeXGyreTermesMath",
    "TeXGyreBonumMath",
    "AsanaMath",
    "MnSymbol",
)
_TEXT_FONT_TOKENS = ("CMR", "CMBX", "CMSS", "CMTT", "CMTI", "CMB")

# 一行里"数学字符"占比超过这个值，就认为整行是公式。
_MATH_CHARACTER_RATIO = 0.5

# 中文注释：下面这批区间是"一眼就能看出是数学"的字符。和前端
# front/src/lib/math-text.ts 里判断伪公式用的是同一套思路，改了这边那边也要看一眼。
#
# 最要紧的是 U+1D400–U+1D7FF 这一段（数学字母数字符号）：论文里那些斜体的
# 𝑆、𝛼、𝑊、𝑙 全在这里。TeX Gyre 这类现代数学字体抽出来的就是这些字符，
# 靠它就能认出公式，不用管字体叫什么名字。
_MATH_UNICODE_RANGES = (
    (0x0370, 0x03FF),  # 希腊字母：α β γ θ λ ω
    (0x2070, 0x209F),  # 上下标：⁰ ¹ ₂ ₃
    (0x2100, 0x214F),  # 字母式符号：ℐ ℓ ℬ
    (0x2190, 0x21FF),  # 箭头：← → ⇒
    (0x2200, 0x22FF),  # 数学运算符：∑ ∏ ∈ ≤ √ ∇ ∂
    (0x27C0, 0x27EF),  # 数学符号 A：⟦ ⟧
    (0x2980, 0x29FF),  # 数学符号 B：⦀ ⦁
    (0x2A00, 0x2AFF),  # 补充数学运算符：⨀ ⨁
    (0x1D400, 0x1D7FF),  # 数学字母数字：𝑆 𝛼 𝑊 𝑙（现代论文公式的主要来源）
)

# 中文注释：这几个符号不在上面任何一段区间里，但也是明确的数学符号，单独列出来。
_MATH_SYMBOL_CHARS = frozenset("±·×÷∓∗∘√∞∂∇‖−′″")

# 一行至少要这么多非空白字符才算公式。中文注释：加这条是为了挡掉页码、孤立的
# 单个斜体字母这类噪声——实测页码 "5" 和孤立的 "𝑖" 都会被单字符判据误抓。
_MIN_MATH_LINE_CHARS = 2

# ---------- 公式区域的形状参数 ----------
# 中文注释：上下标在 PDF 里是分开排的，同一行公式会被切成好几小块。所以判定完
# "哪些行是公式"之后，还要把这些行按位置粘回一块，才能截出一张完整的公式图。
_REGION_VERTICAL_GAP = 6.0  # 两行上下相距不超过这么多点，就算挨在一起
_REGION_HORIZONTAL_GAP = 8.0  # 两行左右相距不超过这么多点，就算挨在一起
# 中文注释：分子和分母经常隔得比上面这条更开，但仍是同一条公式。
_STACK_VERTICAL_GAP = 16.0
# 中文注释：公式左边常是普通字体的函数名，离数学符号有一小段空。
# 栏和栏之间的空白比这更大，超过就不收，免得把另一栏的字卷进来。
_ABSORB_HORIZONTAL_GAP = 28.0
# 中文注释：同一条公式中间偶尔隔着一个大符号。这个空当要很小。
# 再大就多半是另一栏的公式，中间那几个字只是编号，不能把两栏粘成一块。
_BRIDGE_MAX_GAP = 36.0
_REGION_PADDING_X = 10.0  # 截图时左右各多留一点，避免把分数线、根号切掉
_REGION_PADDING_Y = 4.0  # 截图时上下各多留一点
_MAX_REGION_AREA_RATIO = 0.5  # 一块区域要是占了半个页面，那肯定是认错了
_MAX_REGION_ROWS = 10  # 一块区域超过这么多行，也当认错处理
# 中文注释：判断"这个公式是不是夹在句子中间"时，看同一水平位置左右离它多近才算同一行。
_INLINE_NEIGHBOR_GAP = 3.0
# 中文注释：算作"邻近正文"的文字至少要这么长。公式旁边常散着逗号、单个斜体字母这类
# 一两个字符的碎片（它们够不上"整行是公式"的门槛），要是拿它们当"句子里的文字"，
# 好好一条独立公式会被误判成行内公式，也就不会被送去转写了。
_MIN_INLINE_NEIGHBOR_CHARS = 3

# 内容占位符：先把公式或表格在正文里的位置留一个记号，等截图转写完成再换回来。
# 中文注释：这个记号必须单独占一行，而且不能被"删除重复页眉页脚"那一步误删——
# 因为那一步会把数字统一换成 #，两页的 <!-- formula: 5_1 --> 和 <!-- formula: 6_1 -->
# 会被看成同一行，多页首行都是公式时就会被整批删掉，公式就悄悄丢了。
# 所以下面 _protected_lines 里专门给它留了保护。
_MARKER_PATTERN = re.compile(r"^\s*<!--\s*(?:formula|table):\s*\d+_\d+\s*-->\s*$")

# 公式编号的样子：单独一行、形如 (2) 或 (2a)。
_EQUATION_NUMBER_PATTERN = re.compile(r"^\(\s*(\d+[a-z]?)\s*\)$")

# 小于这个字节数的图片基本都是 logo、图标、装饰线，写出来只会给正文添乱。
_MIN_IMAGE_BYTES = 10240

# ---------- 矢量图截图的参数 ----------
# 中文注释：论文里的插图和照片不同，很多是用线条"画"出来的（方法框架图、折线图都是），
# 这类图 get_images 抽不到，只能按线条的位置把那一块截成图片。下面是判断
# "聚出来的一块到底是不是插图"的几个门槛，都很保守。
_VECTOR_FIGURE_DPI = 150  # 截图清晰度：插图上的小字要能看清
_MAX_FIGURE_PX_WIDTH = 1600  # 图宽上限，太宽就按比例降清晰度
_MIN_VECTOR_FIGURE_HEIGHT = 60.0  # 比这矮的，是分隔线、下划线
_MIN_VECTOR_FIGURE_WIDTH = 80.0  # 比这窄的，是竖线、装饰
_MIN_VECTOR_FIGURE_STROKES = 8  # 线条太少的，多半只是个表格框
_MIN_VECTOR_FIGURE_AREA_RATIO = 0.02  # 占页面太小的一块，不是插图
_MAX_VECTOR_FIGURE_AREA_RATIO = 0.6  # 占了大半页的，是把整页正文当成图了
# 中文注释：这块地方已经有位图或表格时，重合超过这个比例就不再截一遍，避免同一张图收两次。
_VECTOR_FIGURE_OVERLAP_LIMIT = 0.3
# 中文注释：一张候选插图有一半以上压在表格范围里，就当它是表格的一部分，不再当插图收。
_TABLE_REGION_OVERLAP_LIMIT = 0.5

# 图注、表注的编号：阿拉伯数字，或 TABLE I 这种罗马数字。
# 中文注释：编号后面必须是空格、冒号或句号。不然 "TABLE Information" 会被认成第 I 张表。
_CAPTION_INDEX = r"(?:\d+|[IVXLC]{1,8})(?=\s|:|：|\.|$)"
# 图注的样子：以 Figure / Fig. / TABLE / Table 加一个编号开头。
_CAPTION_PATTERN = re.compile(rf"^(Figure|Fig\.?|TABLE|Table)\s*{_CAPTION_INDEX}")
# 从图注里把编号抠出来。中文注释：必须紧跟在 Figure/TABLE 这类词后面才算数，
# 不能"在整句里随便找一个数字"——否则 "Figure 2: accuracy at 50% coverage"
# 这种图注会被当成第 50 张图。罗马数字留给表注，不当成插图的序号。
_CAPTION_NUMBER = re.compile(rf"^(?:Figure|Fig\.?|TABLE|Table)\s*({_CAPTION_INDEX})")
# 表注的样子：以 TABLE / Table 加编号开头。IEEE 论文常写 TABLE I，不是 Table 1。
_TABLE_CAPTION_PATTERN = re.compile(rf"^(?:TABLE|Table)\s*({_CAPTION_INDEX})")
# 中文注释：图注/表注的"正文部分"至少要这么长才算真的。加这条是因为"Table 1"这种
# 三个字也可能只是正文里的一句引用，不是表注——真表注后面一定还跟着说明这张表在讲什么。
_MIN_CAPTION_BODY_CHARS = 10

# 表格行：去掉空格后以 "|" 开头。这类行绝不能被当成页眉页脚删掉。
_TABLE_LINE_PATTERN = re.compile(r"^\s*\|")

# 判断"find_tables 找出来的东西到底像不像真表格"的门槛，三条都很保守。
# 中文注释：这些门槛是为了挡住"把图的标注区当成表格"的情况。实测三篇样本里
# 已经有一篇被误判：整页图的标注被拆成 4 列，表头是 240 多字符的一长串拼凑文字。
_MIN_TABLE_ROWS = 2
_MIN_TABLE_CELLS = 2
_MAX_TABLE_HEADER_CHARS = 200
# 表格矩形被图片盖住的比例超过这个值，就认为它是图的地盘而不是表格。
_MAX_TABLE_IMAGE_COVERAGE = 0.5


@dataclass(slots=True)
class PageBlock:
    """页面上的一块内容。后面的正文和分片都从这里来，不再从一篇长文里倒切。

    kind 只会是这几种：heading 标题、paragraph 段落、display_formula 单独成行的公式、
    inline_formula 夹在句子里的公式、table 表格、figure 图。
    """

    kind: str
    page_number: int
    text: str
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    asset_name: str = ""


# 一级标题：罗马数字开头（I. INTRODUCTION），或 "1. Introduction" 这种编号。
# 字母小节（A. ...）不算，分片时只有一级标题才另起一段。
_H1_PATTERN = re.compile(r"^(?:[IVXLC]{1,8}\.\s+\S.{0,140}|\d+\.\s+[A-Z].{0,140})$")


def _looks_like_heading(text: str) -> bool:
    """这一小段是不是一级标题。只认单独一行、又不太长的那种。"""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) > 160:
        return False
    return _H1_PATTERN.match(lines[0]) is not None


@dataclass(slots=True)
class ParsedPdfPage:
    """保存 PDF 单页解析后的文字。

    中文注释：除了页码和正文，metadata 里还会带上这一页找到几个表格、写出几张图，
    这样上层想知道"这页有没有表格/图"时不用再去读 PDF。
    """

    page_number: int
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PdfFormulaRegion:
    """页面上认出的一处公式，以及它在正文里留下的位置记号。

    中文注释：解析 PDF 的时候还不能立刻把公式转成 LaTeX——那一步要调模型、是异步的，
    而读 PDF 是同步的。所以这里先把公式的"照片地址"（在第几页、什么位置）记下来，
    正文里只放一个占位符。等上层拿到模型以后，再照着这份记录去截图、转写、把占位符换掉。

    is_display 表示这是"单独占一行的公式"还是"夹在句子里的公式"。只有前者需要截图转写；
    后者留在正文里，把数学字符包上 $ 就够了。
    """

    page_number: int  # 在第几页（从 1 开始）
    index: int  # 这一页里的第几个公式（从 1 开始）
    rect: tuple[float, float, float, float]  # 截图范围（左, 上, 右, 下），已经留过白边
    is_display: bool  # 真 = 单独占一行，需要转写；假 = 夹在句子里的
    text: str  # 这块区域里的原始文字（转写失败时用它兜底）
    number: str  # 公式编号，比如 "2"；没有编号就是空字符串
    members: list[tuple[int, int]]  # 组成这块区域的行，记着它们属于第几个文字块的第几行

    @property
    def placeholder(self) -> str:
        """正文里占位用的记号。转写完成后按它把内容换回去。"""

        return f"<!-- formula: {self.page_number}_{self.index} -->"

    @property
    def fallback(self) -> str:
        """转写失败时写回正文的内容。

        中文注释：退回到"把这块区域的原始文字原样放进 $$ 块"。虽然还是没法当 LaTeX 读，
        但比改动前强——以前这些碎片会被排版换行切成一堆独立段落，现在至少是完整的一块。
        """

        return "$$\n" + self.text.strip() + "\n$$"


@dataclass(slots=True)
class PdfTableRegion:
    """页面上认出的一张有图注的表格，以及它在正文里留下的位置记号。

    中文注释：为什么要单独拎出来交给模型——PDF 里表格是"排"出来的，程序只能靠
    "框线在哪"去猜哪个格子属于哪一列。遇到跨两层的表头（一层写数据集、一层写指标），
    猜出来的表头会把好几列的名字糊成一格，结果是"数字都在、但哪一列是哪个指标全没了"。
    实测一篇论文的表头被糊成 "LLaVA-OV-7B 64 + ReKV 0.5 fps + LiveVLM 0.5 fps" 这么一长串。

    整张表截成图交给模型，它能按看到的排版把表头和列对齐写回 Markdown 表格。
    转写失败就退回 fallback（也就是现在那份会糊表头的 Markdown 表），保证不会更差。
    """

    page_number: int
    index: int  # 这一页里的第几张表
    rect: tuple[float, float, float, float]  # 整张表的范围
    caption: str
    fallback: str  # 转写失败时写回正文的 Markdown 表
    # 中文注释：这张表由哪几个 find_tables 找到的区拼起来。一张大表常被拆成好几块，
    # 靠图注把它们归到一起。正文输出时要按这个把对应位置换成占位符。
    members: list[int] = field(default_factory=list)

    @property
    def placeholder(self) -> str:
        """正文里占位用的记号。转写完成后按它把内容换回去。"""

        return f"<!-- table: {self.page_number}_{self.index} -->"


@dataclass(slots=True)
class PdfParseResult:
    """保存一次 PDF 解析的统一结果。

    中文注释：不管底层用 pypdf、PyMuPDF 还是更高级的解析器，最终都整理成
    pages + warnings。调用方只关心这个统一形状。formulas 和 tables 里记着这一篇
    认出了哪些需要模型帮忙的内容，只有 PyMuPDF 解析器会填，pypdf 那条件里永远是空的。
    """

    pages: list[ParsedPdfPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 中文注释：按阅读顺序排好的块。Markdown 和后面的分片都从这份列表渲染，
    # 不再拿渲染完的长文倒过来切。pypdf 那条退路没有块，这里就是空的。
    blocks: list[PageBlock] = field(default_factory=list)
    formulas: list[PdfFormulaRegion] = field(default_factory=list)
    # 中文注释：有图注、需要交给模型重排的表格。没有图注的表走原来的 Markdown 转换，
    # 不进这个清单。
    tables: list["PdfTableRegion"] = field(default_factory=list)


class BasePdfParser(ABC):
    """PDF 解析器基类。

    中文注释：这个类只规定"输入一个 PDF 文件，输出一组页文字"。底层具体怎么读
    文件由子类决定。assets_dir 是留给图片的落盘目录，用不到的解析器可以无视它。
    """

    name = "base"

    @abstractmethod
    def parse(self, source_path: Path, *, assets_dir: Path | None = None) -> PdfParseResult:
        """读取 PDF 并返回统一格式的结果。"""


class PypdfParser(BasePdfParser):
    """用 pypdf 读取 PDF 正文的基础解析器。"""

    name = "pypdf"

    def parse(self, source_path: Path, *, assets_dir: Path | None = None) -> PdfParseResult:
        """用 pypdf 逐页提取文字。

        中文注释：pypdf 对扫描版 PDF、复杂公式和双栏排版不一定理想，但它安装简单、
        速度快，适合作为兜底方案。它读不出图片，所以 assets_dir 参数收下就忽略。
        解析后的文字会先做一层简单清理，减少页眉、页脚和参考文献对后续提取的干扰。
        """

        try:
            from pypdf import PdfReader
        except ImportError:
            return PdfParseResult(warnings=["未安装 pypdf，暂时无法读取 PDF 正文"])
        try:
            reader = PdfReader(str(source_path))
            raw_pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:
            return PdfParseResult(warnings=[f"PDF 正文读取失败：{exc}"])
        if not any(page.strip() for page in raw_pages):
            return PdfParseResult(warnings=["PDF 中没有可读取的文字，可能是扫描文件"])
        cleaned_pages = _remove_repeated_headers_and_footers(raw_pages)
        cleaned_pages = _remove_references_at_end(cleaned_pages)
        pages = [
            ParsedPdfPage(page_number=index, text=_normalise_text(text), metadata={"parser": self.name})
            for index, text in enumerate(cleaned_pages, start=1)
            if text.strip()
        ]
        return PdfParseResult(pages=pages)


class PyMuPdfParser(BasePdfParser):
    """用 PyMuPDF 读取 PDF 正文，并尽量保住表格、公式和图片。

    中文注释：pypdf 只会把文字一股脑倒出来，表格被打散成乱七八糟的字符、公式变成
    残缺符号、图片直接丢掉。PyMuPDF 能看到每个字用的字体、每张图的坐标，所以这里
    能把表格重新拼成 Markdown 表格、把公式标成 $$ 块、把图片存成文件。
    """

    name = "pymupdf"

    def parse(self, source_path: Path, *, assets_dir: Path | None = None) -> PdfParseResult:
        try:
            import pymupdf
        except Exception:
            # 中文注释：pymupdf 没装也不能让 PDF 读不出正文，交给 pypdf 顶上。
            logger.warning("pymupdf 不可用，改用 pypdf 解析 PDF", extra={"source_path": str(source_path)})
            return PypdfParser().parse(source_path, assets_dir=assets_dir)
        try:
            with pymupdf.open(str(source_path)) as doc:
                return self._parse_document(pymupdf, doc, source_path, assets_dir)
        except Exception as exc:
            # 中文注释：pymupdf 解析过程中出任何意外，同样回退到 pypdf。
            # 宁可丢表格和图片，也不能让用户拿不到论文正文。
            logger.warning(
                "pymupdf 解析失败，改用 pypdf 解析 PDF",
                extra={"source_path": str(source_path), "reason": str(exc)},
            )
            return PypdfParser().parse(source_path, assets_dir=assets_dir)

    def _parse_document(
        self, pymupdf: Any, doc: Any, source_path: Path, assets_dir: Path | None
    ) -> PdfParseResult:
        """逐页把表格、公式、图片和正文按阅读顺序拼成一段 Markdown 文本。"""

        if doc.page_count == 0:
            return PdfParseResult(warnings=["PDF 中没有可读取的文字，可能是扫描文件"])
        # 中文注释：同一张图可能被好几页引用（xref 相同），全局记住已经写过的，
        # 避免同一张图重复落盘。
        written_xrefs: set[int] = set()
        # 中文注释：全篇已经写过的图片内容（按内容算的指纹 → 文件名）。同一张图在论文里
        # 可能被引用好几次、每次都是不同的对象编号，按内容记才能挡住重复落盘。
        written_digests: dict[str, str] = {}
        # 有些图附近找不到图注，这时用一个从头到尾递增的编号兜底。
        figure_counter = 0
        # 中文注释：全篇已经用过的图号。图注里写的编号和兜底计数器有可能撞号
        # （比如第 11 张图配到了写着 "Fig. 11" 的图注，第 12 张图没配到图注、
        # 兜底计数恰好也是 11），撞号之后正文里就会出现两张 "Figure 11"，分不清谁是谁。
        used_figure_numbers: set[int] = set()
        page_block_lists: list[list[PageBlock]] = []
        page_metadatas: list[dict[str, Any]] = []
        # 中文注释：把每页认出的公式攒到一起。上层拿到这份清单后，才能照着它去截图转写。
        formulas: list[PdfFormulaRegion] = []
        # 中文注释：有图注、要交给模型重排表头的表格，和公式一样攒起来。
        tables: list[PdfTableRegion] = []
        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_blocks, table_count, figure_count, page_formulas, page_tables = self._render_page(
                pymupdf,
                doc,
                page,
                page_index + 1,
                assets_dir,
                written_xrefs,
                written_digests,
                figure_counter,
                used_figure_numbers,
            )
            figure_counter += figure_count
            formulas.extend(page_formulas)
            tables.extend(page_tables)
            page_block_lists.append(page_blocks)
            page_metadatas.append(
                {"parser": self.name, "table_count": table_count, "figure_count": figure_count}
            )
        # 中文注释：页眉页脚和文末参考文献直接从块里拿掉，再渲染每一页的文字。
        # 这样正文和分片用的是同一份块，不会出现"文字删了、块还在"对不上的情况。
        page_block_lists = _drop_repeated_margin_blocks(page_block_lists)
        page_block_lists = _drop_reference_blocks(page_block_lists)
        pages: list[ParsedPdfPage] = []
        blocks: list[PageBlock] = []
        for page_number, page_blocks in enumerate(page_block_lists, start=1):
            text = _normalise_text("\n\n".join(block.text for block in page_blocks if block.text.strip()))
            if not text.strip():
                continue
            pages.append(
                ParsedPdfPage(page_number=page_number, text=text, metadata=page_metadatas[page_number - 1])
            )
            blocks.extend(page_blocks)
        if not pages:
            return PdfParseResult(warnings=["PDF 中没有可读取的文字，可能是扫描文件"])
        return PdfParseResult(pages=pages, blocks=blocks, formulas=formulas, tables=tables)

    def _render_page(
        self,
        pymupdf: Any,
        doc: Any,
        page: Any,
        page_number: int,
        assets_dir: Path | None,
        written_xrefs: set[int],
        written_digests: dict[str, str],
        figure_offset: int,
        used_figure_numbers: set[int],
    ) -> tuple[list[PageBlock], int, int, list[PdfFormulaRegion], list[PdfTableRegion]]:
        """把一页收成按阅读顺序排好的块。"""

        table_rects, table_markdowns = _collect_tables(page)
        blocks = [block for block in page.get_text("dict")["blocks"] if block.get("type") == 0]

        # 中文注释：先把所有"不在表格里"的文字块连坐标一起收好。后面给图片找图注、
        # 按阅读顺序输出正文都要用到。
        text_blocks: list[tuple[Any, str]] = []
        for block in blocks:
            if _covered_by_table(_rect_tuple(block["bbox"]), table_rects) is not None:
                continue
            block_text = _block_text(block)
            if block_text.strip():
                text_blocks.append((block, block_text))

        # 中文注释：有图注的表格要交给模型重排表头，先在这一页把它们找出来。
        page_tables = _collect_table_regions(table_rects, table_markdowns, text_blocks, page_number)
        region_of_table = {member: region for region in page_tables for member in region.members}

        # 中文注释：调用方没给图片目录时完全跳过图片：不写文件，也不往正文里塞引用。
        if assets_dir is None:
            figures: list[dict[str, Any]] = []
            figure_internal_rects: list[tuple[float, float, float, float]] = []
            figure_count = 0
        else:
            figures, figure_internal_rects, figure_count = _collect_page_figures(
                doc,
                page,
                page_number,
                assets_dir,
                written_xrefs,
                written_digests,
                # 中文注释：表格占的地方不能再算成插图。把表格区域的整块范围也交过去，
                # 免得一张表既被重排成 Markdown 表、又被当成插图讲一遍。
                table_rects + [region.rect for region in page_tables],
                text_blocks,
                figure_offset,
                used_figure_numbers,
            )

        # 中文注释：先在这一页上把公式找出来。找出来的每一处都带着自己的位置，
        # 截图转写那一步会用到。这一步只做几何计算，不调模型、很快。
        page_regions = _collect_formula_regions(page, blocks, table_rects, page_number)

        # 中文注释：单独占一行的公式不直接写进正文，而是留一个占位符，等转写完再换。
        # 这里先记下"哪一块文字的第几行属于哪个公式"，待会儿按行号去查。
        # 夹在句子里的公式不进这张表，它们照旧留在正文段落里。
        display_region_of_line: dict[tuple[int, int], PdfFormulaRegion] = {}
        display_rects: list[tuple[float, float, float, float]] = []
        for region in page_regions:
            if not region.is_display:
                continue
            display_rects.append(region.rect)
            for member in region.members:
                display_region_of_line[member] = region

        items: list[_PageItem] = []
        first_item_of_block: dict[int, _PageItem] = {}
        emitted_tables: set[int] = set()
        emitted_regions: set[int] = set()

        def flush_plain(lines: list[str], y: float, block: Any) -> None:
            """把一个文字块里攒下的普通正文行合成一个段落。

            中文注释：一个文字块（get_text("dict") 里的 block）本来就是论文的一个段落，
            里面那些行只是"排版换行"，不是真的段落。以前每一行都单独输出成一个段落，
            结果是 Markdown 里每一行之间都空一行，一段话被拆成十几段。所以这里要把
            同一个块里的行拼回一段（用单个换行连起来，不把换行去掉——那是原文的断行）。
            单独一行、又像章节标题的，标成标题，分片时好从这里另起一段。
            """

            text = "\n".join(lines).strip()
            if not text:
                return
            kind = "heading" if _looks_like_heading(text) else "paragraph"
            item = _PageItem(text, y, kind=kind, rect=_rect_tuple(block["bbox"]))
            items.append(item)
            # 中文注释：记住每个文字块产出的第一项。图片引用要插在图注前面，
            # 所以需要拿到"图注那个块的第一个项"作为插入位置。
            first_item_of_block.setdefault(id(block), item)

        for block_index, block in enumerate(blocks):
            block_top = float(block["bbox"][1])
            table_index = _covered_by_table(_rect_tuple(block["bbox"]), table_rects)
            if table_index is not None:
                # 中文注释：表格里的文字已经被拼进 Markdown 表格了。这些原始文字块要是
                # 再输出一遍，表格内容就会在正文里出现两次，所以整块跳过。
                region = region_of_table.get(table_index)
                if region is not None:
                    # 中文注释：这张表要交给模型重排表头，先在原位留个记号，
                    # 等转写完了再换成真正的 Markdown 表。
                    if id(region) not in emitted_regions:
                        emitted_regions.add(id(region))
                        items.append(
                            _PageItem(region.placeholder, region.rect[1], kind="table", rect=region.rect)
                        )
                    continue
                if table_index not in emitted_tables:
                    emitted_tables.add(table_index)
                    items.append(
                        _PageItem(
                            table_markdowns[table_index],
                            float(table_rects[table_index][1]),
                            kind="table",
                            rect=table_rects[table_index],
                        )
                    )
                continue
            plain_lines: list[str] = []
            for line_index, line in enumerate(block["lines"]):
                line_text = "".join(span["text"] for span in line["spans"])
                if not line_text.strip():
                    continue
                line_rect = _rect_tuple(line["bbox"])
                if _inside_any(line_rect, figure_internal_rects):
                    # 中文注释：这行是插图肚子里的字——坐标轴刻度、图例、框里的说明。
                    # 图本身已经整块截出来交给模型看了，这些散落的词再当正文输出一遍，
                    # 只会让模型读到一堆没头没尾的名词。实测一篇论文的方法框架图被这样
                    # 拆成"Observation / Privileged Critic / Local Occupancy"散在正文里。
                    continue
                region = display_region_of_line.get((block_index, line_index))
                if region is not None:
                    # 中文注释：这行属于某个单独占一行的公式。先把攒着的正文落定，
                    # 再放下占位符。一条公式可能横跨好几行，只在第一行时放一次。
                    flush_plain(plain_lines, block_top, block)
                    plain_lines = []
                    if id(region) not in emitted_regions:
                        emitted_regions.add(id(region))
                        items.append(
                            _PageItem(region.placeholder, region.rect[1], kind="display_formula", rect=region.rect)
                        )
                    continue
                if _center_inside_any(line_rect, display_rects):
                    # 中文注释：这几个字落在某条独立公式的框里，但没被记成公式行。
                    # 多半是公式左边的函数名。框已经把它们截进去了，再写成正文里的 $...$
                    # 就会和公式各写一遍，分片时也被切碎。
                    flush_plain(plain_lines, block_top, block)
                    plain_lines = []
                    continue
                if _is_math_line(line):
                    # 中文注释：整行都是公式、但又和旁边的句子挤在一起，不当独立公式截图。
                    # 单独成一块，分片时不会从它中间切开。
                    flush_plain(plain_lines, block_top, block)
                    plain_lines = []
                    wrapped = _with_inline_math(line).strip()
                    if wrapped:
                        items.append(_PageItem(wrapped, line_rect[1], kind="inline_formula", rect=line_rect))
                    continue
                # 中文注释：其余的行都留在段落里。更小、更靠下的收成下标，更靠上的收成上标。
                plain_lines.append(_with_inline_math(line))
            flush_plain(plain_lines, block_top, block)

        figures = _fold_table_figures_into_regions(figures, page_tables, page_number)

        for figure in figures:
            caption_block = figure["caption_block"]
            anchor = first_item_of_block.get(id(caption_block)) if caption_block is not None else None
            # 中文注释：图注写着 TABLE I 的，折进表格之后正文里放的是表格记号。
            # 这一块按表装箱，不再进插图清单，也不再把格子里的数字竖着排一遍。
            is_table = str(figure["markdown"]).startswith("<!-- table:")
            file_match = re.search(r"assets/([^)]+)", str(figure["markdown"]))
            figure_item = _PageItem(
                figure["markdown"],
                figure["y"],
                kind="table" if is_table else "figure",
                rect=figure["rect"],
                asset_name="" if is_table else (file_match.group(1) if file_match else ""),
            )
            if anchor is None:
                _insert_by_position(items, figure_item)
            else:
                index = next((i for i, item in enumerate(items) if item is anchor), len(items))
                items.insert(index, figure_item)

        # 中文注释：这里要报"真正输出进正文的表格数"，而不是"检测到几个表格"。
        # find_tables 报的数量会虚高，有两种情况：一是把一张表里套着的小表也单独找出来
        # （实测一篇论文的第一页报了 3 张，其实那 3 个矩形是层层嵌套的同一个区域）；
        # 二是把图的标注区当成表格（_looks_like_table 会把它筛掉）。所以按检测数报不准。
        # 中文注释：下标经常被排成单独一行，和上一行叠在一起。
        # 不接回去的话，正文里会出现 "$m,in$" 这种单独一小段。
        items = _join_same_line_items(items)
        page_blocks = [
            PageBlock(
                kind=item.kind,
                page_number=page_number,
                text=item.text,
                rect=item.rect,
                asset_name=item.asset_name,
            )
            for item in items
            if item.text.strip()
        ]
        return page_blocks, len(emitted_tables), figure_count, page_regions, page_tables


def collect_pdf_figures(source_path: Path, assets_dir: Path) -> list[dict[str, Any]]:
    """只把插图截下来并配上图注。正文不从这里来。

    中文注释：正文已经改由 Nougat 按页写。插图仍用原来的办法：
    在页面上找图、配最近的图注、截成文件。表注（TABLE I 这种）不是插图，这里跳过。
    """

    try:
        import pymupdf
    except Exception:
        return []
    figures_out: list[dict[str, Any]] = []
    try:
        document = pymupdf.open(str(source_path))
    except Exception:
        return []
    written_xrefs: set[int] = set()
    written_digests: dict[str, str] = {}
    figure_counter = 0
    used_figure_numbers: set[int] = set()
    try:
        for page_index in range(document.page_count):
            page = document[page_index]
            page_number = page_index + 1
            table_rects, _markdowns = _collect_tables(page)
            text_blocks: list[tuple[Any, str]] = []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                if _covered_by_table(_rect_tuple(block["bbox"]), table_rects) is not None:
                    continue
                block_text = _block_text(block)
                if block_text.strip():
                    text_blocks.append((block, block_text))
            figures, _internal, count = _collect_page_figures(
                document,
                page,
                page_number,
                assets_dir,
                written_xrefs,
                written_digests,
                table_rects,
                text_blocks,
                figure_counter,
                used_figure_numbers,
            )
            figure_counter += count
            for figure in figures:
                caption = str(figure.get("caption_text") or "")
                if _is_table_caption(caption):
                    continue
                file_match = re.search(r"assets/([^)]+)", str(figure.get("markdown") or ""))
                figures_out.append(
                    {
                        "page_number": page_number,
                        "markdown": str(figure["markdown"]),
                        "asset_name": file_match.group(1) if file_match else "",
                        "caption": caption,
                    }
                )
    finally:
        document.close()
    return figures_out


def get_pdf_parser(name: str = "pypdf") -> BasePdfParser:
    """按名字返回 PDF 解析器。

    中文注释：auto 的意思是"能用 PyMuPDF 就用 PyMuPDF，装不上才退回 pypdf"。
    PyMuPDF 能多拿到表格、公式和图片，效果更好；但它是可选依赖，所以一定要留退路。
    """

    if name == "pypdf":
        return PypdfParser()
    if name == "pymupdf":
        return PyMuPdfParser()
    if name == "auto":
        return PyMuPdfParser() if _pymupdf_available() else PypdfParser()
    raise ValueError(f"未知 PDF 解析器：{name}")


def _pymupdf_available() -> bool:
    """检查 PyMuPDF 是否装好、能不能导入。"""

    try:
        import pymupdf  # noqa: F401
    except Exception:
        return False
    return True


class _PageItem:
    """正文里的一小段内容（一行文字、一个表格、一张图片引用）。

    中文注释：故意不写成 dataclass。默认的"相等"判断是"是不是同一个对象"，
    下面要靠这个来定位图注该插在哪，按内容比较会认错人。
    """

    __slots__ = ("text", "y", "kind", "rect", "asset_name")

    def __init__(
        self,
        text: str,
        y: float,
        kind: str = "paragraph",
        rect: tuple[float, float, float, float] | None = None,
        asset_name: str = "",
    ) -> None:
        self.text = text
        self.y = y
        self.kind = kind
        self.rect = rect if rect is not None else (0.0, y, 0.0, y)
        self.asset_name = asset_name


def _collect_tables(page: Any) -> tuple[list[tuple[float, float, float, float]], list[str]]:
    """找出页面上的表格，并转成 Markdown 表格。

    中文注释：find_tables 返回的每个表格有坐标(bbox)和二维内容。坐标必须留着，
    因为表格区域里的原始文字块要跳过，否则表格内容会被输出两遍。

    这里还要把"不像表格的表格"筛掉，原因是 find_tables 在没有框线的 PDF 上很容易把
    "图的标注区"当成表格，把本来读得通的一段文字拆成一堆错乱的格子。实测三篇样本里
    已经命中一篇：整页图的标注被拆成 4 列，表头变成 240 多个字符的一长串拼凑文字。
    这种"假表格"比没有表格更糟——下游会把它当成真的对比数据表读。筛不掉的宁可退回
    纯文本（就是改造前的样子），也不能把语义搅乱。
    """

    try:
        finder = page.find_tables()
    except Exception as exc:
        logger.warning("表格识别失败，跳过本页表格", extra={"reason": str(exc)})
        return [], []
    image_rects = _page_image_rects(page)
    rects: list[tuple[float, float, float, float]] = []
    markdowns: list[str] = []
    for table in finder.tables:
        try:
            data = table.extract()
        except Exception:
            continue
        table_rect = _rect_tuple(table.bbox)
        if not _looks_like_table(data, table_rect, image_rects):
            continue
        markdown = _table_to_markdown(data)
        if not markdown:
            continue
        rects.append(table_rect)
        markdowns.append(markdown)
    return rects, markdowns


def _collect_table_regions(
    table_rects: list[tuple[float, float, float, float]],
    table_markdowns: list[str],
    text_blocks: list[tuple[Any, str]],
    page_number: int,
) -> list[PdfTableRegion]:
    """把"有图注的表格"挑出来，准备交给模型重排。

    中文注释：只挑有图注的。图注（"Table 1: ..."）是"这确实是一张表、而且我们知道它是第几张"
    的确凿信号；没有图注的表格区照样走原来的 Markdown 转换，不惊动模型。

    一张大表常被 find_tables 拆成好几块，所以按图注分组：归同一条表注的几块合并成一张表，
    整块截给模型看，它才能把表头和列正确对齐。
    """

    if not table_rects:
        return []
    captions = [entry for entry in _caption_candidates(text_blocks) if _is_table_caption(entry[1])]
    if not captions:
        return []

    grouped: dict[int, list[int]] = {}
    for table_index, rect in enumerate(table_rects):
        caption_index = _nearest_caption_index(rect, captions)
        if caption_index is None:
            continue
        grouped.setdefault(caption_index, []).append(table_index)

    regions: list[PdfTableRegion] = []
    for caption_index in sorted(grouped):
        members = grouped[caption_index]
        rect = table_rects[members[0]]
        for member in members[1:]:
            rect = _union_rect(rect, table_rects[member])
        # 中文注释：把表注也框进来。模型看到"Table 1: ..."就知道这是第几张表、在讲什么，
        # 排表头时心里有数。
        rect = _union_rect(rect, captions[caption_index][2])
        regions.append(
            PdfTableRegion(
                page_number=page_number,
                index=len(regions) + 1,
                rect=rect,
                caption=captions[caption_index][1].strip(),
                fallback="\n\n".join(table_markdowns[member] for member in members),
                members=members,
            )
        )
    return regions


def _is_table_caption(text: str) -> bool:
    """判断一段文字是不是"表注"（以 Table 加编号开头，而且后面还有像样的内容）。"""

    match = _TABLE_CAPTION_PATTERN.match(text.strip())
    if not match:
        return False
    return len(text[match.end():].strip(" :：.、")) >= _MIN_CAPTION_BODY_CHARS


def _fold_table_figures_into_regions(
    figures: list[dict[str, Any]], page_tables: list[PdfTableRegion], page_number: int
) -> list[dict[str, Any]]:
    """把"其实是表格"的插图交给表格那条线去处理。

    中文注释：为什么会有"其实是表格的插图"——find_tables 只认得出有框线的规则表格。
    遇到没框线、或者排版特殊的表，它一张都找不到，那些表就会被当成普通插图收进来。
    实测一篇论文的 Table 5 就是这样：find_tables 报 0 张，整张表被当位图收成了左右两半。

    所以这里做一次归拢：图注写着 "Table N" 的，本来就不是插图。给它配一条表格区域
    （同一页已经有同一个表号的区域就并进去，别重复），正文里改成放表格占位符。
    表里那些数值最后会由重排那一步变成表头正确的 Markdown 表。
    """

    kept: list[dict[str, Any]] = []
    for figure in figures:
        caption = str(figure.get("caption_text") or "")
        if not _is_table_caption(caption):
            kept.append(figure)
            continue
        rect = figure["rect"]
        region = _region_with_same_table_number(page_tables, caption)
        if region is None:
            region = PdfTableRegion(
                page_number=page_number,
                index=len(page_tables) + 1,
                rect=rect,
                caption=caption,
                # 中文注释：重排不出来时退回图片引用。图本身还在 assets 里，
                # 用户和后续的插图解读照样看得到，只是没有重排好的表头。
                fallback=str(figure["markdown"]),
            )
            page_tables.append(region)
        else:
            region.rect = _union_rect(region.rect, rect)
        figure["markdown"] = region.placeholder
        kept.append(figure)
    return kept


def _region_with_same_table_number(
    regions: list[PdfTableRegion], caption: str
) -> PdfTableRegion | None:
    """找同一页里表号相同的表格区域。

    中文注释：靠表号认，不靠位置。实测有一页的表格被 find_tables 认成了 31 点高的一条
    表头碎片，而真正的表格图在页面下方老远的地方——按位置根本对不上，按"都是 Table 13"
    才对得上。
    """

    number = _table_number(caption)
    if not number:
        return None
    for region in regions:
        if _table_number(region.caption) == number:
            return region
    return None


def _table_number(caption: str) -> str:
    """从表注里取出表号（"Table 13: ..." 里的 13）。取不到返回空字符串。"""

    match = _TABLE_CAPTION_PATTERN.match(caption.strip())
    return match.group(1) if match else ""


def _page_image_rects(page: Any) -> list[tuple[float, float, float, float]]:
    """把这一页上所有图片的矩形位置收出来，用来判断某个矩形是不是"图的地盘"。"""

    rects: list[tuple[float, float, float, float]] = []
    try:
        images = page.get_images(full=True)
    except Exception:
        return rects
    for image in images:
        try:
            for rect in page.get_image_rects(image[0]):
                rects.append(_rect_tuple(rect))
        except Exception:
            continue
    return rects


def _looks_like_table(
    data: Any,
    table_rect: tuple[float, float, float, float],
    image_rects: list[tuple[float, float, float, float]],
) -> bool:
    """判断 find_tables 找出来的这个东西到底像不像一张真表格。

    中文注释：三条很保守的检查，任一条不过就当它不是表格，退回纯文本：
    1. 至少两行、至少两个有内容的格子 —— 一行一格的"表格"没有表头可言；
    2. 第一行（表头）最长的格子不能太长 —— 真表头都是"Agents""Metrics"这种短词，
       长到 200 字符以上说明整段正文被硬塞进了一个格子；
    3. 这个矩形不能被图片占去一大半 —— 那多半是图的标注区，不是表格。
    """

    rows = [row for row in (data or []) if row is not None]
    if len(rows) < _MIN_TABLE_ROWS:
        return False
    non_empty_cells = sum(1 for row in rows for cell in row if str(cell or "").strip())
    if non_empty_cells < _MIN_TABLE_CELLS:
        return False
    first_row = [str(cell or "").strip() for cell in rows[0]]
    if first_row and max(len(cell) for cell in first_row) > _MAX_TABLE_HEADER_CHARS:
        return False
    if image_rects and _rect_coverage(table_rect, image_rects) > _MAX_TABLE_IMAGE_COVERAGE:
        return False
    return True


def _rect_coverage(
    target: tuple[float, float, float, float],
    covers: list[tuple[float, float, float, float]],
) -> float:
    """算出目标矩形有多大比例被另一组矩形盖住（返回值 0~1）。"""

    width = target[2] - target[0]
    height = target[3] - target[1]
    area = width * height
    if area <= 0:
        return 0.0
    covered = 0.0
    for rect in covers:
        overlap_width = min(target[2], rect[2]) - max(target[0], rect[0])
        overlap_height = min(target[3], rect[3]) - max(target[1], rect[1])
        if overlap_width > 0 and overlap_height > 0:
            covered += overlap_width * overlap_height
    return min(covered / area, 1.0)


def _table_to_markdown(data: Any) -> str:
    """把表格的二维内容拼成 GitHub 风格 Markdown 表格。

    中文注释：第一行当表头，第二行是 |---| 分隔行，剩下的是数据行。单元格个数
    不齐时按最宽的一行补齐，免得 Markdown 表格错位。
    """

    rows = [[_clean_table_cell(cell) for cell in row] for row in (data or []) if row is not None]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    if width == 0:
        return ""
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _clean_table_cell(value: Any) -> str:
    """整理表格单元格里的文字。

    中文注释：单元格里可能塞了多行、还可能自带竖线。竖线不转义的话，Markdown 会
    把它当成列分隔符，整张表就错位了。
    """

    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text.replace("|", "\\|")


@dataclass(slots=True)
class _FigureCandidate:
    """还没落盘的一张候选插图。

    中文注释：先只记位置和内容、不写文件，是因为"一张图被切成好几块"的情况要等
    配完图注才知道该不该合并。合并之后要用整块的画面重新截一次，先前那些碎片的
    图片内容就作废了，写下去也是白写。
    """

    rect: tuple[float, float, float, float]
    payload: bytes | None  # 位图的原始字节；矢量图是现场截的，这里是空的
    extension: str
    is_vector: bool


def _collect_page_figures(
    doc: Any,
    page: Any,
    page_number: int,
    assets_dir: Path,
    written_xrefs: set[int],
    written_digests: dict[str, str],
    table_rects: list[tuple[float, float, float, float]],
    text_blocks: list[tuple[Any, str]],
    figure_offset: int,
    used_figure_numbers: set[int],
) -> tuple[list[dict[str, Any]], list[tuple[float, float, float, float]], int]:
    """把一页上的插图收齐、配好图注、落盘，返回 (图列表, 要抹掉的内部文字范围, 图片数)。

    中文注释：一段话讲清为什么这么绕——

    位图（照片、截图、导出的 PNG）用 get_images 就能拿到。但论文里大量插图是"矢量画"出来的：
    用线条、色块、箭头直接在 PDF 里画。机器人、强化学习这类论文的方法框架图、折线图
    基本都是这么画的。这类图 get_images 完全看不见——实测一篇论文第 4 页有 1763 条矢量线条
    （占页面 61%），一张图都没抽出来，正文里只剩图里散落的几个标签词。

    收完还要解决第二件事：一张多面板的图（左右三个子图那种）在 PDF 里是好几块独立内容，
    会被当成好几张图，论文里明明只有 11 张图，正文里却冒出 64 条引用。办法是看它们
    "归属同一条图注"，是的话就合并成一张、按整块重新截一次。
    """

    candidates = [
        candidate
        for candidate in _figure_candidates(doc, page, written_xrefs, table_rects)
        # 中文注释：落在表格里的东西不算插图——那张表交给表格那边单独处理。
        # 不筛的话同一张表会被讲两遍：一遍是重排好的 Markdown 表，一遍是"这是一张表格"的图片描述。
        if not _overlaps_any(candidate.rect, table_rects, _TABLE_REGION_OVERLAP_LIMIT)
    ]
    if not candidates:
        return [], [], 0
    assets_dir.mkdir(parents=True, exist_ok=True)

    captions = _caption_candidates(text_blocks)
    # 中文注释：先给每张候选图各找一个"离我最近的图注"，这里故意不排他——
    # 一张多面板图的几块碎片本来就该配到同一条图注，排他的话只有一块配得上、
    # 其余几块就变成"没有图注的图"，正文里还是一堆引用。
    captions_of: dict[int, int | None] = {
        index: _nearest_caption_index(candidate.rect, captions)
        for index, candidate in enumerate(candidates)
    }

    captions_used: set[int] = set()
    figures: list[dict[str, Any]] = []
    internal_rects: list[tuple[float, float, float, float]] = []
    emitted_captions: set[int] = set()
    order = 0

    # 中文注释：按从上到下的顺序处理。这里排的是"下标"而不是候选图本身——
    # 候选图是普通数据对象，两张位置用内容比较会相等，拿对象去查位置会认错人。
    for original_index in sorted(
        range(len(candidates)), key=lambda index: (candidates[index].rect[1], candidates[index].rect[0])
    ):
        candidate = candidates[original_index]
        caption_index = captions_of.get(original_index)
        if caption_index is not None and caption_index in emitted_captions:
            # 这条图注已经配给同一张图的另一块了，这块是它的兄弟面板，跳过。
            continue
        if caption_index is not None:
            emitted_captions.add(caption_index)
            captions_used.add(caption_index)
            panel_rects = _panel_rects(candidates, captions_of, caption_index)
        else:
            panel_rects = [candidate.rect]

        if len(panel_rects) > 1:
            # 中文注释：多块面板合成一张图，按整块范围重新截一次，这样截出来的是完整的图，
            # 而不是几块被切开的碎片。
            rect = panel_rects[0]
            for panel in panel_rects[1:]:
                rect = _union_rect(rect, panel)
            try:
                payload = page.get_pixmap(clip=_clip_rect(rect), dpi=_capped_figure_dpi(rect)).tobytes("png")
            except Exception:
                continue
            extension, is_vector = "png", False
        else:
            rect = panel_rects[0]
            is_vector = candidate.is_vector
            extension = candidate.extension
            payload = candidate.payload
            if is_vector or payload is None:
                try:
                    payload = page.get_pixmap(clip=_clip_rect(rect), dpi=_capped_figure_dpi(rect)).tobytes("png")
                except Exception:
                    continue
                extension = "png"

        file_name = _store_figure(assets_dir, written_digests, payload, extension, page_number, order)
        caption_text = captions[caption_index][1] if caption_index is not None else ""
        figure_number = _figure_number(caption_text, figure_offset + order + 1, used_figure_numbers)
        used_figure_numbers.add(figure_number)
        if is_vector and caption_text:
            # 中文注释：矢量图内部的文字（坐标轴刻度、图例、框里的说明）也会被当成正文抽出来，
            # 结果正文里散着一堆"Observation""Privileged Critic"这种没头没尾的词。配到图注的
            # 矢量图说明它确实是张插图，那它肚子里的文字就不该再当正文输出。
            internal_rects.append(rect)
        figures.append(
            {
                "caption_block": captions[caption_index][0] if caption_index is not None else None,
                "caption_text": caption_text,
                "rect": rect,
                "y": rect[1],
                "markdown": f"![{_image_alt_text(caption_text, figure_number)}](assets/{file_name})",
            }
        )
        order += 1

    return figures, internal_rects, order


def _panel_rects(
    candidates: list[_FigureCandidate], captions_of: dict[int, int | None], caption_index: int
) -> list[tuple[float, float, float, float]]:
    """找出所有归属同一条图注的碎片。

    中文注释：一条图注对应论文里的一张图，所以"归属同一条图注"就说明这几块碎片
    本来就是同一张图的几个面板（左边一个、中间一个、右边一个那种）。图注是论文自己写的、
    一条只标一张图，用它来分组很可靠。
    """

    return [
        candidate.rect
        for index, candidate in enumerate(candidates)
        if captions_of.get(index) == caption_index
    ]


def _nearest_caption_index(
    rect: tuple[float, float, float, float],
    captions: list[tuple[Any, str, tuple[float, float, float, float]]],
) -> int | None:
    """找离这张图最近的一条图注，返回它在列表里的位置。"""

    best_index: int | None = None
    best_distance: float | None = None
    for index, (_, _, caption_rect) in enumerate(captions):
        distance = _vertical_distance(rect, caption_rect)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def _figure_candidates(
    doc: Any,
    page: Any,
    written_xrefs: set[int],
    table_rects: list[tuple[float, float, float, float]],
) -> list[_FigureCandidate]:
    """把一页上的插图候选收下来，但先不写文件。"""

    candidates: list[_FigureCandidate] = []

    # ---- 位图 ----
    for image in page.get_images(full=True):
        xref = image[0]
        if xref in written_xrefs:
            continue
        try:
            info = doc.extract_image(xref)
        except Exception:
            continue
        payload = info.get("image") or b""
        if len(payload) < _MIN_IMAGE_BYTES:
            # 中文注释：小图基本是 logo、图标、装饰线。实测一篇论文 56 个图片里
            # 只有 18 个达到 10KB，剩下全是这种装饰品。
            continue
        rects = page.get_image_rects(xref)
        if not rects:
            # 图片对象挂在页面上但实际没显示出来，没有位置就没法找图注。
            continue
        written_xrefs.add(xref)
        candidates.append(
            _FigureCandidate(
                rect=_rect_tuple(rects[0]), payload=payload, extension=info.get("ext") or "png", is_vector=False
            )
        )

    # ---- 矢量图 ----
    occupied = [candidate.rect for candidate in candidates] + list(table_rects)
    for rect in _vector_figure_rects(page, occupied):
        candidates.append(_FigureCandidate(rect=rect, payload=None, extension="png", is_vector=True))

    return candidates


def _store_figure(
    assets_dir: Path,
    written_digests: dict[str, str],
    payload: bytes,
    extension: str,
    page_number: int,
    index: int,
) -> str:
    """把一张图写进 assets 目录，返回它的文件名。

    中文注释：按图片"内容"去重，而不是按 PDF 里的对象编号。同一张图在论文里可能被
    引用好几次、每次都是一个新的对象编号，按编号去重挡不住——实测一篇论文的 64 个
    图片文件里只有 50 个内容互不相同，最大一组有 5 个文件字节完全一样。
    内容一样的就直接指回已经写好的那一份，不再重复落盘。
    """

    digest = hashlib.md5(payload).hexdigest()
    existing = written_digests.get(digest)
    if existing is not None:
        return existing
    file_name = f"fig_p{page_number}_{index + 1}.{extension}"
    (assets_dir / file_name).write_bytes(payload)
    written_digests[digest] = file_name
    return file_name


def _vector_figure_rects(
    page: Any, occupied: list[tuple[float, float, float, float]]
) -> list[tuple[float, float, float, float]]:
    """把页面上的矢量线条聚成一块块，挑出真正像插图的那些。

    中文注释：页面上的矢量线条大部分不是插图——表格框线、页眉分隔线、下划线、
    页脚横杠都算。所以不能"有一段线条就当成图"，得看聚出来的那一块够不够"图的样子"：
    够大、够高、里面线条够多。
    """

    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    rects: list[tuple[float, float, float, float]] = []
    for drawing in drawings:
        raw = drawing.get("rect")
        if raw is None:
            continue
        rects.append(_rect_tuple(raw))
    if not rects:
        return []

    page_area = float(page.rect.width) * float(page.rect.height)
    # 中文注释：先把挨在一起的线条按位置并成块。这里用的是和公式区域一样的做法，
    # 只是不用管横向间隙——插图里的线条本来就连成一片。
    groups: list[list[tuple[float, float, float, float]]] = []
    for rect in sorted(rects, key=lambda item: (item[1], item[0])):
        for group in groups:
            if _rects_are_close(_group_box(group), rect):
                group.append(rect)
                break
        else:
            groups.append([rect])

    accepted: list[tuple[float, float, float, float]] = []
    for group in groups:
        box = _group_box(group)
        width = box[2] - box[0]
        height = box[3] - box[1]
        if height < _MIN_VECTOR_FIGURE_HEIGHT or width < _MIN_VECTOR_FIGURE_WIDTH:
            # 太矮或太窄，是分隔线、下划线这类东西。
            continue
        if len(group) < _MIN_VECTOR_FIGURE_STROKES:
            # 线条太少，多半是个表格框或装饰矩形。
            continue
        area_ratio = (width * height) / page_area if page_area else 0.0
        if area_ratio < _MIN_VECTOR_FIGURE_AREA_RATIO or area_ratio > _MAX_VECTOR_FIGURE_AREA_RATIO:
            continue
        if _overlaps_any(box, occupied, _VECTOR_FIGURE_OVERLAP_LIMIT):
            # 这块地方已经有位图或表格了，别重复截一遍。
            continue
        accepted.append(box)
    return accepted


def _inside_any(
    rect: tuple[float, float, float, float], containers: list[tuple[float, float, float, float]]
) -> bool:
    """这个矩形是不是落在其中某个框里面。

    中文注释：用矩形中心点判断，不用"完全包含"。插图的文字有时会稍稍超出线条围出来的
    范围（比如轴标签挂在框外一点点），按完全包含判会漏掉。中心点在框里就算在里面。
    """

    center_x = (rect[0] + rect[2]) / 2
    center_y = (rect[1] + rect[3]) / 2
    for box in containers:
        if box[0] <= center_x <= box[2] and box[1] <= center_y <= box[3]:
            return True
    return False


def _group_box(group: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    """一堆矩形的整体外框。"""

    box = group[0]
    for rect in group[1:]:
        box = _union_rect(box, rect)
    return box


def _overlaps_any(
    rect: tuple[float, float, float, float],
    others: list[tuple[float, float, float, float]],
    limit: float,
) -> bool:
    """这个矩形和已有的那些框，重合面积有没有超过限制。"""

    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    if area <= 0:
        return False
    for other in others:
        width = min(rect[2], other[2]) - max(rect[0], other[0])
        height = min(rect[3], other[3]) - max(rect[1], other[1])
        if width <= 0 or height <= 0:
            continue
        if width * height / area >= limit:
            return True
    return False


def _clip_rect(rect: tuple[float, float, float, float]) -> Any:
    """把四元组包成 PyMuPDF 用的矩形。"""

    import pymupdf

    return pymupdf.Rect(*rect)


def _capped_figure_dpi(rect: tuple[float, float, float, float]) -> int:
    """矢量图截图用多少清晰度，太宽就降一点。"""

    width_points = rect[2] - rect[0]
    if width_points <= 0:
        return _VECTOR_FIGURE_DPI
    if width_points * _VECTOR_FIGURE_DPI / 72 <= _MAX_FIGURE_PX_WIDTH:
        return _VECTOR_FIGURE_DPI
    return max(72, int(_MAX_FIGURE_PX_WIDTH * 72 / width_points))


def _image_alt_text(caption_text: str, figure_number: int) -> str:
    """图片引用里的说明文字。

    中文注释：优先用论文自己写的图注原文，和 HTML 分支的做法保持一致
    （见 read_fulltext.py 的 _figure_markdown）。这样模型读到图片引用时，
    紧接着就知道这张图画的是什么、是第几张图——而不是只看到一个"AFigure 3"。

    图注原文里不能出现的字符要处理掉：换行会把引用折成两行、"]"会提前把引用闭合。
    """

    text = re.sub(r"\s+", " ", caption_text or "").strip().replace("]", "\\]")
    return text or f"Figure {figure_number}"


def _caption_candidates(text_blocks: list[tuple[Any, str]]) -> list[tuple[Any, str, tuple[float, float, float, float]]]:
    """挑出可能是图注的文字块。

    中文注释：只认"以 Figure / Fig. / TABLE / Table 加编号开头"的文字块，
    免得把正文里随便一句话当成图注。
    """

    candidates: list[tuple[Any, str, tuple[float, float, float, float]]] = []
    for block, block_text in text_blocks:
        text = block_text.strip()
        if _CAPTION_PATTERN.match(text):
            candidates.append((block, text, _rect_tuple(block["bbox"])))
    return candidates


def _vertical_distance(
    image_rect: tuple[float, float, float, float],
    rect: tuple[float, float, float, float],
) -> float:
    """两个矩形之间的纵向间距；上下有重叠时算 0（说明基本贴在一起）。"""

    if rect[1] >= image_rect[3]:
        return rect[1] - image_rect[3]
    if rect[3] <= image_rect[1]:
        return image_rect[1] - rect[3]
    return 0.0


def _figure_number(caption_text: str, fallback: int, used: set[int]) -> int:
    """给图片定一个编号。

    中文注释：图注里一般写明了编号（"Fig. 1: ..."），优先用它，这样正文里的图号和
    读者在论文里看到的图号能对上。但要注意这只是"尽量对上"，不保证一定和论文印的图号一致：
    配不到图注的图、或者图注上的编号已经被别的图用掉了，都用递增的兜底编号顶上。
    唯一硬保证是"全篇不重复"——不然正文里出现两张 "Figure 11"，精读时根本分不清指哪张。
    """

    if caption_text:
        match = _CAPTION_NUMBER.match(caption_text)
        if match and match.group(1).isdigit():
            number = int(match.group(1))
            if number not in used:
                return number
    number = fallback
    while number in used:
        number += 1
    return number


def _join_same_line_items(items: list[_PageItem]) -> list[_PageItem]:
    """把上下叠在一起的两段普通文字接成一段。

    中文注释：PDF 常把下标单独排一行，这一行又和上一行叠着。
    看起来是同一句话，抽出来却变成下一段，开头只剩 "$m,in$"。
    下一行正文是另起一行的，上下不重叠，这里不会去接。
    """

    if not items:
        return items
    joined = [items[0]]
    for item in items[1:]:
        previous = joined[-1]
        if previous.kind != "paragraph" or item.kind != "paragraph":
            joined.append(item)
            continue
        # 中文注释：上一段可能有好几行，下标只和最后一行叠在一起。
        # 所以只拿上一段最底下那一截来比，不能拿整段的高度。
        tail_top = max(previous.rect[1], previous.rect[3] - 14.0)
        overlap = min(previous.rect[3], item.rect[3]) - max(tail_top, item.rect[1])
        shorter = min(previous.rect[3] - tail_top, item.rect[3] - item.rect[1])
        if shorter <= 0 or overlap < shorter * 0.45:
            joined.append(item)
            continue
        # 中文注释：下一行开头是一小截下标或上标，就接进上一截公式，
        # 不再单独留下 "$_{m,in}$" 这种碎片。
        attached = _attach_leading_script(previous.text, item.text)
        if attached is not None:
            previous.text = attached
        else:
            previous.text = previous.text.rstrip() + " " + item.text.lstrip()
        previous.rect = (
            min(previous.rect[0], item.rect[0]),
            min(previous.rect[1], item.rect[1]),
            max(previous.rect[2], item.rect[2]),
            max(previous.rect[3], item.rect[3]),
        )
    return joined


def _insert_by_position(items: list[_PageItem], item: _PageItem) -> None:
    """图注找不到时，按纵向位置把图片引用插进正文。

    中文注释：只比上下、不比左右。双栏论文里左右栏的坐标根本没法统一排序，只按
    纵向位置插至少不会把图塞到页尾去。
    """

    for index, existing in enumerate(items):
        if existing.y > item.y:
            items.insert(index, item)
            return
    items.append(item)


def _block_text(block: Any) -> str:
    """把一个文字块里的所有行拼成一段文字。"""

    return "\n".join("".join(span["text"] for span in line["spans"]) for line in block.get("lines", []))


def _is_math_character(character: str) -> bool:
    """判断一个字符是不是"一眼就能看出是数学"的字符。

    中文注释：现代论文（TeX Gyre、Cambria 这类字体排的）里面，公式字母用的就是
    专门的数学字符，比如 𝑆、𝛼、𝑊、𝑙。这类字符有固定的编码区间，直接按编码判断即可，
    完全不用管它用的什么字体——这正是"字体白名单永远追不上新字体"的解药。
    """

    if character in _MATH_SYMBOL_CHARS:
        return True
    code = ord(character)
    return any(start <= code <= end for start, end in _MATH_UNICODE_RANGES)


def _is_math_line(line: Any) -> bool:
    """判断一整行是不是公式。

    中文注释：两条判据任意一条成立就算公式。

    第一条看字符本身——这一行里"长得像数学符号"的字符有没有超过一半。这是主力判据，
    靠它能认出用了新字体的论文（那些论文的公式字符本身就是 𝑆、𝛼 这种数学字符）。

    第二条看字体——这一行里"数学字体"的字符有没有超过一半。这是补漏用的：有些论文
    的公式里全是普通英文字母（比如 MOM nM =），字符本身看不出数学味，但用的是 CMMI 字体。

    两条都按"非空白字符的占比"算，不是"只要含有就算"——因为 = + - ( ) 这些符号在
    正文里到处都是，按"含有"判断会把整段普通句子都认成公式。

    最后还要求整行至少有两个非空白字符，否则页码、孤立的单个斜体字母都会被误抓。
    """

    total = 0
    math_characters = 0
    for span in line["spans"]:
        font_is_math = _is_math_font(span["font"])
        for character in span["text"]:
            if character.isspace():
                continue
            total += 1
            if font_is_math or _is_math_character(character):
                math_characters += 1
    if total < _MIN_MATH_LINE_CHARS:
        return False
    return math_characters / total >= _MATH_CHARACTER_RATIO


# 中文注释：PDF 里这些符号抽出来是单个字符。放进 $ 里时换成 LaTeX 写法，后面才读得懂。
_LATEX_SYMBOLS = {
    "∈": r"\in ",
    "×": r"\times ",
    "≤": r"\le ",
    "≥": r"\ge ",
    "−": "-",
    "–": "-",
    "·": r"\cdot ",
    "ˆ": r"\hat ",
}


def _with_inline_math(line: Any) -> str:
    """把一行里的公式收成带上下标的 $...$。

    中文注释：下标比正文字小、位置更靠下，上标更小、更靠上。以前只看是不是数学字符，
    W 和下标 Q 被拼成 $WQ$。现在按字号和高低写成 W_{Q}。公式中间夹着很少几个普通单词
    （比如 ghost nodes）时仍放在同一对 $ 里，避免 $ 把式子从中间切开。
    """

    spans = [span for span in line.get("spans", []) if span.get("text")]
    if not spans:
        return ""
    sized = [float(span["size"]) for span in spans if str(span["text"]).strip()]
    max_size = max(sized) if sized else 0.0
    normal_centers = [
        _span_center_y(span)
        for span in spans
        if str(span["text"]).strip() and max_size > 0 and float(span["size"]) > max_size * 0.85
    ]
    line_base_y = sum(normal_centers) / len(normal_centers) if normal_centers else None

    pieces: list[str] = []
    tokens: list[tuple[str, str]] = []
    last_base: dict[str, Any] | None = None

    def flush_math() -> None:
        """把攒着的公式写成一对 $，没有上下标的单个字母就原样留下。"""

        nonlocal last_base
        if not tokens:
            return
        pieces.append(_emit_inline_math(tokens))
        tokens.clear()
        last_base = None

    for index, span in enumerate(spans):
        text = str(span["text"])
        if not text.strip():
            # 中文注释：后面还有公式时，空格留在公式里面。后面是普通句子就把公式先收尾。
            if tokens and _has_later_math(spans, index):
                tokens.append(("base", text))
            else:
                flush_math()
                pieces.append(text)
            continue
        if _span_is_math(span) or _is_small_punct(span, max_size):
            role = _script_role(last_base, span) if last_base is not None else None
            if role is None and last_base is None and _span_is_math(span):
                role = _script_role_against_line(span, max_size, line_base_y)
            if role in {"sub", "sup"}:
                body = text.strip()
                if tokens and tokens[-1][0] == role:
                    tokens[-1] = (role, tokens[-1][1] + body)
                elif body:
                    tokens.append((role, body))
                continue
            if not _span_is_math(span):
                flush_math()
                pieces.append(text)
                continue
            last_base = span
            tokens.append(("base", text))
            continue
        accent = _leading_accent(span, spans, index)
        if accent is not None:
            # 中文注释：波浪号和字母一样大，只是位置更高，是盖在字母上的重音，不是单独一个字。
            tokens.append(("accent", accent))
            continue
        # 中文注释：只把“下一个公式之前、总共不超过三个小写单词”收进式子。
        # 每个单词往往单独占一截，不能看见后面还有公式就把整句都吞进去。
        gap = _text_before_next_math(spans, index)
        if tokens and gap is not None and _short_lowercase_gap(gap):
            if _has_letter(text):
                tokens.append(("text", text.strip()))
            else:
                tokens.append(("base", text))
            continue
        flush_math()
        pieces.append(text)
    flush_math()
    return "".join(pieces)


def _span_center_y(span: dict[str, Any]) -> float:
    """这一小截字的纵向中心。PDF 里越靠下，这个数越大。"""

    bbox = span["bbox"]
    return (float(bbox[1]) + float(bbox[3])) / 2


def _span_is_math(span: dict[str, Any]) -> bool:
    """这一小截是不是公式字形，而不是普通单词。"""

    text = str(span["text"])
    chars = [character for character in text if not character.isspace()]
    if not chars:
        return False
    if _is_math_font(str(span.get("font") or "")):
        return True
    return all(_is_math_character(character) for character in chars)


def _script_role(base: dict[str, Any] | None, span: dict[str, Any]) -> str | None:
    """更小、更靠下的是下标，更小、更靠上的是上标。字号差不多就不是上下标。"""

    if base is None:
        return None
    base_size = float(base["size"])
    size = float(span["size"])
    if base_size <= 0 or size > base_size * 0.85:
        return None
    base_y = _span_center_y(base)
    center_y = _span_center_y(span)
    if center_y > base_y + 1.0:
        return "sub"
    if center_y < base_y - 1.0:
        return "sup"
    return None


def _script_role_against_line(span: dict[str, Any], max_size: float, line_base_y: float | None) -> str | None:
    """这一行开头没有主体、只有一小截更小的字时，按整行正文的位置判断上下标。"""

    if line_base_y is None or max_size <= 0 or float(span["size"]) > max_size * 0.85:
        return None
    center_y = _span_center_y(span)
    if center_y > line_base_y + 1.0:
        return "sub"
    if center_y < line_base_y - 1.0:
        return "sup"
    return None


def _is_small_punct(span: dict[str, Any], max_size: float) -> bool:
    """比正文小一号、又没有字母的符号，多半是上下标外面的括号。"""

    text = str(span["text"]).strip()
    if not text or _has_letter(text) or max_size <= 0:
        return False
    return float(span["size"]) <= max_size * 0.85


def _has_later_math(spans: list[dict[str, Any]], index: int) -> bool:
    """这一截后面，这一行里还有没有公式字形。"""

    for span in spans[index + 1 :]:
        if str(span.get("text") or "").strip() and _span_is_math(span):
            return True
    return False


def _text_before_next_math(spans: list[dict[str, Any]], index: int) -> str | None:
    """从这里到下一个公式字形之间的普通文字。后面没有公式就返回空。"""

    chunks: list[str] = []
    for span in spans[index:]:
        if str(span.get("text") or "").strip() and _span_is_math(span):
            return "".join(chunks)
        chunks.append(str(span.get("text") or ""))
    return None


def _short_lowercase_gap(text: str) -> bool:
    """中间这几个字是不是还能算在同一条公式里。

    中文注释：ghost nodes 这种小写短语可以。出现句号，或者 Note 这种大写开头，
    就是下一句了，不能再吞进公式。
    """

    words = text.split()
    if not words or len(words) > 3:
        return False
    if any(mark in text for mark in ".?!"):
        return False
    return not any(word[:1].isupper() for word in words)


_ACCENT_COMMANDS = {
    "˜": "tilde",
    "~": "tilde",
    "ˆ": "hat",
    "¯": "bar",
}


def _leading_accent(span: dict[str, Any], spans: list[dict[str, Any]], index: int) -> str | None:
    """盖在下一个字母上方、字号却一样大的重音，收成 \\tilde 这类写法。"""

    command = _ACCENT_COMMANDS.get(str(span["text"]).strip())
    if command is None:
        return None
    for later in spans[index + 1 :]:
        if not str(later.get("text") or "").strip():
            continue
        if not _span_is_math(later):
            return None
        if _span_center_y(span) < _span_center_y(later) - 1.0:
            return command
        return None
    return None


def _has_letter(text: str) -> bool:
    """这段里有没有字母。只有标点的话不当成单词。"""

    return any(unicodedata.category(character).startswith("L") for character in text)


def _latex_plain(text: str) -> str:
    """把抽出来的公式字符换成 LaTeX 里安全的写法。"""

    pieces: list[str] = []
    for character in text:
        if character in _LATEX_SYMBOLS:
            pieces.append(_LATEX_SYMBOLS[character])
            continue
        if character in "\\{}_%&#^":
            pieces.append("\\" + character)
            continue
        pieces.append(character)
    return "".join(pieces)


def _emit_inline_math(tokens: list[tuple[str, str]]) -> str:
    """把主体、下标、上标拼成一对 $。没有上下标的单个字母不套 $。"""

    has_script = any(kind != "base" and kind != "text" for kind, _ in tokens)
    latex_parts: list[str] = []
    plain_parts: list[str] = []
    accent = ""
    for index, (kind, text) in enumerate(tokens):
        if kind == "accent":
            accent = text
            continue
        if kind == "sub":
            latex_parts.append("_{" + _latex_plain(text) + "}")
            continue
        if kind == "sup":
            latex_parts.append("^{" + _latex_plain(text) + "}")
            continue
        # 中文注释：主体和下标之间有时会抽到一个空格。空格留在 ^ 或 _ 前面，上下标会对不齐。
        if kind == "base" and index + 1 < len(tokens) and tokens[index + 1][0] in {"sub", "sup"}:
            text = text.rstrip()
        if kind == "text":
            latex_parts.append(r"\text{" + text.replace("}", r"\}") + "}")
            plain_parts.append(text)
            accent = ""
            continue
        body = _latex_plain(text)
        if accent and body.strip():
            prefix = body[: len(body) - len(body.lstrip())]
            body = prefix + "\\" + accent + "{" + body.strip() + "}"
            accent = ""
        latex_parts.append(body)
        plain_parts.append(text)
    latex = "".join(latex_parts)
    plain = "".join(plain_parts)
    if not latex.strip():
        return plain
    if not has_script:
        stripped = plain.strip()
        if len(stripped) < 2 or not _has_letter(stripped):
            return plain
    leading = latex[: len(latex) - len(latex.lstrip())]
    trailing = latex[len(latex.rstrip()) :]
    return f"{leading}${latex.strip()}${trailing}"


_LEADING_SCRIPT = re.compile(r"^\$([_^])\{([^{}]*)\}\$")


def _attach_leading_script(previous: str, following: str) -> str | None:
    """下一行开头的 $_{...}$ 或 $^{...}$ 接进上一行最后一对公式。接不上就返回空。"""

    match = _LEADING_SCRIPT.match(following.lstrip())
    if match is None:
        return None
    formulas = list(re.finditer(r"\$([^$]+)\$", previous))
    if not formulas:
        return None
    last = formulas[-1]
    inner = last.group(1) + match.group(1) + "{" + match.group(2) + "}"
    rest = following.lstrip()[match.end() :]
    merged = previous[: last.start()] + f"${inner}$" + previous[last.end() :]
    if rest.startswith((",", ".", ";", ":", ")", "]", "}")):
        return merged.rstrip() + rest
    if rest:
        return merged.rstrip() + rest
    return merged.rstrip()


def _collect_formula_regions(
    page: Any,
    blocks: list[Any],
    table_rects: list[tuple[float, float, float, float]],
    page_number: int,
) -> list[PdfFormulaRegion]:
    """把页面上"看起来是公式"的行按位置粘成一块块区域。

    中文注释：为什么需要"粘"——上下标在 PDF 里是单独排的，一条公式常被切好几块
    （实测 S_i^l = alpha_i^l · W_i^l 被拆成 4 块）。只按单行截图会截到残缺的半条公式，
    所以要先判断"哪些行是公式"，再把这些行按挨得近不近聚成块，最后整块截图。
    """

    # 第一步：把"是公式、又不在表格里"的行挑出来，连它属于第几个块、第几行一起记下。
    # 同时把不是公式的行也收一份坐标，后面判断"公式是不是夹在句子中间"要用。
    page_lines: list[_FormulaLine] = []
    for block_index, block in enumerate(blocks):
        if _covered_by_table(_rect_tuple(block["bbox"]), table_rects) is not None:
            continue
        for line_index, line in enumerate(block["lines"]):
            text = "".join(span["text"] for span in line["spans"])
            if not text.strip():
                continue
            page_lines.append(
                _FormulaLine(
                    block_index=block_index,
                    line_index=line_index,
                    rect=_rect_tuple(line["bbox"]),
                    text=text.strip(),
                    is_math=_is_math_line(line),
                )
            )
    math_lines = [entry for entry in page_lines if entry.is_math]
    if not math_lines:
        return []
    all_rects = [entry.rect for entry in page_lines]

    # 第二步：从上到下、从左到右排一遍，再把挨在一起的行并成一块。
    ordered = sorted(math_lines, key=lambda entry: (entry.rect[1], entry.rect[0]))
    groups: list[list[_FormulaLine]] = [[ordered[0]]]
    current_rect = ordered[0].rect
    for entry in ordered[1:]:
        if _rects_are_close(current_rect, entry.rect):
            groups[-1].append(entry)
            current_rect = _union_rect(current_rect, entry.rect)
            continue
        groups.append([entry])
        current_rect = entry.rect
    # 中文注释：上面是顺着往下扫的，扫到右边另一栏的公式就会把左边这条放下。
    # 左边公式的尾巴如果排得更低，就会被落成单独一块。这里再把挨在一起的块并回去。
    groups = _merge_close_groups(groups)
    groups = _merge_bridged_groups(groups, all_rects)
    # 中文注释：分子分母上下离得开一点，先按"左右重叠、上下不远"再并一次。
    groups = _merge_stacked_groups(groups)
    used_lines = {(entry.block_index, entry.line_index) for group in groups for entry in group}
    absorbed: list[list[_FormulaLine]] = []
    for group in groups:
        widened = _absorb_left_companions(group, page_lines, used_lines)
        absorbed.append(widened)
    groups = absorbed
    groups.sort(key=lambda group: (_group_rect(group)[1], _group_rect(group)[0]))

    page_area = float(page.rect.width) * float(page.rect.height)
    regions: list[PdfFormulaRegion] = []
    for group in groups:
        ordered = sorted(group, key=lambda entry: (round(entry.rect[1], 1), entry.rect[0]))
        rect = _group_rect(ordered)
        text = "\n".join(entry.text for entry in ordered)
        # 中文注释：三道闸门，任何一条不过就当"认错了"直接丢掉，宁缺勿滥。
        # 一是区域占了大半页——那是把整段正文认成公式了。
        if page_area > 0 and (rect[2] - rect[0]) * (rect[3] - rect[1]) / page_area > _MAX_REGION_AREA_RATIO:
            continue
        # 二是行数太多，也不像单条公式。
        if len(group) > _MAX_REGION_ROWS:
            continue
        # 三是去掉空白后没剩几个字，是噪声。
        if len(re.sub(r"\s+", "", text)) < _MIN_MATH_LINE_CHARS:
            continue

        # 中文注释：先判断这是不是"自己占一行"的公式，再去扩编号。
        # 顺序不能反——扩编号会把编号那一行圈进矩形里，而编号本身是一段不带数学符号的
        # 短文字，先扩再判的话它就会把公式"认成"夹在句子里的行内公式，白白漏掉转写。
        member_keys = {(entry.block_index, entry.line_index) for entry in ordered}
        # 中文注释：已经收进公式里的字不能再拿来判断"旁边还有句子"。
        # 否则函数名被收进来之后，又被当成旁边的正文，整条公式就不送去转写了。
        neighbors = [
            entry.rect
            for entry in page_lines
            if (entry.block_index, entry.line_index) not in member_keys
            and not entry.is_math
            and len(re.sub(r"\s+", "", entry.text)) >= _MIN_INLINE_NEIGHBOR_CHARS
        ]
        is_display = _region_is_display(rect, neighbors)

        # 中文注释：公式编号（形如 (2)）一般单独排在页边，离公式主体有一段距离，
        # 上面的聚合够不着它。这里单独找一次，找到就把截图范围往右扩到编号，
        # 这样模型截图里能看见编号，转写时会把编号一起写出来——下游引用"式(2)"才对得上。
        rect, number = _extend_to_equation_number(rect, page_lines)
        regions.append(
            PdfFormulaRegion(
                page_number=page_number,
                index=len(regions) + 1,
                # 中文注释：截图范围要在区域外面留一圈白边。公式的分数线、根号横杠
                # 常常比文字本身宽一点点，不留白会被切掉；留多了也没关系，
                # 反正模型看得懂，多几个空白像素不影响它认公式。
                rect=(
                    rect[0] - _REGION_PADDING_X,
                    rect[1] - _REGION_PADDING_Y,
                    rect[2] + _REGION_PADDING_X,
                    rect[3] + _REGION_PADDING_Y,
                ),
                is_display=is_display,
                text=text,
                number=number,
                members=[(entry.block_index, entry.line_index) for entry in ordered],
            )
        )
    return regions


def _rects_are_close(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> bool:
    """判断两块文字是不是挨在一起（上下近、左右也近）。"""

    # 中文注释：这个方法顺带把双栏排版的问题也解决了——同一水平线上左右两栏是两个
    # 完全不同的公式，中间隔着栏间空白（实测约 14 点），超过阈值就不会被粘成一块。
    # 所以不需要专门去识别"这是不是双栏"，一个横向间距就够了。
    vertical_gap = max(0.0, max(first[1], second[1]) - min(first[3], second[3]))
    if vertical_gap > _REGION_VERTICAL_GAP:
        return False
    horizontal_gap = max(0.0, max(first[0], second[0]) - min(first[2], second[2]))
    return horizontal_gap <= _REGION_HORIZONTAL_GAP


def _merge_close_groups(groups: list[list["_FormulaLine"]]) -> list[list["_FormulaLine"]]:
    """把上下左右都挨着的几块并成一块。

    中文注释：从左到右扫的时候，右边另一条公式可能先被扫到，
    左边公式更低的那截尾巴就被单独留下。只要两块还挨着，就并回去。
    """

    changed = True
    while changed:
        changed = False
        for left_index in range(len(groups)):
            for right_index in range(left_index + 1, len(groups)):
                if not _rects_are_close(_group_rect(groups[left_index]), _group_rect(groups[right_index])):
                    continue
                groups[left_index] = groups[left_index] + groups[right_index]
                del groups[right_index]
                changed = True
                break
            if changed:
                break
    return groups


def _merge_bridged_groups(
    groups: list[list["_FormulaLine"]], all_rects: list[tuple[float, float, float, float]]
) -> list[list["_FormulaLine"]]:
    """把"本来是同一条公式、却被中间的字隔开"的几块合并回一起。

    中文注释：为什么需要单独一趟——有些公式中间夹着只有一两个字符的大运算符字形
    （求和号、连乘号那种），那些字形够不上"整行是公式"的门槛、没被选进来，
    于是同一条公式被拆成互不相连的几块（实测 MOM 那条公式被拆成 3 块）。

    为什么不在第一步直接合并——第一步是拿"已经并起来的大块"去比下一行，块越大越容易
    把不相干的行也吸进来，最后整页塌成一块、反而被"太大了"的闸门丢掉。所以先老老实实
    按位置聚一次，再只对"确实在同一行上"的块做合并。
    """

    changed = True
    while changed:
        changed = False
        for left_index in range(len(groups)):
            for right_index in range(left_index + 1, len(groups)):
                left_rect = _group_rect(groups[left_index])
                right_rect = _group_rect(groups[right_index])
                if not _same_line_bridged(left_rect, right_rect, all_rects):
                    continue
                groups[left_index] = groups[left_index] + groups[right_index]
                del groups[right_index]
                changed = True
                break
            if changed:
                break
    return groups


def _merge_stacked_groups(groups: list[list["_FormulaLine"]]) -> list[list["_FormulaLine"]]:
    """把上下叠着、左右又对得上的几块并成一条公式。

    中文注释：分数的分子和分母经常不在同一行，中间还隔着分数线。
    只按"挨得很近"去并，它们会变成两条残缺的公式。
    """

    changed = True
    while changed:
        changed = False
        for left_index in range(len(groups)):
            for right_index in range(left_index + 1, len(groups)):
                left_rect = _group_rect(groups[left_index])
                right_rect = _group_rect(groups[right_index])
                vertical_gap = max(0.0, max(left_rect[1], right_rect[1]) - min(left_rect[3], right_rect[3]))
                if vertical_gap > _STACK_VERTICAL_GAP:
                    continue
                overlap = min(left_rect[2], right_rect[2]) - max(left_rect[0], right_rect[0])
                narrower = min(left_rect[2] - left_rect[0], right_rect[2] - right_rect[0])
                if narrower <= 0 or overlap < narrower * 0.35:
                    continue
                if len(groups[left_index]) + len(groups[right_index]) > _MAX_REGION_ROWS:
                    continue
                groups[left_index] = groups[left_index] + groups[right_index]
                del groups[right_index]
                changed = True
                break
            if changed:
                break
    return groups


def _absorb_left_companions(
    group: list["_FormulaLine"],
    page_lines: list["_FormulaLine"],
    used_lines: set[tuple[int, int]],
) -> list["_FormulaLine"]:
    """把公式同一行左边还没被收进来的字补进这块。

    中文注释：有的公式右边是数学符号，左边是普通字体的函数名。
    只认数学符号的话，截图就只剩尾巴。这里沿着同一行往左收，
    碰到一整句普通文字或者栏间的大空白就停下。
    """

    members = list(group)
    rect = _group_rect(members)
    changed = True
    while changed:
        changed = False
        for entry in page_lines:
            key = (entry.block_index, entry.line_index)
            if key in used_lines:
                continue
            overlap = min(rect[3], entry.rect[3]) - max(rect[1], entry.rect[1])
            shorter = min(rect[3] - rect[1], entry.rect[3] - entry.rect[1])
            if shorter <= 0 or overlap < shorter * 0.45:
                continue
            if entry.rect[0] >= rect[2]:
                continue
            gap = rect[0] - entry.rect[2]
            if gap > _ABSORB_HORIZONTAL_GAP:
                continue
            if not entry.is_math and len(entry.text) > 60:
                continue
            members.append(entry)
            used_lines.add(key)
            rect = _union_rect(rect, entry.rect)
            changed = True
    return members


def _center_inside_any(
    line_rect: tuple[float, float, float, float],
    regions: list[tuple[float, float, float, float]],
) -> bool:
    """这一行的中心是不是落在某条独立公式的框里。"""

    center_x = (line_rect[0] + line_rect[2]) / 2
    center_y = (line_rect[1] + line_rect[3]) / 2
    for rect in regions:
        if rect[0] <= center_x <= rect[2] and rect[1] <= center_y <= rect[3]:
            return True
    return False


def _group_rect(group: list["_FormulaLine"]) -> tuple[float, float, float, float]:
    """一组行的整体外框。"""

    rect = group[0].rect
    for entry in group[1:]:
        rect = _union_rect(rect, entry.rect)
    return rect


def _same_line_bridged(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    all_rects: list[tuple[float, float, float, float]],
) -> bool:
    """两块是不是"在同一条线上、中间只隔着一小段还夹着字"。

    中文注释：要求两块上下重合得足够多（至少是矮的那块的一半），确认它们确实在同一行；
    然后看中间那段空当里有没有别的字。双栏之间的空当是干干净净的，不会被误合并。
    """

    overlap = min(first[3], second[3]) - max(first[1], second[1])
    shorter = min(first[3] - first[1], second[3] - second[1])
    if shorter <= 0 or overlap < shorter * 0.5:
        return False
    left, right = (first, second) if first[0] <= second[0] else (second, first)
    if right[0] - left[2] > _BRIDGE_MAX_GAP:
        return False
    return _gap_contains_text(first, second, all_rects)


def _gap_contains_text(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    all_rects: list[tuple[float, float, float, float]],
) -> bool:
    """两个片段中间那段空当里，是不是还夹着别的文字。"""

    left, right = (first, second) if first[0] <= second[0] else (second, first)
    gap_start, gap_end = left[2], right[0]
    if gap_end <= gap_start:
        return False
    top = max(left[1], right[1])
    bottom = min(left[3], right[3])
    for candidate in all_rects:
        # 横向要落在空当里，纵向要和这一行齐平。
        if candidate[2] <= gap_start or candidate[0] >= gap_end:
            continue
        if candidate[3] <= top or candidate[1] >= bottom:
            continue
        return True
    return False


def _union_rect(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """求两块矩形的外框。"""

    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _region_is_display(
    rect: tuple[float, float, float, float], other_lines: list[tuple[float, float, float, float]]
) -> bool:
    """判断这块公式是"自己占一行"还是"夹在句子中间"。

    中文注释：办法很简单——看它同一水平位置的左边或右边有没有别的文字。如果有、
    而且挨得近，说明它和那些文字同处一行，那就是句子里的公式；四周空荡荡的，
    才是单独占一行的公式。只有后者值得截图交给模型转写。
    """

    for other in other_lines:
        # 上下不重叠，就不是同一行的文字，跳过。
        if other[3] <= rect[1] or other[1] >= rect[3]:
            continue
        gap = max(0.0, max(rect[0], other[0]) - min(rect[2], other[2]))
        if gap <= _INLINE_NEIGHBOR_GAP:
            return False
    return True


def _extend_to_equation_number(
    rect: tuple[float, float, float, float], page_lines: list["_FormulaLine"]
) -> tuple[tuple[float, float, float, float], str]:
    """从区域文字里找出公式编号，比如单独一行写着的 (2)。

    中文注释：只认"整行就是一个括号包着的数字"这种形状，不在整段文字里乱找数字——
    否则公式里面的系数会被当成编号。
    """

    best: _FormulaLine | None = None
    best_match: re.Match[str] | None = None
    for entry in page_lines:
        if entry.rect[0] < rect[2]:
            continue
        if min(entry.rect[3], rect[3]) - max(entry.rect[1], rect[1]) <= 0:
            continue
        # 中文注释：只认"整行就是一个括号包着的数字"的那种行。公式右边常散着逗号、
        # 单个字母这类碎片，它们离得更近，但它们不是编号，直接跳过。
        match = _EQUATION_NUMBER_PATTERN.match(entry.text)
        if match is None:
            continue
        if best is None or entry.rect[0] < best.rect[0]:
            best = entry
            best_match = match
    if best is None or best_match is None:
        return rect, ""
    return (rect[0], rect[1], max(rect[2], best.rect[2]), rect[3]), best_match.group(1)


class _FormulaLine:
    """记一行文字：它在哪、写了什么、是不是公式。

    中文注释：故意不写成 dataclass，因为这个类只在上面那个函数内部用，
    写成普通类少一层导入，也少一份"要不要暴露出去"的纠结。
    """

    __slots__ = ("block_index", "line_index", "rect", "text", "is_math")

    def __init__(
        self,
        block_index: int,
        line_index: int,
        rect: tuple[float, float, float, float],
        text: str,
        is_math: bool,
    ) -> None:
        self.block_index = block_index
        self.line_index = line_index
        self.rect = rect
        self.text = text
        self.is_math = is_math


def _is_math_font(font_name: str) -> bool:
    """判断一个字体是不是数学字体。

    中文注释：先排除 CMR/CMBX 这些 Computer Modern 正文字体，再看名字里有没有数学
    字体的关键词。顺序不能反：万一以后冒出一个名字同时含两边的字体，宁可当正文字体。
    """

    if any(token in font_name for token in _TEXT_FONT_TOKENS):
        return False
    return any(token in font_name for token in _MATH_FONT_TOKENS)


def _rect_tuple(rect: Any) -> tuple[float, float, float, float]:
    """把 PyMuPDF 的矩形统一成 (左, 上, 右, 下) 四个数字。"""

    return (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))


def _covered_by_table(
    block_rect: tuple[float, float, float, float],
    table_rects: list[tuple[float, float, float, float]],
) -> int | None:
    """判断一个文字块是不是"基本上落在某个表格里"，是的话返回第几个表格。

    中文注释：用"重叠面积占文字块面积的一半以上"来判断。不用"完全包含"是因为表格
    边框附近偶尔有偏差；也不用"碰到一点就算"，免得把紧挨表格的正文误删。
    """

    width = block_rect[2] - block_rect[0]
    height = block_rect[3] - block_rect[1]
    area = width * height
    if area <= 0:
        return None
    for index, table_rect in enumerate(table_rects):
        overlap_width = min(block_rect[2], table_rect[2]) - max(block_rect[0], table_rect[0])
        overlap_height = min(block_rect[3], table_rect[3]) - max(block_rect[1], table_rect[1])
        if overlap_width <= 0 or overlap_height <= 0:
            continue
        if overlap_width * overlap_height / area >= 0.5:
            return index
    return None


def _drop_repeated_margin_blocks(page_blocks: list[list[PageBlock]]) -> list[list[PageBlock]]:
    """拿掉每一页开头、结尾反复出现的那一行。

    中文注释：期刊名、页码会印在每一页的顶上或底下。只看每页第一块和最后一块，
    同一句话出现了至少三次，就把它拿掉。公式、表格、图不动。
    """

    if len(page_blocks) < 3:
        return page_blocks

    def edge_key(blocks: list[PageBlock], *, first: bool) -> str:
        ordered = blocks if first else list(reversed(blocks))
        for block in ordered:
            if block.kind in {"display_formula", "inline_formula", "table", "figure"}:
                return ""
            line = next((item.strip() for item in block.text.splitlines() if item.strip()), "")
            if not line or line.startswith(("$$", "|", "![")):
                return ""
            return _compact_line(line)
        return ""

    first_keys = [edge_key(blocks, first=True) for blocks in page_blocks]
    last_keys = [edge_key(blocks, first=False) for blocks in page_blocks]
    pool = [*first_keys, *last_keys]
    repeated = {key for key in pool if key and pool.count(key) >= 3}
    if not repeated:
        return page_blocks
    cleaned: list[list[PageBlock]] = []
    for blocks, first_key, last_key in zip(page_blocks, first_keys, last_keys, strict=True):
        result = list(blocks)
        if result and first_key in repeated:
            result = result[1:]
        if result and last_key in repeated and not (first_key in repeated and len(blocks) == 1):
            result = result[:-1]
        cleaned.append(result)
    return cleaned


_REFERENCES_HEADING = re.compile(r"(?i)^\s*(references|bibliography|参考文献)\s*$")


def _drop_reference_blocks(page_blocks: list[list[PageBlock]]) -> list[list[PageBlock]]:
    """从后半篇里找到"参考文献"标题，把它和后面的块都丢掉。"""

    if not page_blocks:
        return page_blocks
    start_page = max(0, len(page_blocks) // 2)
    for page_index in range(start_page, len(page_blocks)):
        for block_index, block in enumerate(page_blocks[page_index]):
            if block.kind in {"display_formula", "inline_formula", "table", "figure"}:
                continue
            first_line = next((line.strip() for line in block.text.splitlines() if line.strip()), "")
            if _REFERENCES_HEADING.match(first_line):
                kept = page_blocks[page_index][:block_index]
                return [*page_blocks[:page_index], kept]
    return page_blocks


def _remove_repeated_headers_and_footers(pages: list[str]) -> list[str]:
    """删除多页重复出现的页眉和页脚。

    中文注释：很多论文每页顶部或底部都会重复会议名、作者名、页码。这里只看每页
    第一行和最后一行，如果同一行在多页重复出现，就把它去掉。规则很保守，不会
    大面积改动正文。

    注意：表格行（以"|"开头）和 $$ 公式块里的行一律不参与判断，也绝不会被删掉。
    否则表格会被砍掉表头、公式会被拆散。代价是"页眉正好压在表格上面"时页眉删不掉，
    这是可以接受的保守取舍。
    """

    if len(pages) < 3:
        return pages
    first_lines = [_edge_line(page, first=True) for page in pages]
    last_lines = [_edge_line(page, first=False) for page in pages]
    repeated = {
        line
        for line in [*first_lines, *last_lines]
        if line and sum(candidate == line for candidate in [*first_lines, *last_lines]) >= 3
    }
    if not repeated:
        return pages
    cleaned: list[str] = []
    for page in pages:
        lines = page.splitlines()
        protected = _protected_lines(lines)
        start = 0
        end = len(lines)
        while start < end and start not in protected and _compact_line(lines[start]) in repeated:
            start += 1
        while end > start and (end - 1) not in protected and _compact_line(lines[end - 1]) in repeated:
            end -= 1
        cleaned.append("\n".join(lines[start:end]))
    return cleaned


def _remove_references_at_end(pages: list[str]) -> list[str]:
    """删除最后参考文献部分。

    中文注释：全文提取研究主题、方法和结论时，最后的参考文献通常只会增加噪声。
    这里只从后半篇开始找 References/参考文献 标题，找到后删除它后面的内容。

    注意：只认"整行就是 References"的行，所以表格行（"| References |"）本来就
    匹配不上；公式块里的行同样不会命中。
    """

    if not pages:
        return pages
    start_page = max(0, len(pages) // 2)
    cleaned = list(pages)
    pattern = re.compile(r"(?im)^\s*(references|bibliography|参考文献)\s*$")
    for index in range(start_page, len(cleaned)):
        match = pattern.search(cleaned[index])
        if match is None:
            continue
        if _offset_is_protected(cleaned[index], match.start()):
            continue
        cleaned[index] = cleaned[index][: match.start()].strip()
        return cleaned[: index + 1]
    return cleaned


def _protected_lines(lines: list[str]) -> set[int]:
    """标出哪些行属于表格、公式块或公式占位符，这些行不能被当成页眉页脚删掉。

    中文注释：$$ 是成对出现的，所以用一个开关：碰到单独一行的 $$ 就翻转状态，
    翻转期间的所有行都算公式内容。

    公式占位符（<!-- formula: 5_1 -->）必须单独保护：删重复页眉页脚那一步做比较时
    会把数字统一换成 #，于是第 5 页的占位符和第 6 页的占位符看起来一模一样，
    要是好几页的开头都是公式，它们就会被当成"重复页眉"整批删掉，公式就悄悄丢了。
    """

    protected: set[int] = set()
    in_math = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _MARKER_PATTERN.match(line):
            protected.add(index)
            continue
        if stripped.startswith("$$"):
            if stripped.count("$$") < 2:
                in_math = not in_math
            protected.add(index)
            continue
        if in_math or _TABLE_LINE_PATTERN.match(line):
            protected.add(index)
    return protected


def _offset_is_protected(page: str, offset: int) -> bool:
    """判断整页文字里某个位置落没落在表格行或公式块里。

    中文注释：splitlines 把换行符吃掉了，所以要按行把长度累加回去，才能把"整页里
    的第几个字符"换算成"第几行"。
    """

    lines = page.splitlines()
    protected = _protected_lines(lines)
    consumed = 0
    for index, line in enumerate(lines):
        consumed += len(line) + 1
        if offset < consumed:
            return index in protected
    return bool(lines) and (len(lines) - 1) in protected


def _edge_line(page: str, *, first: bool) -> str:
    """取出页面最上面或最下面的"有效"非空行，用来识别重复页眉页脚。

    中文注释：有效指"不是表格行、也不在公式块里"。这两类行绝不能当页眉页脚处理。
    """

    lines = page.splitlines()
    protected = _protected_lines(lines)
    candidates = [
        _compact_line(line)
        for index, line in enumerate(lines)
        if _compact_line(line) and index not in protected
    ]
    if not candidates:
        return ""
    return candidates[0] if first else candidates[-1]


def _compact_line(value: str) -> str:
    """把一行文字压成便于比较的形式。"""

    return re.sub(r"\s+", " ", value).strip()


def _normalise_text(value: str) -> str:
    """整理 PDF 抽取出来的文字。

    中文注释：删除空字符，压缩过多空行。这里不做激进改写，避免误伤公式、标题和
    表格说明。Markdown 表格靠单个换行分行，所以只压缩三个及以上的空行，不动单换行。
    """

    text = value.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
