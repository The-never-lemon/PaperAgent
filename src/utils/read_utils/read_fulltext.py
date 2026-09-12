from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from src.paper_retrieval.models import PaperDocument
# 中文注释：引入项目统一的日志工具。正文质量闸拦下坏内容时记一条日志，
# 排查问题时能看到"哪一篇论文的全文被判为不合格、原因是什么"。
from src.utils import get_logger
from src.utils.read_utils.pdf_parsers import get_pdf_parser


logger = get_logger(__name__)

_UNSUPPORTED_FULLTEXT_WARNING = "暂不支持该全文文件格式"

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
    warnings: list[str] = field(default_factory=list)


# 中文注释：这里以前有一个"同步版全文解析入口"的兼容壳（convert_fulltext_to_markdown），
# 全仓已经没有调用方（所有地方都直接 await 下面的异步入口），按"要改就改干净"
# 的原则删掉，避免留着一份没人用、以后还要跟着维护的重复入口。
async def async_convert_fulltext_to_markdown(
    paper: PaperDocument,
    *,
    source_path: Path,
    source_url: str | None,
) -> MarkdownConversion:
    """异步全文转换入口，供后续 async 阅读流程直接调用。"""

    markdown_path = _markdown_output_path(source_path)
    # 中文注释：哪怕只是看缓存文件存不存在，本质上也是本地磁盘操作。
    # 这里照样放进 to_thread，避免异步阅读流程在高并发时被本地文件检查拖慢。
    cached = await asyncio.to_thread(_load_cached_markdown, markdown_path)
    if cached is not None:
        return cached

    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        # 中文注释：PDF 解析和 Markdown 写入都是最容易卡住主流程的本地重操作。
        # 这里只把真正重的那一小段丢进线程，不把整个阅读节点都包进线程里。
        return await asyncio.to_thread(_convert_pdf_with_parser, paper, source_path, source_url, markdown_path)
    if suffix in {".html", ".htm"}:
        # 中文注释：HTML 读取、正文提取、Markdown 落盘同样都是阻塞型本地操作。
        # 处理方式和 PDF 保持一致，边界清楚，后面接异步阅读流程会更稳。
        return await asyncio.to_thread(_convert_html, paper, source_path, source_url, markdown_path)
    return MarkdownConversion(warnings=[_UNSUPPORTED_FULLTEXT_WARNING])


def _markdown_output_path(source_path: Path) -> Path:
    """统一计算 Markdown 结果文件路径，避免不同入口各自拼路径。"""

    return source_path.parent / "paper.md"


def _load_cached_markdown(markdown_path: Path) -> MarkdownConversion | None:
    """读取已经生成好的 Markdown 缓存，没有缓存或缓存内容不合格时返回空值。

    中文注释：旧版本可能把"落地页转出来的坏内容"写进过缓存，所以缓存文件
    也要过一次质量闸。不合格的缓存直接删掉，让流程重新转换原始文件再判一次，
    避免垃圾内容一直躺在缓存里被反复当成论文正文去精读。
    """

    if not _has_non_empty_file(markdown_path):
        return None
    try:
        cached_text = markdown_path.read_text(encoding="utf-8")
    except OSError:
        return None
    quality_problem = _check_fulltext_quality(cached_text)
    if quality_problem is not None:
        logger.warning(
            "历史 Markdown 缓存未通过正文质量检查，已删除并重新转换",
            extra={"markdown_path": str(markdown_path), "reason": quality_problem},
        )
        try:
            markdown_path.unlink()
        except OSError:
            # 中文注释：删除失败也不影响正确性——下次进到这个函数还会再次拦下同一份坏缓存。
            pass
        return None
    return MarkdownConversion(markdown_path=markdown_path, page_count=_read_page_count(markdown_path))


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

    stripped = markdown_text.strip()
    if len(stripped) < _MIN_FULLTEXT_CHARACTERS:
        return f"转换后的正文不足 {_MIN_FULLTEXT_CHARACTERS} 字符，下载到的可能只是论文介绍页而不是正文"
    garbled_characters = stripped.count("\ufffd")
    if garbled_characters / len(stripped) > _MAX_GARBLED_CHARACTERS_RATIO:
        return "转换后的正文乱码字符占比超过 1%，解析结果不可用"
    return None


def _convert_html(paper: PaperDocument, source_path: Path, source_url: str | None, markdown_path: Path) -> MarkdownConversion:
    """提取普通 HTML 页面中的标题和段落，生成不含页码的 Markdown 文件。"""

    parser = _ArticleHtmlParser()
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
    return MarkdownConversion(markdown_path=markdown_path)


def _markdown_header(paper: PaperDocument, source_url: str | None, page_count: int | None) -> str:
    """生成 Markdown 开头的论文基本信息，避免正文和来源信息分散保存。"""

    header = {
        "paper_id": paper.id,
        "title": paper.title,
        "doi": paper.doi,
        "source_url": source_url or paper.url,
        "page_count": page_count,
    }
    return "---\n" + json.dumps(header, ensure_ascii=False, indent=2) + "\n---"


def _read_page_count(markdown_path: Path) -> int | None:
    """从已有 Markdown 中读出 PDF 页数，缓存文件不完整时返回空值。"""

    try:
        match = re.search(r'"page_count":\s*(\d+)', markdown_path.read_text(encoding="utf-8")[:1000])
        return int(match.group(1)) if match else None
    except OSError:
        return None


def _convert_pdf_with_parser(paper: PaperDocument, source_path: Path, source_url: str | None, markdown_path: Path) -> MarkdownConversion:
    """使用可替换的 PDF 解析器生成 Markdown。

    中文注释：阅读节点只需要 Markdown，不应该关心 PDF 到底是 pypdf、PyMuPDF
    还是其它工具解析的。这里先使用 pypdf 解析器，后续新增解析器时只需要改
    get_pdf_parser 的选择规则。
    """

    parser = get_pdf_parser("pypdf")
    parsed = parser.parse(source_path)
    if parsed.warnings:
        return MarkdownConversion(warnings=parsed.warnings)
    if not parsed.pages:
        return MarkdownConversion(warnings=["PDF 中没有可读取的正文"])
    body: list[str] = [_markdown_header(paper, source_url, len(parsed.pages))]
    for page in parsed.pages:
        body.extend([f"<!-- page: {page.page_number} -->", page.text])
    markdown_text = "\n\n".join(body).strip() + "\n"
    # 中文注释：和 HTML 分支一样，写文件之前先过正文质量闸。
    # PDF 有可能每一页都解析成功、但抽出来的文字全是乱码（比如文件损坏），
    # 这种内容不能拿去精读，拦下来让上层降级为摘要精读。
    quality_problem = _check_fulltext_quality(markdown_text)
    if quality_problem is not None:
        logger.warning(
            "PDF 全文转换结果未通过正文质量检查",
            extra={"source_path": str(source_path), "reason": quality_problem},
        )
        return MarkdownConversion(warnings=[quality_problem])
    markdown_path.write_text(markdown_text, encoding="utf-8")
    return MarkdownConversion(markdown_path=markdown_path, page_count=len(parsed.pages))


class _ArticleHtmlParser(HTMLParser):
    """用标准库提取常见 HTML 正文标签，避免额外引入网页解析依赖。"""

    def __init__(self) -> None:
        """初始化标签栈和已经整理出的 Markdown 片段。"""

        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._current_tag: str | None = None
        self._buffer: list[str] = []
        self._blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """遇到开始标签时记录正文标签，脚本和样式内容直接忽略。"""

        del attrs
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth == 0 and lowered in {"p", "h1", "h2", "h3", "h4", "li", "blockquote"}:
            self._flush_current()
            self._current_tag = lowered

    def handle_endtag(self, tag: str) -> None:
        """遇到结束标签时把已收集的段落写入结果列表。"""

        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"} and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return
        if self._ignored_depth == 0 and self._current_tag == lowered:
            self._flush_current()

    def handle_data(self, data: str) -> None:
        """只收集正文标签内的文字，避免把导航菜单等内容写入论文正文。"""

        if self._ignored_depth == 0 and self._current_tag is not None:
            self._buffer.append(data)

    def to_markdown(self) -> str:
        """完成最后一个未闭合段落，并返回拼接后的 Markdown 正文。"""

        self._flush_current()
        return "\n\n".join(self._blocks)

    def _flush_current(self) -> None:
        """将当前标签中的文字转换成简单 Markdown 块，并清空临时缓存。"""

        if self._current_tag is None:
            return
        text = " ".join("".join(self._buffer).split())
        tag = self._current_tag
        self._buffer = []
        self._current_tag = None
        if not text:
            return
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self._blocks.append("#" * min(int(tag[1]), 4) + " " + html.unescape(text))
        elif tag == "li":
            self._blocks.append("- " + html.unescape(text))
        elif tag == "blockquote":
            self._blocks.append("> " + html.unescape(text))
        else:
            self._blocks.append(html.unescape(text))
