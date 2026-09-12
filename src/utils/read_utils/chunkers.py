from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.paper_retrieval.models import PaperDocument
from src.utils import get_logger


logger = get_logger(__name__)


JsonObject = dict[str, Any]


# 中文注释：分块规则的版本号。只要这个数字变了，说明"切分方式"改了，之前存下来的
# chunk.json 就不再可信，必须重新切一次。判断缓存能不能复用时全靠它。
CHUNKER_VERSION = 4

# 中文注释：下面这几条正则干的是"认路"的活。分块时要靠它们认出哪些行是表格、
# 哪些是公式、哪些是图片引用，只有这样才知道刀该落在哪儿。
_TABLE_LINE_RE = re.compile(r"^\s*\|")  # 表格的每一行都以竖线开头
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")  # 形如 |---|---| 的分隔行
_MATH_BLOCK_RE = re.compile(r"\$\$")  # 块级公式的 $$ 记号
_MATH_INLINE_RE = re.compile(r"(?<!\$)\$(?!\$)[^$\n]+\$(?!\$)")  # 行内公式 $...$
_FIGURE_RE = re.compile(r"!\[")  # Markdown 图片写法 ![图注](地址)
# 中文注释：页码标记，一行独占，转 PDF 时写在每一页的开头。
_PAGE_MARKER_RE = re.compile(r"^\s*<!--\s*page:\s*\d+\s*-->\s*$")
# 中文注释：图注行，形如 "Figure 1: ..."、"Fig. 3 ..."、"Table 2 ..."、"图 1 ..."。
# 只认"关键字后面紧跟着编号"这种写法，像 "IEEE ROBOTICS AND AUTOMATION LETTERS"
# 这种真正的页眉不会被误认成图注，该删的重复页眉照样删得掉。
_CAPTION_RE = re.compile(r"^\s*(?:figure|fig\.?|table|图)\s*\d+", re.IGNORECASE)

# 中文注释：判断一段文字是不是"没内容的碎片"（页码残留之类）的长度门槛。
# 正文碎片一定会出现字母（中文汉字也算字母），而清理页眉页脚之后剩下的纯数字碎片
# 基本都是页码。实测一篇真实论文 37 个片段里有 4 个是 '2' / '12' / '15' / '6'
# 这种孤立页码——每个照样花掉一次模型调用（约 11% 的调用白费），还会把"内容不完整"
# 的噪声混进汇总材料。门槛卡得很死（既短、又一个字母都没有），所以不会误伤表格里的
# 数字行：表格行总是和带字的表头待在同一个片段里。
_MAX_JUNK_CHARACTERS = 20


@dataclass(slots=True)
class TextChunk:
    """保存一个供阅读和向量化使用的正文片段。

    中文注释：chunk_id 会写进 LLM 提示词，模型提取结论时必须带上它，例如
    “方法使用对比学习[paper_001:p0003]”。previous_chunk_id 和 next_chunk_id
    让后续节点能知道相邻片段是谁。
    """

    chunk_id: str
    paperId: str
    chunk_index: int
    content: str
    page_start: int | None = None
    page_end: int | None = None
    section: str = ""
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        """把切片转成可以写入 chunk.json 的普通字典。"""

        return {
            "chunkId": self.chunk_id,
            "paperId": self.paperId,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section": self.section,
            "previous_chunk_id": self.previous_chunk_id,
            "next_chunk_id": self.next_chunk_id,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ChunkBuildResult:
    """保存分块结果和 chunk.json 的位置。"""

    chunks_path: Path
    chunks: list[TextChunk]


class BaseChunker(ABC):
    """正文分块基类。

    中文注释：不同切分方式只需要实现 chunk()。阅读节点不用关心它按页、按章节
    还是按其它规则切。
    """

    name = "base"

    @abstractmethod
    def chunk(self, paper: PaperDocument, markdown: str) -> list[TextChunk]:
        """把 Markdown 正文切成较小片段。"""


class PageChunker(BaseChunker):
    """按 PDF 页码切分 Markdown 的最简单策略。"""

    name = "page"
    max_chunk_characters = 1200
    # 中文注释：表格和公式是"整体"——中间任何一刀切下去，剩下的内容都会变成
    # 看不懂的符号。所以给它们单独定一个更宽松的上限：只要一整张表（或一整个
    # 公式块）没超过 4000 字，就整块放进一个片段里，宁可让这个片段超过 1200 字。
    max_atomic_characters = 4000

    def chunk(self, paper: PaperDocument, markdown: str) -> list[TextChunk]:
        """按 `<!-- page: N -->` 标记切分正文。

        中文注释：read_fulltext.py 转 PDF 时会把页码标记写进 Markdown。这里优先
        使用这些标记；如果遇到 HTML 或没有页码的文本，就退回成一个普通片段。
        """

        paper_id = str(paper.paperId or paper.id)
        parts = _split_by_page_marker(markdown)
        if not parts:
            parts = [{"page": None, "content": _remove_front_matter(markdown)}]
        chunks: list[TextChunk] = []
        # 中文注释：跨页的长表格，转换器不一定会把表头在续页再写一遍。这里记住上一页
        # 最后那张表的表头两行（标题行 + |---| 分隔行），下一页接着写的时候补回去，
        # 免得第二页的片段一开头就是一堆没有表头的数据行，下游不知道哪列是哪列。
        last_table_header: list[str] = []
        for index, part in enumerate(parts):
            content = str(part.get("content") or "").strip()
            if not content:
                continue
            if last_table_header and _starts_with_headerless_table(content):
                # 中文注释：补表头这件事必须放在"这一页超没超过 1200 字"的判断之前，
                # 因为超长和不超长走的是两条不同的切分路径，两条都得能拿到表头。
                # 注意是用一个换行接上，不能空行——空行会把表格拦腰断开。
                content = "\n".join(last_table_header) + "\n" + content
            last_table_header = _trailing_table_header(content)
            page = part.get("page")
            page_number = int(page) if isinstance(page, int) else None
            if len(content) > self.max_chunk_characters:
                segments = _split_long_content(content, self.max_chunk_characters, self.max_atomic_characters)
                for segment_index, page_chunk in enumerate(segments, start=1):
                    if _is_junk_content(page_chunk):
                        _log_dropped_junk(paper_id, page_number, page_chunk)
                        continue
                    page_prefix = f"{paper_id}:p{page_number:04d}" if page_number is not None else f"{paper_id}:c{index:04d}"
                    # 中文注释：这一行的章节名以前写的是乱码"姝ｆ枃"——它是"正文"两个字
                    # 在编码弄错之后变成的样子，会原样写进 chunk.json 给后面的模型和用户看。
                    # 现已改成正确的"正文"，和下面"整页不切分"分支的写法保持一致。
                    chunks.append(
                        TextChunk(
                            chunk_id=f"{page_prefix}:s{segment_index:04d}",
                            paperId=paper_id,
                            chunk_index=len(chunks),
                            content=page_chunk,
                            page_start=page_number,
                            page_end=page_number,
                            section=f"page_{page_number}" if page_number is not None else "正文",
                            metadata=_chunk_metadata(self.name, page_chunk),
                        )
                    )
                continue
            if _is_junk_content(content):
                _log_dropped_junk(paper_id, page_number, content)
                continue
            chunk_id = f"{paper_id}:p{page_number:04d}" if page_number is not None else f"{paper_id}:c{index:04d}"
            chunks.append(
                TextChunk(
                    chunk_id=chunk_id,
                    paperId=paper_id,
                    chunk_index=len(chunks),
                    content=content,
                    page_start=page_number,
                    page_end=page_number,
                    section=f"page_{page_number}" if page_number is not None else "正文",
                    metadata=_chunk_metadata(self.name, content),
                )
            )
        _attach_neighbors(chunks)
        return chunks


def _is_junk_content(content: str) -> bool:
    """判断一段文字是不是"没有实际内容的碎片"，是的话就别送去精读了。

    中文注释：转换 PDF 时会先删掉每页重复的页眉页脚，删完偶尔会剩下一两个字符——
    最常见的就是页码。这种碎片单看长度不为零，但里面一个字母都没有。它送去精读
    只会白花一次模型调用，还会让汇总阶段拿到"该片段内容不完整，无法提取信息"这种噪声。
    所以直接丢掉，并记一条日志，免得"内容悄悄少了"没人知道。
    """

    stripped = content.strip()
    if len(stripped) > _MAX_JUNK_CHARACTERS:
        return False
    # 中文注释：isalpha() 对汉字同样返回真，所以中文正文不会被误判。
    return not any(character.isalpha() for character in stripped)


def _log_dropped_junk(paper_id: str, page_number: int | None, content: str) -> None:
    """记一条日志：某个片段因为"里面没有实际内容"被丢掉了。

    中文注释：故意不静默丢弃——"内容变少了"这种事必须留痕，否则以后有人发现
    片段数对不上，只能靠猜。
    """

    logger.info(
        "丢弃没有实际内容的正文碎片（多半是清理页眉页脚后剩下的页码）",
        extra={"paper_id": paper_id, "page": page_number, "content": content[:40]},
    )


def _starts_with_headerless_table(content: str) -> bool:
    """判断一页的内容是不是"接着上一页的表格往下写"、但自己没带表头。

    中文注释：两个条件同时成立才算数：
    1. 跳过开头的空行之后，第一行就是表格行（以竖线开头）；
    2. 紧接着的第二行不是 |---|---| 这种分隔行——有分隔行说明这一页本来就有表头，
       再补一遍反而会多出一段假表头。
    """

    lines = [line for line in content.splitlines() if line.strip()]
    if not lines or not _TABLE_LINE_RE.match(lines[0]):
        return False
    return not (len(lines) >= 2 and _TABLE_SEPARATOR_RE.match(lines[1]))


def _trailing_table_header(content: str) -> list[str]:
    """记下这一页最后一段表格的表头两行，留给下一页的续接表格用。

    中文注释：只有在表格确实带表头（第二行是 |---|---| 分隔行）时才返回那两行；
    如果这段表格自己就没有表头，就返回空——把一段数据行当表头记下来，
    下一页会被补得更乱。
    """

    lines = content.splitlines()
    end = -1
    for index in range(len(lines) - 1, -1, -1):
        if _TABLE_LINE_RE.match(lines[index]):
            end = index
            break
    if end < 0:
        return []
    start = end
    while start > 0 and _TABLE_LINE_RE.match(lines[start - 1]):
        start -= 1
    header = _table_header_rows(lines[start : end + 1])
    return header if len(header) == 2 else []


def build_chunks_file(
    paper: PaperDocument,
    *,
    markdown_path: Path,
    chunks_path: Path | None = None,
    chunker: BaseChunker | None = None,
) -> ChunkBuildResult:
    """读取 Markdown，清理非正文内容后切分，并写入 chunk.json。

    中文注释：这个文件是后续全文提取和向量化共同使用的“同一份上下文”。这样
    extraction.json 里引用的 chunkId，和向量库里的 chunkId 可以保持一致。
    """

    output_path = chunks_path or markdown_path.parent / "chunk.json"
    cached = load_chunks_file(output_path)
    # 中文注释：什么时候能直接拿老的 chunk.json 来用？两个条件都满足才行：
    # 一是它记录的切分规则版本号跟现在的 CHUNKER_VERSION 一样（不一样说明切法变了，
    # 老结果可能是被硬切过的）；二是里面每个片段都没超过"表格/公式整块保留"的上限
    # （只要有一个超了，就说明这可能是被从中切开的半张表，得重新切）。
    if (
        cached
        and _read_chunks_version(output_path) == CHUNKER_VERSION
        and all(len(chunk.content) <= PageChunker.max_atomic_characters for chunk in cached)
    ):
        return ChunkBuildResult(chunks_path=output_path, chunks=cached)
    markdown = markdown_path.read_text(encoding="utf-8")
    resolved_chunker = chunker or PageChunker()
    # 中文注释：PDF 转换出的 Markdown 往往混有每页重复的页眉、页脚、摘要和参考文献。
    # 这些内容会干扰模型判断，所以切分前先只留下论文正文。
    body_markdown = preprocess_markdown_body(markdown)
    chunks = resolved_chunker.chunk(paper, body_markdown)
    payload = {
        "paperId": paper.paperId or paper.id,
        "chunker": resolved_chunker.name,
        # 中文注释：把切分规则的版本号一起存进去，下次打开这个文件就知道该不该复用。
        "version": CHUNKER_VERSION,
        "chunks": [chunk.to_dict() for chunk in chunks],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return ChunkBuildResult(chunks_path=output_path, chunks=chunks)


def _read_chunks_version(chunks_path: Path) -> int | None:
    """读出 chunk.json 里记录的切分规则版本号，文件不在或格式不对就返回 None。"""

    try:
        payload = json.loads(chunks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    return version if isinstance(version, int) else None


def _chunk_metadata(chunker_name: str, content: str) -> JsonObject:
    """拼出一个片段要写进 chunk.json 的附加信息。

    中文注释：除了记录是用哪个分块器切的，还额外打三个"标签"，告诉下游这段文字
    里有没有表格、公式、图片，方便后面的精读和向量化按需处理。
    """

    metadata: JsonObject = {"chunker": chunker_name}
    metadata.update(_content_flags(content))
    return metadata


def _content_flags(content: str) -> JsonObject:
    """检查一段文字里有没有表格、公式和图片。

    中文注释：判断办法很土但够用——表格看行首的竖线，公式看 $$ 或者成对的单个 $，
    图片看 ![ 开头。
    """

    return {
        "has_table": any(_TABLE_LINE_RE.match(line) for line in content.splitlines()),
        "has_math": bool(_MATH_BLOCK_RE.search(content) or _MATH_INLINE_RE.search(content)),
        "has_figure": bool(_FIGURE_RE.search(content)),
    }


def _split_long_content(content: str, max_characters: int, max_atomic_characters: int) -> list[str]:
    """把过长的正文切成若干小段，同时保证表格和公式不被拦腰截断。

    中文注释（为什么要这么麻烦）：以前的做法是每 1200 个字硬切一刀，一刀下去
    表格被切成两半、公式只剩半截，模型读到的就是一堆看不懂的乱码。现在改成：
    先尽量在空行（段落分界）处下刀；碰到 Markdown 表格或者 $$ 公式块时，只要
    整块没超过 max_atomic_characters，就保证"这一块绝不被切开"——但它不一定
    要独占一段，能跟前后正文挤进同一段就一起放着，这样才不至于把一页正文切得
    七零八落（一页里有几十个公式块时，让每个公式块都独占一段会切出上百个碎片，
    精读时要多跑上百次模型）。只有连整块都超过 max_atomic_characters 时，才按
    表格的行边界切开，并且给后面每一段都补上表头，保证下游不会读到"半张表"。
    """

    pieces: list[str] = []
    buffer: list[str] = []  # 正在攒的这一段的若干行
    buffer_length = 0

    def flush() -> None:
        """把攒在 buffer 里的内容打包成一个片段。"""

        nonlocal buffer, buffer_length
        text = "\n".join(buffer).strip()
        if text:
            pieces.append(text)
        buffer = []
        buffer_length = 0

    def put(line: str) -> None:
        """往 buffer 里加一行普通正文，放不下就先把已有的打包。"""

        nonlocal buffer_length
        if buffer and buffer_length + len(line) + 1 > max_characters:
            flush()
        buffer.append(line)
        buffer_length += len(line) + 1

    def append_block(lines: list[str]) -> None:
        """把一整个表格/公式块原样塞进 buffer，不切开它。

        中文注释：只在块的前面补一个空行。Markdown 里表格和 $$ 公式块紧贴着上一段
        正文时可能渲染不出来，留白是让下游看到的内容仍然是一张完整的表、一个完整的公式。
        块后面不补空行——片段末尾的空行最后会被 strip 去掉，补了也留不住，与其写个
        和实际不符的注释，不如就说清楚只补前面这一个。
        """

        nonlocal buffer_length
        if buffer:
            buffer.append("")
            buffer_length += 1
        buffer.extend(lines)
        buffer_length += sum(len(line) + 1 for line in lines)

    for kind, block_lines in _iter_content_blocks(content, max_atomic_characters):
        if kind == "plain":
            for paragraph in _split_blank_lines(block_lines):
                paragraph_length = len("\n".join(paragraph))
                if paragraph_length <= max_characters:
                    if buffer and buffer_length + paragraph_length + 2 > max_characters:
                        flush()
                    for line in paragraph:
                        put(line)
                    continue
                # 中文注释：这一整段本来就超长，只能先打包掉手头的文字，再单独处理它。
                flush()
                pieces.extend(_split_long_paragraph(paragraph, max_characters))
            continue

        block_text = "\n".join(block_lines).strip()
        if not block_text:
            continue
        if len(block_text) <= max_atomic_characters:
            # 中文注释：整块没超上限，就保证不切开。装得下当前这段就一起装，
            # 装不下就先把当前这段打包，让它从这里另起一段。
            if buffer and buffer_length + len(block_text) + 2 > max_characters:
                flush()
            append_block(block_lines)
            continue

        # 中文注释：整块连 4000 字都超了，只能切开，这时才不得不打断它。
        # 表格块走 _split_long_table；公式块走 _split_long_math——它自己会先用
        # _TABLE_LINE_RE 扫一遍，块里其实夹着表格的话，也会按表格的规矩切、每片补表头。
        flush()
        if kind == "math":
            pieces.extend(_split_long_math(block_lines, max_characters, max_atomic_characters))
        else:
            pieces.extend(_split_long_table(block_lines, max_characters, max_atomic_characters))
    flush()
    return [piece for piece in pieces if piece]


def _iter_content_blocks(content: str, max_atomic_characters: int) -> list[tuple[str, list[str]]]:
    """把正文按"表格 / 公式 / 普通文字"切成一段一段。

    中文注释：返回的每一项是 (类型, 行列表)，类型只会是 table、math、plain 三种。
    先分清每一行归谁管，后面才知道刀能落在哪儿——绝不能在表格行或者 $$ 公式块
    之间下刀。
    """

    lines = content.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if _TABLE_LINE_RE.match(lines[index]):
            # 连续的表格行算作同一张表
            start = index
            while index < len(lines) and _TABLE_LINE_RE.match(lines[index]):
                index += 1
            blocks.append(("table", lines[start:index]))
            continue
        if stripped.startswith("$$"):
            start = index
            index += 1
            if stripped.count("$$") < 2:
                # 中文注释：这一行只是公式的开头，要往下找配对的结束 $$。往下找多远
                # 才放弃？放宽到"整块保留上限"的 10 倍（4 万字）——不能就卡在 4000，
                # 因为一个长公式块本身完全可能超过 4000 字，那种块是要切开、每片补回
                # $$ 处理的（见 _split_long_math），卡在 4000 就再也认不出它们。
                # 真正兜底的是"扫到底也没找到配对的 $$，就只把这一行当公式"这条规则。
                scan_limit = max_atomic_characters * 10
                scanned = len(lines[start]) + 1
                probe = index
                close_index = -1
                while probe < len(lines) and scanned <= scan_limit:
                    if "$$" in lines[probe]:
                        close_index = probe
                        break
                    scanned += len(lines[probe]) + 1
                    probe += 1
                if close_index >= 0:
                    index = close_index + 1  # 把写着结束 $$ 的那一行也算进公式块
                # 中文注释：扫到底都没找到配对的 $$，index 就停在 start + 1，
                # 也就是"只把这一行当公式"。后面的内容一点都不会少——绝不能从这里
                # 把这一页后面所有东西一口吞掉：那样后面的表格会被当成"超长的公式"
                # 按行切碎，每一段都没有表头，全变成半张表。
            blocks.append(("math", lines[start:index]))
            continue
        start = index
        while index < len(lines):
            current = lines[index]
            if _TABLE_LINE_RE.match(current) or current.strip().startswith("$$"):
                break
            index += 1
        blocks.append(("plain", lines[start:index]))
    return blocks


def _split_blank_lines(lines: list[str]) -> list[list[str]]:
    """按空行把若干行文字切成一个个段落（空行 = 只用来分段，本身不保留）。"""

    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    return paragraphs


def _split_long_paragraph(lines: list[str], max_characters: int) -> list[str]:
    """一个段落实在太长时，先按行切；某一行本身就超长，才在行中间硬切。"""

    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        if len(line) > max_characters:
            if current:
                pieces.append("\n".join(current).strip())
                current = []
                length = 0
            pieces.extend(_hard_split(line, max_characters))
            continue
        if current and length + len(line) + 1 > max_characters:
            pieces.append("\n".join(current).strip())
            current = []
            length = 0
        current.append(line)
        length += len(line) + 1
    if current:
        pieces.append("\n".join(current).strip())
    return [piece for piece in pieces if piece]


def _hard_split(text: str, max_characters: int) -> list[str]:
    """实在找不到可以下刀的位置时，才按字数硬切。"""

    pieces = [
        text[start : start + max_characters].strip()
        for start in range(0, len(text), max_characters)
        if text[start : start + max_characters].strip()
    ]
    # 中文注释：最后一片只剩几十个字时，让它单独占一个片段很浪费——精读要为这个
    # 几乎没内容的片段多跑一次模型。直接并回上一片。两片加起来离 4000 字的上限
    # 还差得远，不会把片段撑到超过上限。
    if len(pieces) >= 2 and len(pieces[-1]) < 50:
        pieces[-2] = pieces[-2] + pieces[-1]
        pieces.pop()
    return pieces


def _strip_outer_math_marks(lines: list[str]) -> list[str]:
    """把公式块最外面那对单独的 $$ 记号摘掉，只留下里面的公式内容。

    中文注释：只认"整行就是一个 $$"这种写法（开头一行、结尾一行），这是转换器
    写出来的标准样子。整条公式写在同一行里的 $$x=1$$ 也顺手把两头的记号摘掉。
    摘掉是为了重新切分之后能给每一片套上一对新的 $$（见 _split_long_math）。
    """

    body = list(lines)
    if body and body[0].strip() == "$$":
        body = body[1:]
    if body and body[-1].strip() == "$$":
        body = body[:-1]
    if len(body) == 1:
        single = body[0].strip()
        if single.startswith("$$") and single.count("$$") >= 2:
            inner = single[2:]
            if inner.endswith("$$"):
                inner = inner[:-2]
            body = [inner]
    return body


def _split_long_math(lines: list[str], max_characters: int, max_atomic_characters: int) -> list[str]:
    """把一个超过上限的 $$ 公式块切开，并保证每一片自身都是一对完整的 $$。

    中文注释（为什么必须这么切）：公式块从中间切开以后，一片可能只剩半条公式、
    另一片开头没有 $$，模型读到的是一堆看不懂的符号。所以先把最外面那对 $$ 摘掉，
    再把里面的内容切开，最后给每一片都套上一对新的 $$——这样每一片单独拿出来
    都是一个能渲染的完整公式块。

    中文注释：这个块虽然被判成了公式，里面却可能是表格（未配对的 $$、公式里夹着
    一张表，都会让判定出错）。所以先用 _TABLE_LINE_RE 扫一遍：确实有表格行就按
    表格的规矩切，每一片都补上表头；什么都没有才退化成按行硬切。
    """

    body = _strip_outer_math_marks(lines)
    if any(_TABLE_LINE_RE.match(line) for line in body):
        # 中文注释：要套在外面的两行 $$ 加上换行一共占 6 个字符，先把它留出来，
        # 免得套完 $$ 之后每一片的长度又超过上限。
        pieces = _split_long_table(body, max_characters - 6, max_atomic_characters)
    else:
        pieces = _split_long_paragraph(body, max(1, max_characters - 6))
    return [f"$$\n{piece}\n$$" for piece in pieces if piece.strip()]


def _split_long_table(lines: list[str], max_characters: int, max_atomic_characters: int) -> list[str]:
    """超长表格按行边界切开，并给每一段都补上表头。

    中文注释：表格切开以后，如果第二段只有数据行、没有表头，下游看到的就是
    "一堆数字不知道哪列是哪列"。所以每一段开头都重复一遍表头（标题行 + |---| 分隔行）。
    """

    # 中文注释：先数清楚这张表有几列，等会儿切开超宽的单行时要按这个列数补齐。
    column_count = next((_row_column_count(line) for line in lines if _TABLE_LINE_RE.match(line)), 1)
    header_lines = _table_header_rows(lines)
    header_length = len("\n".join(header_lines)) + 1
    body_rows: list[str] = []
    for line in lines[len(header_lines) :]:
        if header_length + len(line) + 1 <= max_atomic_characters:
            # 中文注释：单行没超上限就整行原样留着，哪怕它比 1200 字的常规上限长。
            # 宁可让这一片长一点，也不能把一行表格切成碎片——"保住这张表"比
            # "不超过 1200 字"重要。留的底线是：补上表头之后整体仍然不超过 4000 字，
            # 否则片段会超过"整块保留"的上限，chunk.json 的缓存判断就永远失效了。
            body_rows.append(line)
            continue
        # 中文注释：单行超过 4000 字（一个单元格里塞了几千字，极少见），只能切开。
        # 切开时每一片都重新补上竖线、按列数补齐，保证每一片还是一行合法表格。
        body_rows.extend(_split_wide_table_row(line, max_characters, column_count))
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for row in body_rows:
        if current and header_length + length + len(row) + 1 > max_characters:
            pieces.append("\n".join(header_lines + current).strip())
            current = []
            length = 0
        current.append(row)
        length += len(row) + 1
    if current:
        pieces.append("\n".join(header_lines + current).strip())
    if not pieces:
        pieces.append("\n".join(header_lines).strip())
    return [piece for piece in pieces if piece]


def _table_header_rows(rows: list[str]) -> list[str]:
    """认出表格的头两行：标题行，以及形如 |---|---| 的分隔行。"""

    if not rows:
        return []
    if len(rows) >= 2 and _TABLE_SEPARATOR_RE.match(rows[1]):
        return [rows[0], rows[1]]
    return [rows[0]]


def _row_column_count(line: str) -> int:
    """数一数一行表格有几个格子。

    中文注释：单元格里的竖线在转换时已经被转义成 \\|，它不算列分隔符，所以这里
    只数"前面没有反斜杠的竖线"。
    """

    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return max(len(re.split(r"(?<!\\)\|", body)), 1)


def _split_wide_table_row(line: str, max_characters: int, column_count: int) -> list[str]:
    """把一行超宽的表格切成几片，每一片仍然是"列数对齐的一行表格"。

    中文注释：直接按字数硬切，切出来的碎片就没有竖线了——下游既看不出这是表格，
    也不知道哪一段属于哪一列，可 metadata 里还写着 has_table=true，完全对不上。
    所以这里切完之后，每一片都把竖线补回来、按原来的列数补齐空格，让每一片
    单独拿出来仍然是一行格式正确的表格。
    """

    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    body = body.strip()
    # 中文注释：每一片外面要套成 "| 内容 |  |  |"，先把它占掉的字符数留出来，
    # 这样切出来的每一片加上竖线之后仍然不超过 max_characters。
    budget = max(1, max_characters - (3 * column_count + 1))
    pieces: list[str] = []
    for start in range(0, len(body), budget):
        text = body[start : start + budget].strip()
        if not text:
            continue
        # 中文注释：切出来的文字里可能夹着竖线，直接拼进去会被当成多出来的一列，
        # 这一行就不合法了。竖线在 Markdown 表格里要写成 \|，这里照规矩转义。
        text = re.sub(r"(?<!\\)\|", r"\\|", text)
        cells = [text] + [""] * (column_count - 1)
        pieces.append("| " + " | ".join(cells) + " |")
    return pieces


async def async_build_chunks_file(
    paper: PaperDocument,
    *,
    markdown_path: Path,
    chunks_path: Path | None = None,
    chunker: BaseChunker | None = None,
) -> ChunkBuildResult:
    """异步流程里的分块入口，把本地文件读写放到线程里执行。"""

    return await asyncio.to_thread(
        build_chunks_file,
        paper,
        markdown_path=markdown_path,
        chunks_path=chunks_path,
        chunker=chunker,
    )


def load_chunks_file(chunks_path: Path) -> list[TextChunk]:
    """读取已有 chunk.json。

    中文注释：缓存命中时直接复用，避免同一篇论文反复切分，也避免 chunk_id 在
    不同运行里发生变化。
    """

    try:
        payload = json.loads(chunks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_chunks = payload.get("chunks") if isinstance(payload, dict) else payload
    if not isinstance(raw_chunks, list):
        return []
    chunks: list[TextChunk] = []
    for item in raw_chunks:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        chunk_id = str(item.get("chunkId") or "").strip()
        if not content or not chunk_id:
            continue
        # 中文注释：老版本的 chunk.json 里 metadata 没有 has_table / has_math /
        # has_figure 这三个标签，读的时候统一补成 False，免得下游拿到 None 报错。
        metadata = dict(item.get("metadata") or {})
        for flag in ("has_table", "has_math", "has_figure"):
            metadata[flag] = bool(metadata.get(flag, False))
        chunks.append(
            TextChunk(
                chunk_id=chunk_id,
                paperId=str(item.get("paperId") or item.get("paper_id") or ""),
                chunk_index=int(item.get("chunk_index") or len(chunks)),
                content=content,
                page_start=_optional_int(item.get("page_start")),
                page_end=_optional_int(item.get("page_end")),
                section=str(item.get("section") or ""),
                previous_chunk_id=_optional_text(item.get("previous_chunk_id")),
                next_chunk_id=_optional_text(item.get("next_chunk_id")),
                metadata=metadata,
            )
        )
    return chunks


def preprocess_markdown_body(markdown: str) -> str:
    """删除页眉页脚、摘要和参考文献，只保留用于精读的正文。"""

    pages = _split_by_page_marker(markdown)
    if pages:
        # 中文注释：同一段文字反复出现在多页首尾时，通常就是页眉或页脚。
        # 先按页去掉这些重复文字，再拼回页码标记，后面的切分仍能保留页码信息。
        page_contents = _remove_repeated_page_margins([str(page.get("content") or "") for page in pages])
        body = "\n\n".join(
            f"<!-- page: {page['page']} -->\n{content.strip()}"
            for page, content in zip(pages, page_contents, strict=True)
            if content.strip()
        )
    else:
        body = _remove_front_matter(markdown)
    return _remove_abstract_and_references(body).strip()


def _remove_repeated_page_margins(page_contents: list[str]) -> list[str]:
    """识别多页中重复出现的首尾文字，并将它们从对应页面删除。"""

    if len(page_contents) < 2:
        return page_contents

    header_pages: dict[str, set[int]] = {}
    footer_pages: dict[str, set[int]] = {}
    for page_index, content in enumerate(page_contents):
        lines = _meaningful_lines(content)
        if not lines:
            continue
        header_key = _page_margin_key(lines[0])
        footer_key = _page_margin_key(lines[-1])
        if header_key:
            header_pages.setdefault(header_key, set()).add(page_index)
        if footer_key:
            footer_pages.setdefault(footer_key, set()).add(page_index)

    # 中文注释：至少出现在两页、且覆盖半数页面的首尾文字才删除，避免误删正文标题。
    minimum_pages = max(2, (len(page_contents) + 1) // 2)
    repeated_headers = {key for key, pages in header_pages.items() if len(pages) >= minimum_pages}
    repeated_footers = {key for key, pages in footer_pages.items() if len(pages) >= minimum_pages}
    cleaned_pages: list[str] = []
    for content in page_contents:
        lines = content.splitlines()
        meaningful_indexes = _meaningful_indexes(lines)
        if meaningful_indexes and _page_margin_key(lines[meaningful_indexes[0]]) in repeated_headers:
            lines[meaningful_indexes[0]] = ""
        if meaningful_indexes and _page_margin_key(lines[meaningful_indexes[-1]]) in repeated_footers:
            lines[meaningful_indexes[-1]] = ""
        cleaned_pages.append("\n".join(lines).strip())
    return cleaned_pages


def _meaningful_lines(content: str) -> list[str]:
    """返回页面里可以参与页眉页脚判断的非空文字行。"""

    lines = content.splitlines()
    return [lines[index].strip() for index in _meaningful_indexes(lines)]


def _meaningful_indexes(lines: list[str]) -> list[int]:
    """找出可以拿来判断"这是不是页眉页脚"的行号。

    中文注释：有几种行必须先排除掉：
    1. Markdown 表格的行（以竖线开头）——论文里同一张表可能每页都印，表头文字
       在多页里重复出现，要是拿它当页眉删掉，表格就被啃掉一块。
    2. $$ 公式块内部的行 —— 公式也常在多页里重复出现，同样不能删。
    3. 图片引用行和带编号的图注行 —— 页码会被统一成 '#'，不同页的图片引用看起来
       就一模一样了，一旦被当成重复页眉删掉，图就全没了。
    """

    protected = _protected_line_flags(lines)
    return [index for index, line in enumerate(lines) if line.strip() and not protected[index]]


def _protected_line_flags(lines: list[str]) -> list[bool]:
    """标出哪些行属于"表格行""$$ 公式块内部""图片引用和图注"，这些行任何清理都不许动。

    中文注释：返回的列表和传入的行一一对应，True 表示这一行受保护。
    """

    flags = [False] * len(lines)
    in_math = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _TABLE_LINE_RE.match(line):
            flags[index] = True
            continue
        if _PAGE_MARKER_RE.match(line):
            # 中文注释：页码标记是每一页的分界线，走到这里就把"正在公式里"这个状态清掉。
            # 假如某一页有个没配对的 $$，不在这里复位的话，从那一行一直到文末都会被
            # 当成公式保护起来——参考文献标题就再也认不出来，上千行参考文献会全部
            # 被切进片段送去精读。复位之后，一处异常最多污染它所在的那一页。
            in_math = False
            continue
        if _FIGURE_RE.search(line) or _CAPTION_RE.match(line):
            # 中文注释：图片引用行和"Figure 1: ..."这样的图注行也要保护起来。
            # 页眉页脚是按"多页里重复出现"来认的，而页码会被统一成 '#'——于是
            # assets/fig_p1_1.png 和 assets/fig_p2_1.png 会归一化成同一个样子。
            # 一篇每页都带图的论文，这些图片引用行落在半数以上页面的首行或末行时，
            # 就会整批被当成重复页眉删掉，图片全部消失、has_figure 全是 false。
            # 注意这里只保护"图片引用"和"带编号的图注"，真正重复的页眉文字
            # （比如期刊名、"Preprint. Under review."）照样会被删掉。
            flags[index] = True
            continue
        if in_math:
            flags[index] = True
            if "$$" in stripped:
                in_math = False  # 遇到结束的 $$，公式块到此为止
            continue
        if stripped.startswith("$$"):
            flags[index] = True
            # 中文注释：同一行里出现两次 $$（比如 $$x=1$$）说明公式这一行就写完了。
            if stripped.count("$$") < 2:
                in_math = True
    return flags


def _page_margin_key(line: str) -> str:
    """把页码中的数字统一替换，识别“第 1 页”和“第 2 页”这类重复页脚。"""

    normalized = re.sub(r"\d+", "#", line.lower())
    normalized = re.sub(r"\s+", "", normalized)
    return normalized if 3 <= len(normalized) <= 160 else ""


def _remove_abstract_and_references(markdown: str) -> str:
    """删除摘要到引言之间的内容，并删除参考文献及其后的内容。

    中文注释：找标题的时候要跳过表格行和 $$ 公式块里的行——万一某张表的单元格里
    刚好写着 "References"，按行去找标题就会从这里开始砍，把后面的正文全砍没。
    """

    lines = markdown.splitlines()
    protected = _protected_line_flags(lines)
    reference_index = next(
        (index for index, line in enumerate(lines) if not protected[index] and _is_references_heading(line)),
        len(lines),
    )
    body_lines = lines[:reference_index]
    abstract_index = next(
        (index for index, line in enumerate(body_lines) if not protected[index] and _is_abstract_heading(line)),
        None,
    )
    if abstract_index is None:
        return "\n".join(body_lines)

    # 中文注释：只有找到引言这类正文起点才删除摘要，避免解析异常时误删后续正文。
    body_start = next(
        (
            index
            for index in range(abstract_index + 1, len(body_lines))
            if not protected[index] and _is_body_start_heading(body_lines[index])
        ),
        None,
    )
    if body_start is None:
        return "\n".join(body_lines)
    return "\n".join(body_lines[:abstract_index] + body_lines[body_start:])


def _is_abstract_heading(line: str) -> bool:
    """判断一行是否是摘要标题，兼容英文和中文的常见写法。"""

    return bool(re.match(r"^\s*(?:#{1,6}\s*)?(?:abstract|摘要)\b[\s:：.\-—]*.*$", line, flags=re.IGNORECASE))


def _is_body_start_heading(line: str) -> bool:
    """判断一行是否标志着摘要结束后的正文开始。"""

    return bool(
        re.match(
            r"^\s*(?:#{1,6}\s*)?(?:(?:\d+|[ivxlcdm]+)[.)、]?\s*)?(?:introduction|引言)\b.*$",
            line,
            flags=re.IGNORECASE,
        )
    )


def _is_references_heading(line: str) -> bool:
    """判断一行是否是参考文献标题，命中后该行及后续内容都不参与精读。"""

    return bool(
        re.match(
            r"^\s*(?:#{1,6}\s*)?(?:(?:\d+|[ivxlcdm]+)[.)、]?\s*)?(?:references?|bibliography|参考文献)\s*$",
            line,
            flags=re.IGNORECASE,
        )
    )


def _split_by_page_marker(markdown: str) -> list[JsonObject]:
    """按 Markdown 里的页码标记切分内容。"""

    text = _remove_front_matter(markdown)
    pattern = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
    matches = list(pattern.finditer(text))
    if not matches:
        return []
    parts: list[JsonObject] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts.append({"page": int(match.group(1)), "content": text[start:end].strip()})
    return parts


def _remove_front_matter(markdown: str) -> str:
    """去掉 Markdown 开头的元数据块，只保留正文。"""

    text = markdown.strip()
    if not text.startswith("---"):
        return markdown
    match = re.match(r"(?s)^---\s*.*?\s*---\s*", text)
    return text[match.end() :] if match else markdown


def _attach_neighbors(chunks: list[TextChunk]) -> None:
    """给每个 chunk 补上前后相邻 chunk 的编号。"""

    for index, chunk in enumerate(chunks):
        chunk.previous_chunk_id = chunks[index - 1].chunk_id if index > 0 else None
        chunk.next_chunk_id = chunks[index + 1].chunk_id if index + 1 < len(chunks) else None


def _optional_int(value: Any) -> int | None:
    """把可能为空的页码转成整数。"""

    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    """把可能为空的字段转成字符串或 None。"""

    text = str(value).strip() if value is not None else ""
    return text or None
