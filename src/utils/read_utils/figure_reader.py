"""把论文插图交给视觉模型读一遍，写出"这张图画了什么"的笔记。

中文注释：这一步和别的地方不一样，值得说清楚它为什么单独存在——

论文插图以前完全不进模型。正文切分出来的片段里只有一行
"![图注原文](assets/fig_p2_1.png)"，模型知道"这里有一张图、图注说了什么"，
但不知道图画成什么样。而折线图这类图，图注往往只说"画了什么"，数据趋势全在图里。

为什么不像公式那样把图挂到切分片段上：分段阅读每段只写 500 字笔记，
图片信息进去也会被压掉；而且一张图跨好几段，挂到哪一段都别扭。
所以这里单独走一趟——图自己成一批发给模型，读出来的笔记再并进最后的汇总阶段。
这样既不受片段大小限制，也不会被 500 字上限砍掉。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.llm.base import vision_image_block
from src.utils.llm_json import parse_llm_json

logger = logging.getLogger(__name__)

# 一次请求带几张图。中文注释：带太多模型容易张冠李戴，带太少又白跑一趟。
FIGURES_PER_REQUEST = 4
# 同时发几个请求。
FIGURE_READING_CONCURRENCY = 3
# 回复的 token 上限。中文注释：这个模型会先写一段"思考"，而且图文都给它之后
# 它写得比公式那边长得多——实测一批 4 张图输出能到 4000 以上，给少了会写到一半被截断，
# 返回的 JSON 就不完整、整批白读。
FIGURE_READING_MAX_TOKENS = 8192
# 一张图的解读允许多长。中文注释：图片里的信息量比一条公式大得多，但也不该写成长篇——
# 这些笔记最后是要并进汇总阶段的提示词的。
MAX_FIGURE_NOTE_CHARS = 600
# 短于这个长度的解读当作没读出来。
MIN_FIGURE_NOTE_CHARS = 8

# 正文里的图片引用：![说明文字](assets/文件名)
_IMAGE_REFERENCE_PATTERN = re.compile(r"!\[([^\]]*)\]\(assets/([^)]+)\)")
# 图片文件名里带着页码：fig_p2_1.png 表示第 2 页第 1 张。
_PAGE_IN_FILE_NAME_PATTERN = re.compile(r"_p(\d+)_")
# 正文里的分页标记：<!-- page: 5 -->
_PAGE_MARKER_PATTERN = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
# 图注开头的"种类 + 编号"：Fig. 2 / Figure 2 / TABLE 2 / Table 2
_CAPTION_PREFIX_PATTERN = re.compile(r"^(Figure|Fig\.?|TABLE|Table)\s*(\d+)(.*)$", re.S)
# 中文注释：没有图注的图，说明文字会被兜底写成"Figure 3"这种样子。它长得和真图注
# 几乎一样，直接拿去抠编号会把兜底编号当成真图号，然后配上另一张图的说明段落。
# 所以要求"编号后面还得有像样的内容"才算真图注——真图注不会只有"Figure 3"几个字。
_MIN_CAPTION_BODY_CHARS = 10
# 中文注释：去正文里找"有没有提到这张图"时，要按种类分开搜。图和表是两样东西，
# 拿 "Table 1" 的编号去搜，会搜到论文里的 "Figure 1"，上下文就串到别的对象上了。
_REFERENCE_PATTERNS = {
    "figure": r"(?:Figure|Fig\.?)s?\.?",
    "table": r"(?:TABLE|Table)s?\.?",
}

# 中文注释：给每张图配多少正文上下文。给少了模型还是猜，给多了纯烧 token，
# 而且一段页正文动辄几千字，图本身的信息会被淹掉。
MAX_FIGURE_CONTEXT_CHARS = 1200
# 正文里提到这张图的地方，最多取几段。同一张图常被反复引用，取前几段就够。
MAX_FIGURE_CONTEXT_PARAGRAPHS = 3

FIGURE_READING_SYSTEM_PROMPT = """
你是论文插图解读助手。用户会给你几张从论文 PDF 上截下来的插图，每张前面标着它的编号、页码、图注（如果有），以及一段论文正文作为上下文。

请逐张用中文说明这张图表达了什么。按图的类型抓重点：

- 数据图（折线、柱状、散点）：横纵轴各是什么、有哪些曲线或分组、整体趋势，以及能看出来的关键数值。
- 结构图或框架图：由哪些模块组成、模块之间怎么连接、数据或流程往哪个方向走。
- 示意图或场景图：画的是什么场景、有哪些标注、想说明什么。

上下文怎么用：正文里往往写明了图里每个符号、每条曲线、每个指标是什么意思，作者也会在那里解释图想说明什么。**优先借助上下文把图看懂**——比如正文说了"B 是预算""MARVEL 是基线"，你就要在解读里点明图中哪条曲线对应哪个方法。但只能写上下文和图里都能对上的内容，上下文提到的、图上找不到的东西不要写进来。

要求：
1. 只输出一个 JSON 对象，形如：{"figures":[{"index":1,"note":"..."}]}，index 就是用户给的编号。
2. 每张图的 note 不超过 400 字。
3. 只写图里确实能看到的内容。看不清的地方就说看不清，不要凭论文标题或常识猜。
4. 不要输出解释文字，不要用 ``` 代码块包裹。
5. 如果某张图实在认不出内容，把它的 note 写成空字符串。
""".strip()


@dataclass(slots=True)
class PaperFigure:
    """论文里的一张插图：在第几页、图片文件在哪、图注写的是什么、正文里怎么讲它。"""

    page_number: int
    path: Path
    caption: str
    context: str


def collect_paper_figures(
    markdown_text: str,
    assets_dir: Path | None,
    blocks: list[Any] | None = None,
) -> list[PaperFigure]:
    """整理插图清单。优先用解析时的图块，其次用正文开头记下的清单。

    中文注释：图块上有页码、文件名和图注，比从 Markdown 图片链接里反查更稳。
    网页全文没有图块，也没有这份清单，才退回正文里的图片引用。

    同时给每张图配一段正文上下文。脱离了正文，图是读不准的：一张画着"coverage rate"
    的图，光看图注不知道这个指标在本篇论文里怎么定义、哪条线是基线；而正文里通常
    写着这些。所以这里先找"正文里明确提到这张图的段落"，找不到再退回它所在那一页的正文。
    """

    if assets_dir is None or not assets_dir.is_dir():
        return []
    page_texts = _split_by_page(markdown_text)
    body_paragraphs = _body_paragraphs(markdown_text)
    body_text = "\n".join(body_paragraphs)
    records = _figure_records(blocks, markdown_text)
    if records is None:
        records = [
            {"page": None, "file": file_name, "caption": caption}
            for caption, file_name in _IMAGE_REFERENCE_PATTERN.findall(markdown_text)
        ]

    figures: list[PaperFigure] = []
    seen: set[str] = set()
    for record in records:
        file_name = str(record.get("file") or "")
        caption = str(record.get("caption") or "")
        if not file_name or file_name in seen:
            continue
        path = assets_dir / file_name
        if not path.is_file():
            continue
        seen.add(file_name)
        page_number = record.get("page")
        if not isinstance(page_number, int):
            match = _PAGE_IN_FILE_NAME_PATTERN.search(file_name)
            page_number = int(match.group(1)) if match else 0
        figures.append(
            PaperFigure(
                page_number=page_number,
                path=path,
                caption=caption.strip(),
                context=_figure_context(caption.strip(), page_number, body_text, body_paragraphs, page_texts),
            )
        )
    return figures


def _figure_records(blocks: list[Any] | None, markdown_text: str) -> list[dict[str, Any]] | None:
    """取出插图清单。有图块就用图块；正文开头写了 figures 就用那份；都没有就返回空值。"""

    if blocks:
        records: list[dict[str, Any]] = []
        for block in blocks:
            if getattr(block, "kind", "") != "figure":
                continue
            file_name = str(getattr(block, "asset_name", "") or "")
            if not file_name:
                continue
            caption = str(getattr(block, "text", "") or "")
            match = _IMAGE_REFERENCE_PATTERN.search(caption)
            if match:
                caption = match.group(1)
            records.append(
                {"page": getattr(block, "page_number", None), "file": file_name, "caption": caption}
            )
        return records
    if not markdown_text.startswith("---"):
        return None
    end = markdown_text.find("\n---", 3)
    if end < 0:
        return None
    try:
        header = json.loads(markdown_text[3:end].strip())
    except json.JSONDecodeError:
        return None
    listed = header.get("figures") if isinstance(header, dict) else None
    if not isinstance(listed, list):
        return None
    return [item for item in listed if isinstance(item, dict)]


def _split_by_page(markdown_text: str) -> dict[int, str]:
    """按正文里的分页标记把 Markdown 切成"第几页 → 那一页的文字"。"""

    pages: dict[int, str] = {}
    matches = list(_PAGE_MARKER_PATTERN.finditer(markdown_text))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown_text)
        pages[int(match.group(1))] = markdown_text[start:end]
    return pages


def _body_paragraphs(markdown_text: str) -> list[str]:
    """把正文拆成一段一段，顺手把图片引用那一行去掉。

    中文注释：图片引用本身不是正文，它的文字就是图注，已经单独拿到了；
    留在段落里只会让上下文里混进一堆重复的图注。
    """

    paragraphs: list[str] = []
    for block in markdown_text.split("\n\n"):
        text = _IMAGE_REFERENCE_PATTERN.sub("", block)
        text = _PAGE_MARKER_PATTERN.sub("", text).strip()
        if len(text) >= 20:
            paragraphs.append(text)
    return paragraphs


def _figure_context(
    caption: str,
    page_number: int,
    body_text: str,
    body_paragraphs: list[str],
    page_texts: dict[int, str],
) -> str:
    """给一张图配正文上下文。

    中文注释：分两层取——

    第一层：正文里明确提到这张图的那几段。作者一般会在那里解释这张图想说明什么，
    信息最对得上。找法是拿图注里的编号（"Fig. 2" 里的 2）去正文里搜"Fig. 2"这类写法。

    第二层：实在没人提，就退回它所在那一页的正文。这一层只是聊胜于无——同一页的
    文字不一定在讲这张图，但至少是同一节的语境。
    """

    reference = _caption_reference(caption)
    if reference:
        kind, number = reference
        referenced = _paragraphs_mentioning(body_paragraphs, kind, number)
        if referenced:
            return "\n".join(referenced)[:MAX_FIGURE_CONTEXT_CHARS]
    page_text = page_texts.get(page_number, "")
    page_text = _IMAGE_REFERENCE_PATTERN.sub("", page_text)
    return " ".join(page_text.split())[:MAX_FIGURE_CONTEXT_CHARS]


def _caption_reference(caption: str) -> tuple[str, str] | None:
    """从图注里认出"这是第几号图/表"，返回 (种类, 编号)。认不出来就返回空值。

    中文注释：只有"像真图注"才认。没有图注的图，说明文字是兜底生成的"Figure 3"，
    它和真图注长得几乎一样，但它后面没有正文——拿它去搜"Figure 3"，会配到论文里
    另一张真图 3 的说明段落，上下文就串了。所以要求编号后面还得有像样的内容。
    """

    match = _CAPTION_PREFIX_PATTERN.match(caption or "")
    if not match:
        return None
    prefix, number, rest = match.group(1), match.group(2), match.group(3)
    if len(rest.strip(" :：.、")) < _MIN_CAPTION_BODY_CHARS:
        return None
    return ("table" if prefix.lower().startswith("tab") else "figure"), number


def _paragraphs_mentioning(paragraphs: list[str], kind: str, number: str) -> list[str]:
    """挑出正文里提到这个编号的段落。

    中文注释：编号后面加个"不能再跟数字"的限制，否则找第 2 张图时会把
    "Fig. 20" 也一起捞进来。
    """

    pattern = re.compile(rf"{_REFERENCE_PATTERNS[kind]}\s*{re.escape(number)}(?!\d)")
    picked: list[str] = []
    for paragraph in paragraphs:
        if pattern.search(paragraph):
            picked.append(paragraph)
            if len(picked) >= MAX_FIGURE_CONTEXT_PARAGRAPHS:
                break
    return picked


async def read_figure_notes(
    *,
    figures: list[PaperFigure],
    focus: str,
    llm: Any | None,
    on_progress: Callable[[str], None] | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
) -> tuple[str, int, int]:
    """把插图交给模型读一遍，返回 (插图笔记文本, 输入 token, 输出 token)。

    中文注释：任何一张图读不出来都不影响其他图；整批全失败就返回空文本，
    汇总阶段拿不到插图笔记而已，报告照样出。
    """

    if not figures or llm is None:
        return "", 0, 0

    # 中文注释：每批记下"这批从第几张开始"，模型只回报批内的序号，
    # 换算成全局序号才能对上哪张图是哪张。
    batches: list[tuple[int, list[PaperFigure]]] = [
        (start, figures[start:start + FIGURES_PER_REQUEST])
        for start in range(0, len(figures), FIGURES_PER_REQUEST)
    ]
    if on_progress is not None:
        on_progress(f"正在解读论文插图（共 {len(figures)} 张）")

    semaphore = asyncio.Semaphore(FIGURE_READING_CONCURRENCY)
    notes: dict[int, str] = {}
    total_input = 0
    total_output = 0
    completed = 0

    async def run_one(offset: int, batch: list[PaperFigure]) -> None:
        nonlocal total_input, total_output, completed
        async with semaphore:
            # 中文注释：并发跑的时候也要能中途停下，用户点了停止就及时退出。
            if raise_if_cancelled is not None:
                raise_if_cancelled()
            result, input_tokens, output_tokens = await _read_one(batch, offset, focus, llm)
            total_input += input_tokens
            total_output += output_tokens
            notes.update(result)
            completed += 1
            if on_progress is not None:
                on_progress(f"正在解读论文插图（{completed}/{len(batches)} 批）")

    await asyncio.gather(*(run_one(offset, batch) for offset, batch in batches))
    return _render_notes(figures, notes), total_input, total_output


def _render_notes(figures: list[PaperFigure], notes: dict[int, str]) -> str:
    """把每张图的解读拼成一段文字，交给汇总阶段。"""

    lines: list[str] = []
    for index, figure in enumerate(figures, 1):
        note = notes.get(index, "")
        if not note:
            continue
        where = f"第 {figure.page_number} 页" if figure.page_number else "页码未知"
        caption = f"，图注：{figure.caption}" if figure.caption else ""
        lines.append(f"【图 {index}（{where}{caption}）】{note}")
    return "\n".join(lines)


async def _read_one(
    batch: list[PaperFigure], offset: int, focus: str, llm: Any
) -> tuple[dict[int, str], int, int]:
    """问一次模型。返回 (第几张图 → 解读, 输入 token, 输出 token)。"""

    try:
        payload = await asyncio.to_thread(_load_batch_images, batch)
    except Exception as exc:
        logger.warning("插图读取失败，这一批跳过", extra={"reason": str(exc)})
        return {}, 0, 0

    try:
        response = await llm.provider.chat(
            _build_messages(batch, payload, focus, llm),
            temperature=0,
            max_tokens=FIGURE_READING_MAX_TOKENS,
        )
    except Exception as exc:
        logger.warning("插图解读请求异常，这一批跳过", extra={"reason": str(exc)})
        return {}, 0, 0

    usage = _usage_of(response)
    if not _is_ok(response):
        logger.warning(
            "插图解读失败，这一批跳过",
            extra={"error_kind": getattr(response, "error_kind", None)},
        )
        return {}, usage[0], usage[1]

    parsed = await parse_llm_json(getattr(response, "content", "") or "", fallback={})
    entries = parsed.get("figures") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        logger.warning("插图解读返回的内容不是预期的 JSON，这一批跳过")
        return {}, usage[0], usage[1]

    outcome: dict[int, str] = {}
    by_position: dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            position = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        by_position[position] = str(entry.get("note") or "").strip()

    for position, figure in enumerate(batch, start=1):
        note = by_position.get(position, "")
        if len(note) < MIN_FIGURE_NOTE_CHARS:
            continue
        if len(note) > MAX_FIGURE_NOTE_CHARS:
            note = note[:MAX_FIGURE_NOTE_CHARS]
        outcome[offset + position] = note
    return outcome, usage[0], usage[1]


def _load_batch_images(batch: list[PaperFigure]) -> list[bytes]:
    """把这一批图片读进内存。中文注释：读文件是重活，调用方会把它丢到线程里跑。"""

    return [figure.path.read_bytes() for figure in batch]


def _build_messages(
    batch: list[PaperFigure], payloads: list[bytes], focus: str, llm: Any
) -> list[dict[str, Any]]:
    """拼出这一次请求的消息。图片格式按模型实际走的接口来。"""

    content: list[dict[str, Any]] = []
    for position, (figure, payload) in enumerate(zip(batch, payloads), start=1):
        label = f"图 {position}（第 {figure.page_number} 页）"
        if figure.caption:
            label += f"，图注：{figure.caption}"
        content.append({"type": "text", "text": f"{label}："})
        if figure.context:
            # 中文注释：正文放在图片前面。模型是先读文字再看图，带着"这篇论文里
            # 这个指标是什么意思、哪条线是基线"去看，比先看图再补文字准得多。
            content.append({"type": "text", "text": f"论文正文里的相关段落：\n{figure.context}"})
        content.append(vision_image_block(llm, payload, _media_type(figure.path.name)))
    instruction = "请只输出一个 JSON 对象：{\"figures\":[{\"index\":1,\"note\":\"...\"}]}。index 用上面给出的图编号。"
    if focus:
        instruction += f"\n用户关注的角度是「{focus}」，图里能看出和它相关的内容就优先写出来。"
    instruction += "\n读不出来的图把 note 写成空字符串。"
    content.append({"type": "text", "text": instruction})
    return [
        {"role": "system", "content": FIGURE_READING_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _media_type(file_name: str) -> str:
    """按文件后缀给出图片类型。"""

    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "png"
    if suffix in {"jpg", "jpeg"}:
        return "image/jpeg"
    if suffix == "webp":
        return "image/webp"
    return "image/png"


def _is_ok(response: Any) -> bool:
    """这次请求算不算成功。"""

    ok = getattr(response, "ok", None)
    if ok is None:
        return not getattr(response, "error_kind", None)
    return bool(ok)


def _usage_of(response: Any) -> tuple[int, int]:
    """从响应里取这次用掉多少 token。两种厂商的字段名都认。"""

    usage = getattr(response, "usage", None)
    if not isinstance(usage, dict):
        return 0, 0
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens")
    if output_tokens is None:
        output_tokens = usage.get("completion_tokens")
    try:
        return int(input_tokens or 0), int(output_tokens or 0)
    except (TypeError, ValueError):
        return 0, 0
