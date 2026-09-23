from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from src.llm.config import SystemConfig
from src.paper_retrieval.models import PaperDocument
# 中文注释：引入项目统一的日志工具。正文质量闸拦下坏内容时记一条日志，
# 排查问题时能看到"哪一篇论文的全文被判为不合格、原因是什么"。
from src.utils import get_logger
from src.utils.read_utils.formula_ocr import transcribe_formula_regions
from src.utils.read_utils.pdf_parsers import PageBlock, PdfFormulaRegion, PdfTableRegion, get_pdf_parser
from src.utils.read_utils.table_ocr import transcribe_table_regions


logger = get_logger(__name__)

_UNSUPPORTED_FULLTEXT_WARNING = "暂不支持该全文文件格式"

# 中文注释：转换器的版本号。只要"PDF/HTML 怎么变成 Markdown"的规则改了，就把它加一。
# 缓存目录里放着的旧 paper.md 一旦发现版本号对不上，会被直接删掉重新转换——
# 不然升级之后大家读到的还是旧版转出来的、没有表格公式图片的正文。
# 2 -> 3：HTML 解析器修掉了"表格跨行跨列不展开导致整行左移""省略结束标签导致正文倒序"等问题，
# 旧缓存里存的正是那些错位的表格，必须让它们作废重转。
# 3 -> 4：公式从"把字形原样展平进 $$ 块"改成"截图交给模型转写成真正的 LaTeX"，
# 正文里多了 $...$ 包裹的行内公式，旧的展平结果必须作废。
# 4 -> 5：插图改成"位图 + 矢量图"一起收（矢量画出来的框架图、折线图以前完全抽不到），
# 图片引用的说明文字从"Figure 3"改成论文自己的图注原文，图里的文字不再混进正文。
# 5 -> 6：有图注的表格改成"截图交给模型重排表头"再写回正文，替掉以前那份表头被
# 糊成一格、列名对不上指标的 Markdown 表。
# 6 -> 7：正文先按块抽出（标题、段落、公式、表格、图），再渲染成 Markdown。
# 旧缓存是先揉成一篇长文再切的，公式经常被切碎，必须作废重转。
# 7 -> 8：行内公式按字号和高低收成上下标；TABLE I 这种罗马数字表注改走表格截图。
# 旧缓存里还是 $WQ$ 和竖着排的一列数字，必须作废重转。
CONVERTER_VERSION = 8

# 中文注释：正文质量闸的两个阈值——
# 1) 转换出来的正文（去掉首尾空白后）不足 3000 字符，就认为"不是正文"：
#    一篇真论文的正文动辄几万字符，而论文"介绍页/落地页"提取出来的文字往往只有很短一段。
# 2) 正文里 Unicode 替换符 \ufffd 的占比超过 1%，就认为是乱码：
#    这个符号只有在"原始字节没法正常解码"时才会出现，占比高说明拿回来的根本不是正常文字。
_MIN_FULLTEXT_CHARACTERS = 3000
_MAX_GARBLED_CHARACTERS_RATIO = 0.01


@dataclass(slots=True)
class MarkdownConversion:
    """保存全文转成 Markdown 后的文件位置、页数和提示信息。"""

    markdown_path: Path | None = None
    page_count: int | None = None
    # 中文注释：PDF 里的图片会被解析器写到 paper.md 旁边的 assets 目录，
    # 这里记下目录位置，方便上层需要时找到图片文件。目录里没有写出任何图片时是空的。
    assets_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)
    # 中文注释：公式转写用掉的 token 数。上层要把它并进整篇精读的用量里一起上报，
    # 不然后台看到的用量会比真实花的少。没开公式识别、或者一篇论文没有公式时都是 0。
    input_tokens: int = 0
    output_tokens: int = 0
    # 中文注释：这次转换用的块。分片和插图清单都从这里来。
    # 命中旧文件缓存时没有块，调用方再从 Markdown 里认。
    blocks: list[PageBlock] = field(default_factory=list)


@dataclass(slots=True)
class _PdfMarkdownDraft:
    """PDF 刚解析完、但还没写进文件的 Markdown 草稿。

    中文注释：为什么不解析完就直接写文件——公式还要交给模型转写，而转写是异步的、
    可能失败、也可能被用户中途取消。要是这时候就把带占位符的半成品写下去，
    下次再读会命中这份残次品（缓存只比对版本号，不看内容），公式就永远换不回来了。
    所以写文件统一放到最后一步，中途出任何意外磁盘上都干干净净。
    """

    markdown: str = ""
    page_count: int | None = None
    # 中文注释：这一篇认出来的公式，连位置一起带着，转写那一步要用。
    formulas: list[PdfFormulaRegion] = field(default_factory=list)
    blocks: list[PageBlock] = field(default_factory=list)
    # 中文注释：有图注的表格，同样要交给模型重排表头。
    tables: list[PdfTableRegion] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# 中文注释：这里以前有一个"同步版全文解析入口"的兼容壳（convert_fulltext_to_markdown），
# 全仓已经没有调用方（所有地方都直接 await 下面的异步入口），按"要改就改干净"
# 的原则删掉，避免留着一份没人用、以后还要跟着维护的重复入口。
async def async_convert_fulltext_to_markdown(
    paper: PaperDocument,
    *,
    source_path: Path,
    source_url: str | None,
    llm: Any | None = None,
    on_progress: Callable[[str], None] | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
) -> MarkdownConversion:
    """异步全文转换入口，供后续 async 阅读流程直接调用。

    中文注释：llm 是"用来把公式截图转成 LaTeX"的模型，可以不给——不给就跳过公式识别，
    公式退回原来的展平写法。on_progress 用来在转写过程中报进度，
    raise_if_cancelled 用来让长时间转写能中途停下。三个都是可选的，
    不传调用方的行为和以前完全一样。
    """

    markdown_path = _markdown_output_path(source_path)
    # 中文注释：哪怕只是看缓存文件存不存在，本质上也是本地磁盘操作。
    # 这里照样放进 to_thread，避免异步阅读流程在高并发时被本地文件检查拖慢。
    cached = await asyncio.to_thread(_load_cached_markdown, markdown_path)
    if cached is not None:
        return cached

    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        # 中文注释：PDF 这一步分三段走，顺序不能变——
        # ① 先解析成草稿（同步、重活、丢线程跑），这一步不写文件；
        # ② 再把公式截图交给模型转写（异步）；
        # ③ 最后才质量检查 + 写文件。
        # 这样中途取消或转写崩溃，磁盘上不会留下一份"带着占位符的 paper.md"。
        draft = await asyncio.to_thread(_parse_pdf_markdown, paper, source_path, source_url, markdown_path)
        if draft.warnings:
            return MarkdownConversion(warnings=draft.warnings)
        markdown_text, input_tokens, output_tokens = await transcribe_formula_regions(
            pdf_path=source_path,
            regions=draft.formulas,
            markdown_text=draft.markdown,
            blocks=draft.blocks,
            # 中文注释：开关关掉时传空值进去，转写那一步就不调模型，
            # 但占位符照样会被换成展平的兜底内容，正文不会留下记号。
            llm=llm if _formula_ocr_enabled() else None,
            on_progress=on_progress,
            raise_if_cancelled=raise_if_cancelled,
        )
        # 中文注释：公式换完之后再重排表格。两步都要改写正文，串着做最省事——
        # 各改各的占位符，互不干扰。
        markdown_text, table_input, table_output = await transcribe_table_regions(
            pdf_path=source_path,
            regions=draft.tables,
            markdown_text=markdown_text,
            blocks=draft.blocks,
            llm=llm,
            on_progress=on_progress,
            raise_if_cancelled=raise_if_cancelled,
        )
        return await asyncio.to_thread(
            _finalize_markdown,
            draft,
            markdown_text,
            markdown_path,
            input_tokens + table_input,
            output_tokens + table_output,
        )
    if suffix in {".html", ".htm"}:
        # 中文注释：HTML 读取、正文提取、Markdown 落盘同样都是阻塞型本地操作。
        # 处理方式和 PDF 保持一致，边界清楚，后面接异步阅读流程会更稳。
        return await asyncio.to_thread(_convert_html, paper, source_path, source_url, markdown_path)
    return MarkdownConversion(warnings=[_UNSUPPORTED_FULLTEXT_WARNING])


def _formula_ocr_enabled() -> bool:
    """看配置里"公式识别"这个开关开没开。

    中文注释：读配置本身出了意外时按"开"处理——这个开关的默认值是开，
    因为读不到配置就说"关"会让所有论文悄悄退回旧行为，反而更难发现。
    """

    try:
        return SystemConfig.load().read.formula_ocr
    except Exception:
        return True


def _markdown_output_path(source_path: Path) -> Path:
    """统一计算 Markdown 结果文件路径，避免不同入口各自拼路径。"""

    return source_path.parent / "paper.md"


def _load_cached_markdown(markdown_path: Path) -> MarkdownConversion | None:
    """读取已经生成好的 Markdown 缓存，没有缓存或缓存内容不合格时返回空值。

    中文注释：缓存要过两道检查。
    第一道看转换器版本：旧版本转出来的 paper.md 里没有表格、公式和图片，
    版本号对不上就删掉重转（这里只比版本号，不去猜内容新旧）。
    第二道看正文质量：旧版本可能把"落地页转出来的坏内容"写进过缓存，
    不合格的一律删掉，让流程重新转换原始文件再判一次，
    避免垃圾内容一直躺在缓存里被反复当成论文正文去精读。
    """

    if not _has_non_empty_file(markdown_path):
        return None
    try:
        cached_text = markdown_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if _read_markdown_header_text(cached_text).get("converter_version") != CONVERTER_VERSION:
        logger.warning(
            "历史 Markdown 缓存由旧版转换器生成，已删除并重新转换",
            extra={"markdown_path": str(markdown_path), "converter_version": CONVERTER_VERSION},
        )
        _delete_quietly(markdown_path)
        return None
    quality_problem = _check_fulltext_quality(cached_text)
    if quality_problem is not None:
        logger.warning(
            "历史 Markdown 缓存未通过正文质量检查，已删除并重新转换",
            extra={"markdown_path": str(markdown_path), "reason": quality_problem},
        )
        _delete_quietly(markdown_path)
        return None
    return MarkdownConversion(
        markdown_path=markdown_path,
        page_count=_read_page_count(markdown_path),
        # 中文注释：命中缓存和重新转换走的是同一个判据。
        # 之前这里一直是空值，导致"第二次以后被精读的论文"永远拿不到图片目录，
        # 而生产环境里绝大多数论文都是第二次以后才被精读的。
        assets_dir=_existing_assets_dir(markdown_path),
    )


def _delete_quietly(path: Path) -> None:
    """删除一个文件，删不掉也不报错。

    中文注释：删不掉不影响正确性——下次进到同一个判断还会再次把这份坏缓存拦下来。
    """

    try:
        path.unlink()
    except OSError:
        pass


def _read_markdown_header_text(markdown_text: str) -> dict[str, Any]:
    """读出 Markdown 开头那段用 --- 包起来的论文信息（里面存的是 JSON）。

    中文注释：老的缓存文件可能没有这段头部、或者头部的 JSON 被写坏了，
    这些情况一律当成"读不出信息"，返回一个空字典，由调用方决定怎么处理。
    """

    if not markdown_text.startswith("---"):
        return {}
    end = markdown_text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        parsed = json.loads(markdown_text[3:end].strip())
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _markdown_body_text(markdown_text: str) -> str:
    """砍掉开头那段用 --- 包起来的论文信息，只留下正文。

    中文注释：质量闸只看正文，不看论文信息。标题、DOI 这些元数据有多长，
    和"这段文字是不是论文正文"一点关系都没有；把它们算进去，
    会出现"同一份内容，换个标题就从拦住变成放行"这种说不清的结果。
    """

    if not markdown_text.startswith("---"):
        return markdown_text
    end = markdown_text.find("\n---", 3)
    if end < 0:
        return markdown_text
    return markdown_text[end + 4:]


def _has_non_empty_file(path: Path) -> bool:
    """判断已有 Markdown 缓存是否存在，并且里面确实有内容。"""

    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _check_fulltext_quality(markdown_text: str) -> str | None:
    """检查转换出来的正文像不像一篇真论文的正文，不合格时返回原因，合格返回 None。

    中文注释：能下载到不等于下对了——论文"介绍页/落地页"同样是 HTTP 200，
    也照样能被解析出文字。太短或乱码太多，说明拿到的不是论文正文。
    这里把坏内容拦下来之后，返回的转换结果里不带 markdown_path，
    上层精读流程看到"没有转换结果"就会自动降级为"摘要精读"，不需要新增分支。
    """

    # 中文注释：只量正文那一段。开头包着 JSON 的 --- 头部是论文信息，不属于正文。
    body = _markdown_body_text(markdown_text).strip()
    if len(body) < _MIN_FULLTEXT_CHARACTERS:
        return f"转换后的正文不足 {_MIN_FULLTEXT_CHARACTERS} 字符，下载到的可能只是论文介绍页而不是正文"
    garbled_characters = body.count("\ufffd")
    if garbled_characters / len(body) > _MAX_GARBLED_CHARACTERS_RATIO:
        return "转换后的正文乱码字符占比超过 1%，解析结果不可用"
    return None


def _convert_html(paper: PaperDocument, source_path: Path, source_url: str | None, markdown_path: Path) -> MarkdownConversion:
    """提取普通 HTML 页面中的正文、表格、公式和图片，生成 Markdown 文件。"""

    # 中文注释：把页面地址交给解析器，因为网页里的图片大多是相对地址
    # （比如 "2502.20217v1/model.png"），必须知道"这篇文章在哪个网址下"才能补成完整链接。
    parser = _ArticleHtmlParser(base_url=source_url)
    try:
        parser.feed(source_path.read_text(encoding="utf-8", errors="replace"))
        parser.close()
    except OSError as exc:
        return MarkdownConversion(warnings=[f"HTML 正文读取失败：{exc}"])
    article = parser.to_markdown()
    if not article.strip():
        return MarkdownConversion(warnings=["HTML 页面没有可读取的正文"])
    markdown_text = _markdown_header(paper, source_url, None) + "\n\n" + article.strip() + "\n"
    # 中文注释：写文件之前先过正文质量闸——太短或乱码的内容不落盘，
    # 避免坏内容混进缓存被反复使用。返回结果里没有 markdown_path 时，上层会自动降级为摘要精读。
    quality_problem = _check_fulltext_quality(markdown_text)
    if quality_problem is not None:
        logger.warning(
            "HTML 全文转换结果未通过正文质量检查",
            extra={"source_path": str(source_path), "reason": quality_problem},
        )
        return MarkdownConversion(warnings=[quality_problem])
    markdown_path.write_text(markdown_text, encoding="utf-8")
    return MarkdownConversion(
        markdown_path=markdown_path,
        # 中文注释：和 PDF 分支、和命中缓存的分支用同一个判据，三条路给出的结果一致。
        assets_dir=_existing_assets_dir(markdown_path),
    )


def _markdown_header(
    paper: PaperDocument,
    source_url: str | None,
    page_count: int | None,
    figures: list[dict[str, Any]] | None = None,
) -> str:
    """生成 Markdown 开头的论文基本信息，避免正文和来源信息分散保存。"""

    header = {
        # 中文注释：记下是哪个版本的转换器生成的这份 Markdown。
        # 以后解析规则再改，光看这个数字就知道缓存该不该重转。
        "converter_version": CONVERTER_VERSION,
        "paper_id": paper.id,
        "title": paper.title,
        "doi": paper.doi,
        "source_url": source_url or paper.url,
        "page_count": page_count,
    }
    # 中文注释：PDF 会带上从图块里抄出来的插图清单。没有这张清单时不写这个字段，
    # 网页全文仍然按正文里的图片引用去认。
    if figures is not None:
        header["figures"] = figures
    return "---\n" + json.dumps(header, ensure_ascii=False, indent=2) + "\n---"


def _figure_records(blocks: list[PageBlock]) -> list[dict[str, Any]]:
    """从图块里抄出插图清单：第几页、文件名、图注。"""

    records: list[dict[str, Any]] = []
    for block in blocks:
        if block.kind != "figure" or not block.asset_name:
            continue
        caption = ""
        match = re.search(r"!\[([^\]]*)\]", block.text)
        if match:
            caption = match.group(1).replace("\\]", "]")
        records.append({"page": block.page_number, "file": block.asset_name, "caption": caption})
    return records


def _read_page_count(markdown_path: Path) -> int | None:
    """从已有 Markdown 中读出 PDF 页数，缓存文件不完整时返回空值。"""

    try:
        page_count = _read_markdown_header_text(markdown_path.read_text(encoding="utf-8")).get("page_count")
    except OSError:
        return None
    return page_count if isinstance(page_count, int) else None


def _parse_pdf_markdown(
    paper: PaperDocument, source_path: Path, source_url: str | None, markdown_path: Path
) -> _PdfMarkdownDraft:
    """用可替换的 PDF 解析器把 PDF 读成 Markdown 草稿，但不写文件。

    中文注释：阅读节点只需要 Markdown，不应该关心 PDF 到底是 pypdf、PyMuPDF
    还是其它工具解析的。用哪个由配置里的 read.pdf_parser 决定，
    auto 表示"装了 PyMuPDF 就用 PyMuPDF"，PyMuPDF 能多提取出表格、公式和图片。

    为什么只是"草稿"、不在这里写文件——正文里的公式位置上留的是占位符，
    还要等模型把它换成 LaTeX。写盘统一放到 _finalize_markdown。
    """

    parser = get_pdf_parser(SystemConfig.load().read.pdf_parser)
    # 中文注释：PDF 里的图片不能塞进一个 Markdown 文件里，只能单独存成文件，
    # 放在 paper.md 旁边的 assets 目录，正文里用 assets/xxx.png 这样的相对路径引用。
    assets_dir = markdown_path.parent / "assets"
    parsed = parser.parse(source_path, assets_dir=assets_dir)
    if parsed.warnings:
        return _PdfMarkdownDraft(warnings=parsed.warnings)
    if not parsed.pages:
        return _PdfMarkdownDraft(warnings=["PDF 中没有可读取的正文"])
    body: list[str] = [_markdown_header(paper, source_url, len(parsed.pages), _figure_records(parsed.blocks))]
    for page in parsed.pages:
        body.extend([f"<!-- page: {page.page_number} -->", page.text])
    return _PdfMarkdownDraft(
        markdown="\n\n".join(body).strip() + "\n",
        page_count=len(parsed.pages),
        formulas=parsed.formulas,
        tables=parsed.tables,
        blocks=parsed.blocks,
    )


def _finalize_markdown(
    draft: _PdfMarkdownDraft,
    markdown_text: str,
    markdown_path: Path,
    input_tokens: int,
    output_tokens: int,
) -> MarkdownConversion:
    """把转写完成的正文过一遍质量闸，然后写进文件。

    中文注释：这是整条链路上唯一写 paper.md 的地方。到这里公式该转的已经转完、
    转不出来的也都换成了兜底内容，写下去的一定是一份完整成品。
    """

    # 中文注释：和 HTML 分支一样，写文件之前先过正文质量闸。
    # PDF 有可能每一页都解析成功、但抽出来的文字全是乱码（比如文件损坏），
    # 这种内容不能拿去精读，拦下来让上层降级为摘要精读。
    quality_problem = _check_fulltext_quality(markdown_text)
    if quality_problem is not None:
        logger.warning(
            "PDF 全文转换结果未通过正文质量检查",
            extra={"markdown_path": str(markdown_path), "reason": quality_problem},
        )
        return MarkdownConversion(warnings=[quality_problem])
    markdown_path.write_text(markdown_text, encoding="utf-8")
    return MarkdownConversion(
        markdown_path=markdown_path,
        page_count=draft.page_count,
        assets_dir=_existing_assets_dir(markdown_path),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        blocks=draft.blocks,
    )


def _existing_assets_dir(markdown_path: Path) -> Path | None:
    """返回 paper.md 旁边那个真的有图片的 assets 目录，没有就是空值。

    中文注释：不管是刚刚转出来的，还是直接读的缓存，都拿这一个函数判断，
    就不会出现"新转的有图片目录、读缓存的没有"这种前后不一致的情况。
    """

    assets_dir = markdown_path.parent / "assets"
    return assets_dir if _has_any_file(assets_dir) else None


def _has_any_file(directory: Path) -> bool:
    """判断目录里是不是真的有文件（目录不存在或读不了都算没有）。"""

    try:
        return any(entry.is_file() for entry in directory.iterdir())
    except OSError:
        return False


# 中文注释：脚本、样式、内嵌图形这三类标签里的内容是给浏览器看的，不是论文正文。
# 遇到它们就整段跳过，连里面的文字都不要。
_IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg"})

# 中文注释：标题、段落、列表项、引用这几种标签是正文的基本单位，每一个都会单独成段。
_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"})

# 中文注释：这几种标签一出现，就说明"新的一段 / 新的一张表 / 新的一张图开始了"。
# 它们出现时，如果表格格子或图注还开着口，说明网页漏写了 </td> 或 </figcaption>，
# 必须先把它们收尾，否则它们会继续把后面的正文吸进自己肚子里。
# 注意 <div> 只在这件事上算块级标签，它自己不攒文字（见下面 _TEXT_KINDS 的说明）：
# 网页顶栏底栏那些"登录 / 搜索 / 关注我们"之类的按钮文字全在 <div> 里，
# 让 <div> 攒文字会把一大堆跟论文无关的页面噪声灌进正文
# （实测 PMC 那个页面会因此多出 3082 字符，arXiv 页面多出 1730 字符）。
_BLOCK_STARTERS = _BLOCK_TAGS | frozenset({"div", "table", "figure"})

# 中文注释：只有这几种标签才往自己身上攒文字。文字永远归给"最里层"的那个，
# 这样 <td> 里套 <span> 时，文字算在表格单元格头上，不会跑到别处；
# 而 <div> 不在这里面，所以网页顶栏底栏里那些直接放在 <div> 中的按钮文字会被自然挡掉。
_TEXT_KINDS = frozenset({"block", "caption", "cell", "math"})

# 中文注释：arXiv 网页里的图片地址本来就带着论文编号（形如 "2502.20217v1/图.png"）。
# 这个正则用来认出"论文编号"这种形状的目录名，只在补全图片地址时用来去重。
_ARXIV_ID_PATTERN = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


def _span_count(raw: str | None) -> int:
    """读出 colspan / rowspan 写的是跨几格（没写或者写坏了都当成 1 格）。"""

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return value if value > 0 else 1


@dataclass
class _OpenTag:
    """记录一个还没闭合的标签。

    中文注释：网页的标签是一层套一层的（<figure> 套 <figcaption> 套 <span>，
    <table> 套 <tr> 套 <td>），只有把"当前打开了哪些标签"按顺序记下来，
    才知道一段文字到底该归给谁。kind 表示这个标签扮演什么角色，
    position 是它在网页里出现的顺序号，parts 用来攒文字，data 放各自的附加信息。
    """

    tag: str
    kind: str
    position: int = 0
    parts: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


class _ArticleHtmlParser(HTMLParser):
    """用标准库提取 HTML 正文，并尽量保住表格、公式和图片。

    中文注释：这里用"标签栈"来记状态（一个列表当栈用）：进来一个标签就压栈，
    碰到闭标签就弹栈。因为网页的嵌套层数不固定，只记"当前这一个标签"根本不够用，
    深层嵌套的内容会张冠李戴。

    输出的先后顺序不看"谁先弹栈"，只看每个片段在网页里的位置号。
    网页经常省略 </p> </li> </td> </tr> 这些结束标签（HTML5 允许这么写），
    靠弹栈顺序拼出来的正文会整段倒过来。
    """

    def __init__(self, base_url: str | None = None) -> None:
        """初始化标签栈、忽略计数和已经整理出的 Markdown 片段。"""

        super().__init__(convert_charrefs=True)
        # 中文注释：页面地址，用来把图片的相对地址补成完整网址。
        self._base_url = base_url or ""
        self._stack: list[_OpenTag] = []
        self._ignored_depth = 0
        # 中文注释：每输出一段正文，就连同它出现的位置号一起存进来，最后按位置号从小到大排。
        self._blocks: list[tuple[int, str]] = []
        # 中文注释：位置号是一个只增不减的计数器。每压一次栈就发一号，
        # 谁先出现谁号小，最后照着号排就能还原网页上的顺序。
        self._next_position = 0

    def _push(self, tag: str, kind: str, data: dict[str, Any] | None = None) -> _OpenTag:
        """压一个标签进栈，并给它发一个位置号。"""

        node = _OpenTag(
            tag=tag,
            kind=kind,
            position=self._next_position,
            data=data if data is not None else {},
        )
        self._next_position += 1
        self._stack.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """遇到开始标签时，判断它是正文、表格、公式、图片还是无关内容。"""

        lowered = tag.lower()
        attributes = {name.lower(): (value or "") for name, value in attrs}
        # 中文注释：脚本、样式这类整段忽略的标签，用计数记住要跳过几层嵌套；
        # 已经在忽略区域里的其它标签，也要压栈，保证弹栈时不会错位。
        if lowered in _IGNORED_TAGS or self._ignored_depth > 0:
            if lowered in _IGNORED_TAGS:
                self._ignored_depth += 1
            self._push(lowered, "ignored")
            return
        # 中文注释：<br> 在网页上看到的就是"这里断一下"，对应到文字里就是一个空格。
        # 不补这个空格，"word1<br>word2" 就会粘成一个词。
        if lowered == "br":
            self._append_space()
            return
        if lowered == "math":
            self._open_math(attributes)
            return
        if lowered == "img":
            self._remember_figure_image(attributes)
            return

        classes = attributes.get("class", "")

        # 中文注释：新的一段/一张表/一张图开始了。这时候要是表格格子或者图注还开着口，
        # 说明网页漏写了 </td>、</figcaption>，先把它们收尾，别让它们继续吸后面的正文。
        if lowered in _BLOCK_STARTERS:
            self._close_open_kinds(("cell", "caption"))

        enclosing_table = self._innermost("table")
        if enclosing_table is not None:
            if enclosing_table.data.get("equation"):
                # 中文注释：公式排版表（arXiv 把公式也排成 <table>）里的 <tr>/<td>
                # 只是排版用的格子，不当成数据表的行列，免得把整条公式拆成一张乱码表。
                self._push(lowered, "plain")
                return
            if lowered == "tr" or "ltx_tr" in classes:
                # 中文注释：上一行的最后一个格子可能没写 </td>，先收尾再开新行。
                self._close_open_kinds(("cell",))
                row = self._push(lowered, "row", {"cells": []})
                enclosing_table.data.setdefault("rows", []).append(row)
                return
            if lowered in {"td", "th"} or "ltx_td" in classes:
                # 中文注释：同一行里上一个格子可能也没写 </td>，先收尾。
                self._close_open_kinds(("cell",))
                cell = self._push(
                    lowered,
                    "cell",
                    {
                        "colspan": _span_count(attributes.get("colspan")),
                        "rowspan": _span_count(attributes.get("rowspan")),
                        "text": "",
                    },
                )
                row = self._innermost("row")
                if row is not None:
                    # 中文注释：格子按它出现的先后顺序记在行里。
                    # 最后是从左往右排的，所以顺序必须按"出现顺序"，不能按"闭合顺序"。
                    row.data.setdefault("cells", []).append(cell)
                return
            if lowered in _BLOCK_TAGS:
                # 中文注释：表格里偶尔也会夹着段落，照样当一段正文收集，免得文字白白丢掉。
                self._push(lowered, "block")
                return
            self._push(lowered, "plain")
            return

        # 中文注释：判断"这是不是一张数据表"，看的是里面有没有表格的行和格子，
        # 而不是看它有没有 ltx_tabular 这个名字。
        # 之前只认 class 里带 ltx_tabular 的表，别的 <table> 整张被丢掉，
        # 连里面的文字都不剩，还没有任何提示——非 arXiv 的论文全文页（比如 PMC）就是这么丢表的。
        if lowered == "table":
            self._push("table", "table", {"equation": "ltx_equation" in classes, "rows": []})
            return
        # 中文注释：arXiv 有的表连 <table> 都不写，直接拿 <span class="ltx_tabular"> 当表格用
        # （实测 2502.20217v1 的第三张表就是这样）。所以 class 这条路也得留着，不然那张表会整个丢掉。
        if "ltx_equation" in classes:
            self._push(lowered, "table", {"equation": True, "rows": []})
            return
        if "ltx_tabular" in classes:
            self._push(lowered, "table", {"equation": False, "rows": []})
            return

        if lowered == "figure":
            # 中文注释：一个 <figure> 里可能有一张图片和一段图注。图片地址和图注文字
            # 都先攒在这个标签身上，等它闭合时再一起输出，这样"图片在前、图注在后"的顺序能保证。
            self._push(lowered, "figure", {"images": [], "caption": ""})
            return
        if lowered == "figcaption":
            self._push(lowered, "caption")
            return
        if lowered in _BLOCK_TAGS:
            self._push(lowered, "block")
            return
        self._push(lowered, "plain")

    def handle_endtag(self, tag: str) -> None:
        """遇到结束标签时，把对应的内容整理成 Markdown 块。"""

        lowered = tag.lower()
        # 中文注释：网页里偶尔会漏写闭合标签。这里从栈顶往下找同名的那个标签，
        # 把中间没闭合的一起收尾，免得一个错位把后面所有内容都算错。
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == lowered:
                while len(self._stack) > index:
                    self._close_top()
                return

    def handle_data(self, data: str) -> None:
        """收集文字，只归给最里层那个正在攒文字的标签。"""

        if self._ignored_depth > 0:
            return
        for node in reversed(self._stack):
            if node.kind in _TEXT_KINDS:
                node.parts.append(data)
                return

    def to_markdown(self) -> str:
        """完成还没闭合的标签，返回拼接后的 Markdown 正文。"""

        while self._stack:
            self._close_top()
        # 中文注释：按位置号从小到大排，正文顺序就和网页上看到的一样了，
        # 不会因为网页漏写结束标签而整段倒过来。
        # 位置号相同的（比如一张图的图片引用和它的图注）保持原来的先后顺序。
        self._blocks.sort(key=lambda item: item[0])
        return "\n\n".join(text for _, text in self._blocks)

    def _close_top(self) -> None:
        """弹出一个标签，并按它扮演的角色输出内容。"""

        node = self._stack.pop()
        kind = node.kind
        if kind == "ignored":
            if node.tag in _IGNORED_TAGS and self._ignored_depth > 0:
                self._ignored_depth -= 1
            return
        if kind == "block":
            self._emit_block(node.tag, self._joined_text(node), node.position)
            return
        if kind == "caption":
            # 中文注释：图注文字先存到所属的 <figure> 上，等图闭合时和图片一起输出。
            figure = self._innermost("figure")
            if figure is not None:
                figure.data["caption"] = self._joined_text(node)
            return
        if kind == "cell":
            self._close_cell(node)
            return
        if kind == "table":
            self._emit_table(node)
            return
        if kind == "figure":
            self._emit_figure(node)
            return
        if kind == "math":
            self._emit_math(node)
            return

    def _close_open_kinds(self, kinds: tuple[str, ...]) -> None:
        """把栈里还没闭合的某几类标签收尾，连同它们里面套着的一起。

        中文注释：网页漏写 </td> 或 </figcaption> 时，这些标签会一直挂在栈上，
        把后面所有正文都吸进自己肚子里。这里在"新的一段开始了"的时候主动收尾。
        """

        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].kind in kinds:
                while len(self._stack) > index:
                    self._close_top()
                return

    def _close_cell(self, node: _OpenTag) -> None:
        """单元格闭合时，把里面的文字记在它自己身上。"""

        # 中文注释：单元格里的换行会把 Markdown 表格拆散，"|" 会被当成列分隔符，
        # 所以换行压成空格、竖线转义成 \|，表格才不会被撑破。
        # 至于这个格子排在行的第几列，前面开标签的时候就已经定好了，这里不动它。
        node.data["text"] = self._joined_text(node).replace("|", "\\|")

    def _innermost(self, kind: str) -> _OpenTag | None:
        """找出栈里最靠上的某一类标签。"""

        for node in reversed(self._stack):
            if node.kind == kind:
                return node
        return None

    def _joined_text(self, node: _OpenTag) -> str:
        """把一个标签里攒到的文字连起来，并把多余的空格和换行压平。"""

        return " ".join("".join(node.parts).split())

    def _emit(self, text: str, position: int) -> None:
        """记下一段要输出的正文，连同它在网页里的位置号。"""

        if not text:
            return
        self._blocks.append((position, text))

    def _emit_block(self, tag: str, text: str, position: int) -> None:
        """把一个段落/标题/列表项转换成 Markdown 块。"""

        if not text:
            return
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self._emit("#" * min(int(tag[1]), 4) + " " + text, position)
        elif tag == "li":
            self._emit("- " + text, position)
        elif tag == "blockquote":
            self._emit("> " + text, position)
        else:
            self._emit(text, position)

    def _append_space(self) -> None:
        """给当前正在攒文字的那一段补一个空格（<br> 用）。"""

        for node in reversed(self._stack):
            if node.kind in _TEXT_KINDS:
                node.parts.append(" ")
                return

    def _append_inline(self, text: str) -> None:
        """把一小段文字塞进当前正在攒的段落或单元格里。"""

        for node in reversed(self._stack):
            if node.kind in _TEXT_KINDS:
                node.parts.append(text)
                return
        # 中文注释：偶尔有公式直接写在 <div> 里，外面没有段落标签。这种情况也把它单独
        # 输出一段，总比整条公式丢掉强。位置号用"下一个还没发出去的号"，
        # 这样它会排在已经攒好的内容后面、排在还没出现的标签前面。
        self._emit(text, self._next_position)

    def _open_math(self, attributes: dict[str, str]) -> None:
        """处理 <math>：优先取 alttext 属性，那里面装的就是现成的 LaTeX 公式。"""

        # 中文注释：网页里的 alttext 是转义过的（比如 &lt; 表示小于号），要先还原回来。
        latex = html.unescape(attributes.get("alttext", "")).strip()
        is_block = attributes.get("display", "").strip() == "block"
        self._push("math", "math", {"latex": latex, "block": is_block})

    def _emit_math(self, node: _OpenTag) -> None:
        """把一条公式输出成 Markdown：独占一行的用 $$ 包，夹在句子里的用 $ 包。"""

        latex = str(node.data.get("latex") or "")
        if not latex:
            # 中文注释：极少数公式没有 alttext，就退一步拿它能显示出来的字符凑合，
            # 绝不自己去猜 LaTeX 该怎么写。
            latex = self._joined_text(node)
        if not latex:
            return
        if node.data.get("block"):
            self._emit(f"$$\n{latex}\n$$", node.position)
        else:
            self._append_inline(f"${latex}$")

    def _remember_figure_image(self, attributes: dict[str, str]) -> None:
        """只收集 <figure> 里面的图片。

        中文注释：页面顶上的 arXiv 标志、底部的赞助方图标、构建工具的小图标都是
        <img>，但它们不在 <figure> 里。只认 <figure> 里的图片，这些装饰图就自动被排除。
        """

        figure = self._innermost("figure")
        if figure is None:
            return
        source = attributes.get("src", "").strip()
        if not source:
            return
        figure.data["images"].append(self._resolve_image_url(source))

    def _resolve_image_url(self, source: str) -> str:
        """把图片地址补成完整网址。"""

        if not source:
            return source
        # 中文注释：本来就是完整网址的（http://... 或者 //例子.com/...）原样返回。
        # 这种地址再拿页面地址去拼、再去掉重复段落，只会把一个好好的网址改坏——
        # 比如 "https://arxiv.org/html/2502.20217v1/2502.20217v1/x.png" 会被削成 404 的地址。
        if urlparse(source).netloc or source.startswith("//"):
            return source
        if not self._base_url:
            return source
        return _collapse_repeated_path_segment(urljoin(self._base_url, source))

    def _emit_table(self, node: _OpenTag) -> None:
        """把一张数据表输出成 Markdown 表格，公式表则什么都不做。"""

        if node.data.get("equation"):
            # 中文注释：公式表格里的内容已经由 <math> 输出成 $$ 公式了，这里再输出一遍
            # 只会让每条公式变成一张乱码表格。
            return
        rows = _expand_table_grid(node.data.get("rows") or [])
        # 中文注释：门槛——真表格至少要有 2 个格子。整张表只有一个格子的，
        # 都是网页拿来排版的外壳（比如 PMC 把公式包在一个 <table> 里），不是论文的表格。
        if sum(len(row) for row in rows) < 2:
            return
        self._emit(_render_markdown_table(rows), node.position)

    def _emit_figure(self, node: _OpenTag) -> None:
        """输出一个 <figure>：先把图片引用排好，再跟一段图注文字。"""

        caption = str(node.data.get("caption") or "").strip()
        for url in node.data.get("images", []):
            if url:
                self._emit(_figure_markdown(caption, url), node.position)
        if caption:
            self._emit(caption, node.position)


def _expand_table_grid(rows: list[_OpenTag]) -> list[list[str]]:
    """把表格的行和格子摊成一个规整的方格，跨行跨列的位置补上空格子。

    中文注释：网页里的格子能横着跨几列（colspan）、竖着跨几行（rowspan）。
    要是"一个格子就当一格"直接排，跨行跨列后面的格子会整体往左移一格，
    数字就挂到了错误的列标题底下——这比整张表丢掉还危险，
    因为看起来是一张正常的表，没人会发现数字串了列。

    做法：从上到下、从左到右走一遍，给每个格子算出它真正落在第几列。
    被上面跨行格子占住的列，在这一行补一个空格子站住位置。
    """

    grid: list[list[str]] = []
    # 中文注释：记下"第几列被某个跨行格子占着，占到第几行为止（这一行也算）"。
    occupied_until: dict[int, int] = {}
    for row_index, row in enumerate(rows):
        cells = row.data.get("cells") or []
        if not cells:
            # 中文注释：一个格子都没有的 <tr> 没有内容，跳过。
            # 位置上它还占着一行，所以 row_index 照常往前走。
            continue
        current: dict[int, str] = {}
        column = 0
        for cell in cells:
            # 中文注释：先跳过上面跨行格子占住的列，这些列在这一行是空的。
            while occupied_until.get(column, -1) >= row_index:
                current[column] = ""
                column += 1
            colspan = int(cell.data.get("colspan") or 1)
            rowspan = int(cell.data.get("rowspan") or 1)
            current[column] = str(cell.data.get("text") or "")
            # 中文注释：横着跨几列，多出来的列补空格子。
            for extra in range(1, colspan):
                current[column + extra] = ""
            if rowspan > 1:
                for index in range(column, column + colspan):
                    occupied_until[index] = row_index + rowspan - 1
            column += colspan
        # 中文注释：行尾要是还被跨行格子占着，也要补空格子，不然这一行会比别人短。
        while occupied_until.get(column, -1) >= row_index:
            current[column] = ""
            column += 1
        width = max(current) + 1
        grid.append([current.get(index, "") for index in range(width)])
    # 中文注释：整行都是空字的不带任何信息（网页常拿它当分隔条），去掉。
    return [row for row in grid if any(cell.strip() for cell in row)]


def _render_markdown_table(rows: list[list[str]]) -> str:
    """把表格的行列拼成 GitHub 风格的 Markdown 表格。

    中文注释：每一行的竖线个数必须完全一样，多一个少一个都会让整张表错位。
    所以这里先量出最宽的一行有几列，再把每一行都补齐到这个列数，
    保证每一行的竖线数和表头行一模一样。
    """

    column_count = max(len(row) for row in rows)
    lines: list[str] = []
    for index, row in enumerate(rows):
        padded = list(row) + [""] * (column_count - len(row))
        lines.append("| " + " | ".join(padded) + " |")
        if index == 0:
            # 中文注释：Markdown 表格的第一行是表头，第二行必须是 |---|---| 这样的分隔行，
            # 少了分隔行，渲染出来就只是一堆带竖线的普通文字。
            lines.append("| " + " | ".join(["---"] * column_count) + " |")
    return "\n".join(lines)


def _figure_markdown(caption: str, url: str) -> str:
    """拼出 Markdown 的图片引用：![图注](图片地址)。"""

    # 中文注释：图注里的 "]" 会提前把方括号配对收尾，把整个引用写法弄坏，先转义掉。
    alt = caption.replace("]", "\\]") if caption else "Figure"
    return f"![{alt}]({url})"


def _collapse_repeated_path_segment(url: str) -> str:
    """去掉网址路径里连续重复的一段论文编号。

    中文注释：arXiv 网页里图片地址本身就带着论文编号（形如 "2502.20217v1/图.png"），
    如果拿"页面地址"去补全，很容易拼出 ".../2502.20217v1/2502.20217v1/图.png"
    这种编号写两遍的地址，点开就是 404。这里专挑"连着两段一模一样的论文编号"，
    把多出来的那一段删掉。只认论文编号这种形状，不会误伤正常的网址。
    """

    prefix, separator, path = url.partition("://")
    if not separator:
        prefix, separator, path = "", "", url
    collapsed: list[str] = []
    for segment in path.split("/"):
        if collapsed and collapsed[-1] == segment and _ARXIV_ID_PATTERN.match(segment):
            continue
        collapsed.append(segment)
    return prefix + separator + "/".join(collapsed)
