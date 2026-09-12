from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 中文注释：TeX 排版数学公式时会换成一套专门的"数学字体"（CMMI、CMSY 这种），
# 而正文用的是 Computer Modern Roman（CMR）这类字体。判断某一行是不是公式，
# 最可靠的办法就是看这一行里"数学字体字符"占多大比例。
#
# 这里必须把 CMR / CMBX / CMSS / CMTT / CMTI 明确排除掉：它们也是 CM 开头的 TeX
# 字体，但装的是普通文字。实测三篇论文里 CMR10 分别出现 243 / 709 / 844 个字符，
# 一旦把它们误当成数学字体，整页正文都会被判定成公式，输出就全毁了。
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
)
_TEXT_FONT_TOKENS = ("CMR", "CMBX", "CMSS", "CMTT", "CMTI", "CMB")

# 一行里数学字体字符占比超过这个值，就认为整行是公式。
_MATH_CHARACTER_RATIO = 0.5

# 小于这个字节数的图片基本都是 logo、图标、装饰线，写出来只会给正文添乱。
_MIN_IMAGE_BYTES = 10240

# 图注的样子：以 Figure / Fig. / TABLE / Table 加一个编号开头。
_CAPTION_PATTERN = re.compile(r"^(Figure|Fig\.?|TABLE|Table)\s*\d+")
# 从图注里把编号抠出来。中文注释：必须紧跟在 Figure/TABLE 这类词后面才算数，
# 不能"在整句里随便找一个数字"——否则 "Figure 2: accuracy at 50% coverage"
# 这种图注会被当成第 50 张图。
_CAPTION_NUMBER = re.compile(r"^(?:Figure|Fig\.?|TABLE|Table)\s*(\d+)")

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
class ParsedPdfPage:
    """保存 PDF 单页解析后的文字。

    中文注释：除了页码和正文，metadata 里还会带上这一页找到几个表格、写出几张图，
    这样上层想知道"这页有没有表格/图"时不用再去读 PDF。
    """

    page_number: int
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PdfParseResult:
    """保存一次 PDF 解析的统一结果。

    中文注释：不管底层用 pypdf、PyMuPDF 还是更高级的解析器，最终都整理成
    pages + warnings。调用方只关心这个统一形状。
    """

    pages: list[ParsedPdfPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
        # 有些图附近找不到图注，这时用一个从头到尾递增的编号兜底。
        figure_counter = 0
        # 中文注释：全篇已经用过的图号。图注里写的编号和兜底计数器有可能撞号
        # （比如第 11 张图配到了写着 "Fig. 11" 的图注，第 12 张图没配到图注、
        # 兜底计数恰好也是 11），撞号之后正文里就会出现两张 "Figure 11"，分不清谁是谁。
        used_figure_numbers: set[int] = set()
        page_texts: list[str] = []
        page_metadatas: list[dict[str, Any]] = []
        for page_index in range(doc.page_count):
            page = doc[page_index]
            text, table_count, figure_count = self._render_page(
                pymupdf,
                doc,
                page,
                page_index + 1,
                assets_dir,
                written_xrefs,
                figure_counter,
                used_figure_numbers,
            )
            figure_counter += figure_count
            page_texts.append(text)
            page_metadatas.append(
                {"parser": self.name, "table_count": table_count, "figure_count": figure_count}
            )
        cleaned_pages = _remove_repeated_headers_and_footers(page_texts)
        cleaned_pages = _remove_references_at_end(cleaned_pages)
        pages: list[ParsedPdfPage] = []
        for index, text in enumerate(cleaned_pages):
            normalised = _normalise_text(text)
            if not normalised.strip():
                continue
            pages.append(ParsedPdfPage(page_number=index + 1, text=normalised, metadata=page_metadatas[index]))
        if not pages:
            return PdfParseResult(warnings=["PDF 中没有可读取的文字，可能是扫描文件"])
        return PdfParseResult(pages=pages)

    def _render_page(
        self,
        pymupdf: Any,
        doc: Any,
        page: Any,
        page_number: int,
        assets_dir: Path | None,
        written_xrefs: set[int],
        figure_offset: int,
        used_figure_numbers: set[int],
    ) -> tuple[str, int, int]:
        """把一页内容拼成 Markdown，返回 (正文, 表格数, 写出的图片数)。"""

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

        figures, figure_count = _collect_figures(
            doc, page, page_number, assets_dir, written_xrefs, text_blocks, figure_offset, used_figure_numbers
        )

        items: list[_PageItem] = []
        first_item_of_block: dict[int, _PageItem] = {}
        emitted_tables: set[int] = set()
        math_buffer: list[str] = []

        def flush_math(y: float) -> None:
            """把攒起来的连续公式行合成一个 $$ 块。

            中文注释：一条长公式常常一行放不下、被拆成好几行。分开输出会变成一堆
            看不懂的碎片，所以要合并回一整块。
            """

            if math_buffer:
                items.append(_PageItem("$$\n" + "\n".join(math_buffer) + "\n$$", y))
                math_buffer.clear()

        def flush_plain(lines: list[str], y: float, block: Any) -> None:
            """把一个文字块里攒下的普通正文行合成一个段落。

            中文注释：一个文字块（get_text("dict") 里的 block）本来就是论文的一个段落，
            里面那些行只是"排版换行"，不是真的段落。以前每一行都单独输出成一个段落，
            结果是 Markdown 里每一行之间都空一行，一段话被拆成十几段。所以这里要把
            同一个块里的行拼回一段（用单个换行连起来，不把换行去掉——那是原文的断行）。
            """

            text = "\n".join(lines).strip()
            if not text:
                return
            item = _PageItem(text, y)
            items.append(item)
            # 中文注释：记住每个文字块产出的第一项。图片引用要插在图注前面，
            # 所以需要拿到"图注那个块的第一个项"作为插入位置。
            first_item_of_block.setdefault(id(block), item)

        for block in blocks:
            block_top = float(block["bbox"][1])
            table_index = _covered_by_table(_rect_tuple(block["bbox"]), table_rects)
            if table_index is not None:
                # 中文注释：表格里的文字已经被拼进 Markdown 表格了。这些原始文字块要是
                # 再输出一遍，表格内容就会在正文里出现两次，所以整块跳过。
                if table_index not in emitted_tables:
                    emitted_tables.add(table_index)
                    flush_math(block_top)
                    items.append(_PageItem(table_markdowns[table_index], float(table_rects[table_index][1])))
                continue
            plain_lines: list[str] = []
            for line in block["lines"]:
                line_text = "".join(span["text"] for span in line["spans"])
                if not line_text.strip():
                    continue
                if _is_math_line(line):
                    flush_plain(plain_lines, block_top, block)
                    plain_lines = []
                    math_buffer.append(line_text.strip())
                    continue
                # 中文注释：普通正文行出现，说明上面攒的公式已经结束了，先把它落定。
                flush_math(block_top)
                plain_lines.append(line_text)
            flush_plain(plain_lines, block_top, block)
        flush_math(float(blocks[-1]["bbox"][1]) if blocks else 0.0)

        for figure in figures:
            caption_block = figure["caption_block"]
            anchor = first_item_of_block.get(id(caption_block)) if caption_block is not None else None
            figure_item = _PageItem(figure["markdown"], figure["y"])
            if anchor is None:
                _insert_by_position(items, figure_item)
            else:
                index = next((i for i, item in enumerate(items) if item is anchor), len(items))
                items.insert(index, figure_item)

        # 中文注释：这里要报"真正输出进正文的表格数"，而不是"检测到几个表格"。
        # find_tables 报的数量会虚高，有两种情况：一是把一张表里套着的小表也单独找出来
        # （实测一篇论文的第一页报了 3 张，其实那 3 个矩形是层层嵌套的同一个区域）；
        # 二是把图的标注区当成表格（_looks_like_table 会把它筛掉）。所以按检测数报不准。
        return "\n\n".join(item.text for item in items), len(emitted_tables), figure_count


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

    __slots__ = ("text", "y")

    def __init__(self, text: str, y: float) -> None:
        self.text = text
        self.y = y


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


def _collect_figures(
    doc: Any,
    page: Any,
    page_number: int,
    assets_dir: Path | None,
    written_xrefs: set[int],
    text_blocks: list[tuple[Any, str]],
    figure_offset: int,
    used_figure_numbers: set[int],
) -> tuple[list[dict[str, Any]], int]:
    """把页面上的图片存成文件，并给每张图配好图注和 Markdown 引用。"""

    if assets_dir is None:
        # 中文注释：调用方没给图片目录时完全跳过图片：不写文件，也不往正文里塞引用。
        return [], 0

    # 第一步：先把够大的图片落盘，把位置和文件名记下来。
    pending: list[tuple[tuple[float, float, float, float], str]] = []
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
            # 中文注释：小图基本是 logo、图标、装饰线。实测第三篇论文 56 个图片里
            # 只有 18 个达到 10KB，剩下全是这种装饰品。
            continue
        rects = page.get_image_rects(xref)
        if not rects:
            # 图片对象挂在页面上但实际没显示出来，没有位置就没法找图注。
            continue
        written_xrefs.add(xref)
        image_rect = _rect_tuple(rects[0])
        assets_dir.mkdir(parents=True, exist_ok=True)
        extension = info.get("ext") or "png"
        file_name = f"fig_p{page_number}_{len(pending) + 1}.{extension}"
        (assets_dir / file_name).write_bytes(payload)
        pending.append((image_rect, file_name))

    # 第二步：按纵向位置从上到下排好，跟图注一对一地配。
    # 中文注释：一页里有多张图时，如果每张图都各自去找"离我最近的图注"，
    # 两张图可能抢到同一条图注，结果两张图拿到同一个图号，正文里就分不清谁是谁了。
    # 所以这里按从上到下的顺序配，配过的图注不再给别人用。
    captions = _caption_candidates(text_blocks)
    used_captions: set[int] = set()
    figures: list[dict[str, Any]] = []
    for order, (image_rect, file_name) in enumerate(sorted(pending, key=lambda item: (item[0][1], item[0][0]))):
        caption_block, caption_text, caption_index = _best_caption(image_rect, captions, used_captions)
        if caption_index is not None:
            used_captions.add(caption_index)
        figure_number = _figure_number(caption_text, figure_offset + order + 1, used_figure_numbers)
        used_figure_numbers.add(figure_number)
        figures.append(
            {
                "caption_block": caption_block,
                "y": image_rect[1],
                "markdown": f"![Figure {figure_number}](assets/{file_name})",
            }
        )
    return figures, len(pending)


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


def _best_caption(
    image_rect: tuple[float, float, float, float],
    captions: list[tuple[Any, str, tuple[float, float, float, float]]],
    used: set[int],
) -> tuple[Any | None, str, int | None]:
    """给一张图挑最近的、且还没被别的图用掉的图注。"""

    best_distance: float | None = None
    best: tuple[Any | None, str, int | None] = (None, "", None)
    for index, (block, text, rect) in enumerate(captions):
        if index in used:
            continue
        distance = _vertical_distance(image_rect, rect)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = (block, text, index)
    return best


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
        if match:
            number = int(match.group(1))
            if number not in used:
                return number
    number = fallback
    while number in used:
        number += 1
    return number


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


def _is_math_line(line: Any) -> bool:
    """判断一整行是不是公式。

    中文注释：做法是把这一行里每个字符按其字体归到"数学"或"正文"两类，数学字体
    占比超过一半才算公式。这样正文里夹一个数学符号不会被误判成整行公式。
    """

    total = 0
    math_characters = 0
    for span in line["spans"]:
        text = span["text"]
        total += len(text)
        if _is_math_font(span["font"]):
            math_characters += len(text)
    if not total:
        return False
    return math_characters / total >= _MATH_CHARACTER_RATIO


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
    """标出哪些行属于表格或 $$ 公式块，这些行不能被当成页眉页脚删掉。

    中文注释：$$ 是成对出现的，所以用一个开关：碰到单独一行的 $$ 就翻转状态，
    翻转期间的所有行都算公式内容。
    """

    protected: set[int] = set()
    in_math = False
    for index, line in enumerate(lines):
        stripped = line.strip()
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
