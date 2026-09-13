"""把论文里的表格截图交给视觉模型，重排成表头正确的 Markdown 表格。

中文注释：为什么表格也要走这一步——

PDF 里表格是"排"出来的，程序只能靠"框线在哪"去猜哪个格子属于哪一列。遇到跨两层的
表头（上面一层写数据集、下面一层写指标），猜出来的表头会把好几列的名字糊进一个格子。
实测一篇论文的表头被糊成 "LLaVA-OV-7B 64 + ReKV 0.5 fps + LiveVLM 0.5 fps" 这么一长串，
结果是数字全在、但哪一列是哪个指标全没了——报告里写不出"在 X 数据集上 Y 指标是多少"。

把整张表截成图给模型，它按看到的排版能把表头和列对齐写回来。这一步和公式转写是
同一个路子：正文里先留占位符，转写完成后换掉；转不出来就退回原来那份 Markdown 表。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any, Callable

from src.utils.llm_json import parse_llm_json
from src.utils.read_utils.pdf_parsers import PdfTableRegion

logger = logging.getLogger(__name__)

# 截图清晰度。中文注释：表格是文字最密的地方，单元格里的小字要能看清，
# 给得比插图高一些。
TABLE_OCR_DPI = 220
# 图片宽度上限，太宽就按比例降清晰度。
TABLE_OCR_MAX_PX_WIDTH = 2000
# 一次请求带几张表。中文注释：表格图比公式图大得多，转写结果本身也长——实测一次带两张
# 大表时，模型写到一半就撞上输出上限、返回的 JSON 不完整，整批白读。所以一次只带一张。
TABLES_PER_REQUEST = 1
# 同时发几个请求。
TABLE_OCR_CONCURRENCY = 2
# 回复的 token 上限。中文注释：这个模型会先写一段"思考"，表格本身又长（实测一张
# 40 行的结果表转写出来好几千 token）。给 8192 时最大的那几张表会写到一半被截断，
# 返回的 JSON 不完整、整张表白读。这里给到 16384，实测接口接受。
TABLE_OCR_MAX_TOKENS = 16384
# 一张表的 Markdown 最长允许多少字符。
MAX_TABLE_MARKDOWN_CHARS = 8000

# Markdown 表格行：以 | 开头。
_TABLE_ROW_PATTERN = re.compile(r"^\s*\|")
# Markdown 表格的分隔行：| --- | --- |（允许带对齐冒号）。
_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
# 回复里带的代码围栏。
_FENCE_PATTERN = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

TABLE_OCR_SYSTEM_PROMPT = """
你是论文表格识别助手。用户会给你几张从论文 PDF 上截下来的表格图片，每张前面标着它的编号、页码和图注。

请把每张表格转写成 GitHub 风格的 Markdown 表格。

要求：
1. 只输出一个 JSON 对象，形如：{"tables":[{"index":1,"markdown":"| 表头 | ... |\\n| --- | --- |\\n| 数据 | ... |"}]}，index 就是用户给的编号。
2. markdown 字段里是完整的表格：第一行是表头，第二行是 | --- | 分隔行，之后是数据行。
3. **表头必须写全**。表格上面如果有多层表头（一层写数据集、另一层写指标），把每一列到底是哪个数据集下的哪个指标写清楚，可以把上下两层用空格拼在一个表头格里。
4. 数字照抄，不要四舍五入、不要换单位、不要改动。
5. 单元格里的内容如果很长，照原样写进去即可；单元格里的换行用空格代替。
6. 不要把表格外面的正文、图注写进表格里。
7. 不要用 ``` 代码块包裹，不要输出解释文字。
8. 如果某张表格实在认不出，把它的 markdown 写成空字符串。
""".strip()


async def transcribe_table_regions(
    *,
    pdf_path: Path,
    regions: list[PdfTableRegion],
    markdown_text: str,
    llm: Any | None,
    on_progress: Callable[[str], None] | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
) -> tuple[str, int, int]:
    """把正文里的表格占位符换成重排好的 Markdown 表，返回 (换好的正文, 输入 token, 输出 token)。

    中文注释：不管转写成不成功，占位符都必须被换掉——转写失败的就换回原来那份
    Markdown 表，绝不能让 <!-- table: 7_1 --> 这种东西留在正文里。

    模型没给（比如没配、或者调用方没传）时不调模型，全部走原文，返回的 token 数是 0。
    """

    replacements: dict[str, str] = {}
    total_input = 0
    total_output = 0

    if regions and llm is not None:
        try:
            crops = await asyncio.to_thread(_render_crops, pdf_path, regions)
        except Exception as exc:
            logger.warning("表格截图失败，本次全部退回原有表格", extra={"reason": str(exc)})
            crops = {}
        if crops:
            batches = _make_batches([region for region in regions if region.placeholder in crops])
            if batches and on_progress is not None:
                on_progress(f"正在识别论文表格（共 {len(batches)} 批）")
            transcribed, total_input, total_output = await _transcribe_batches(
                batches, crops, llm, on_progress, raise_if_cancelled
            )
            replacements.update(transcribed)

    for region in regions:
        markdown = replacements.get(region.placeholder)
        markdown_text = markdown_text.replace(
            region.placeholder, markdown if markdown else region.fallback
        )
    return markdown_text, total_input, total_output


def _render_crops(pdf_path: Path, regions: list[PdfTableRegion]) -> dict[str, bytes]:
    """按表格区域把 PDF 截成一张张图，返回 {占位符: 图片字节}。"""

    import pymupdf

    crops: dict[str, bytes] = {}
    with pymupdf.open(str(pdf_path)) as doc:
        for region in regions:
            page = doc[region.page_number - 1]
            clip = pymupdf.Rect(*region.rect)
            if clip.is_empty or clip.is_infinite:
                continue
            crops[region.placeholder] = page.get_pixmap(clip=clip, dpi=_capped_dpi(clip)).tobytes("png")
    return crops


def _capped_dpi(clip: Any) -> int:
    """算这张表该用多少清晰度，太宽的表就降一点。"""

    width_points = float(clip.x1) - float(clip.x0)
    if width_points <= 0:
        return TABLE_OCR_DPI
    if width_points * TABLE_OCR_DPI / 72 <= TABLE_OCR_MAX_PX_WIDTH:
        return TABLE_OCR_DPI
    return max(72, int(TABLE_OCR_MAX_PX_WIDTH * 72 / width_points))


def _make_batches(regions: list[PdfTableRegion]) -> list[list[PdfTableRegion]]:
    """把表格按页排好，每组的数量不超过一次请求能带的张数。"""

    ordered = sorted(regions, key=lambda item: (item.page_number, item.index))
    return [
        ordered[start:start + TABLES_PER_REQUEST]
        for start in range(0, len(ordered), TABLES_PER_REQUEST)
    ]


async def _transcribe_batches(
    batches: list[list[PdfTableRegion]],
    crops: dict[str, bytes],
    llm: Any,
    on_progress: Callable[[str], None] | None,
    raise_if_cancelled: Callable[[], None] | None,
) -> tuple[dict[str, str], int, int]:
    """一批一批地并发去问模型，返回 {占位符: Markdown 表} 和累计的 token 用量。"""

    semaphore = asyncio.Semaphore(TABLE_OCR_CONCURRENCY)
    results: dict[str, str] = {}
    total_input = 0
    total_output = 0
    completed = 0

    async def run_one(batch: list[PdfTableRegion]) -> None:
        nonlocal total_input, total_output, completed
        async with semaphore:
            # 中文注释：并发跑的时候也要能中途停下，用户点了停止就及时退出。
            if raise_if_cancelled is not None:
                raise_if_cancelled()
            outcome, input_tokens, output_tokens = await _transcribe_one(batch, crops, llm)
            total_input += input_tokens
            total_output += output_tokens
            results.update(outcome)
            completed += 1
            if on_progress is not None:
                on_progress(f"正在识别论文表格（{completed}/{len(batches)} 批）")

    await asyncio.gather(*(run_one(batch) for batch in batches))
    return results, total_input, total_output


async def _transcribe_one(
    batch: list[PdfTableRegion], crops: dict[str, bytes], llm: Any
) -> tuple[dict[str, str], int, int]:
    """问一次模型。返回 (占位符到 Markdown 表的对应, 输入 token, 输出 token)。"""

    try:
        response = await llm.provider.chat(
            _build_messages(batch, crops), temperature=0, max_tokens=TABLE_OCR_MAX_TOKENS
        )
    except Exception as exc:
        logger.warning("表格转写请求异常，这一批退回原有表格", extra={"reason": str(exc)})
        return {}, 0, 0

    usage = _usage_of(response)
    if not _is_ok(response):
        logger.warning(
            "表格转写失败，这一批退回原有表格",
            extra={"error_kind": getattr(response, "error_kind", None)},
        )
        return {}, usage[0], usage[1]

    parsed = await parse_llm_json(getattr(response, "content", "") or "", fallback={})
    entries = parsed.get("tables") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        logger.warning("表格转写返回的内容不是预期的 JSON，这一批退回原有表格")
        return {}, usage[0], usage[1]

    by_index: dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[index] = str(entry.get("markdown") or "")

    outcome: dict[str, str] = {}
    for position, region in enumerate(batch, start=1):
        problem = _validate_table(by_index.get(position, ""))
        if problem is not None:
            logger.warning(
                "表格转写结果没通过检查，退回原有表格",
                extra={"page": region.page_number, "index": region.index, "reason": problem},
            )
            continue
        outcome[region.placeholder] = _normalise_table(by_index[position])
    return outcome, usage[0], usage[1]


def _build_messages(
    batch: list[PdfTableRegion], crops: dict[str, bytes]
) -> list[dict[str, Any]]:
    """拼出这一次请求的消息。

    中文注释：图片用 Anthropic 那套原生格式，项目里发消息那条链路对它是原样透传的。
    图注也一起给，模型看到"Table 1: 在 StreamingBench 上的性能对比"就知道表头大概该写什么。
    """

    content: list[dict[str, Any]] = []
    for position, region in enumerate(batch, start=1):
        label = f"表格 {position}（第 {region.page_number} 页）"
        if region.caption:
            label += f"，图注：{region.caption}"
        content.append({"type": "text", "text": f"{label}："})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(crops[region.placeholder]).decode("ascii"),
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": (
                "请只输出一个 JSON 对象："
                '{"tables":[{"index":1,"markdown":"| 表头 | ... |\\n| --- | --- |\\n| 数据 | ... |"}]}。'
                "index 用上面给出的表格编号。认不出的把 markdown 写成空字符串。"
            ),
        }
    )
    return [
        {"role": "system", "content": TABLE_OCR_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _validate_table(markdown: str) -> str | None:
    """检查模型给的表格能不能用。能用返回 None，不能用返回原因。"""

    text = _strip_fence(markdown).strip()
    if not text:
        return "空的"
    if len(text) > MAX_TABLE_MARKDOWN_CHARS:
        return "太长了"
    lines = [line for line in text.splitlines() if line.strip()]
    rows = [line for line in lines if _TABLE_ROW_PATTERN.match(line)]
    if len(rows) < 3:
        # 表格至少要有表头行、分隔行、一行数据。
        return "行数不够"
    if not any(_TABLE_SEPARATOR_PATTERN.match(line) for line in rows):
        # 没有 |---| 分隔行就不是合法的 Markdown 表格，前端渲染不出来。
        return "缺少分隔行"
    return None


def _normalise_table(markdown: str) -> str:
    """把模型给的表格整理成能直接放进正文的样子。"""

    lines = [line.rstrip() for line in _strip_fence(markdown).strip().splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _strip_fence(markdown: str) -> str:
    """去掉模型可能带上的代码围栏。"""

    return _FENCE_PATTERN.sub("", markdown.strip())


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
