"""把论文里的公式截图交给视觉模型，转写成 LaTeX。

中文注释：这个文件只干一件事——拿 PDF 里认出来的公式位置，把那一小块截图，
问模型"这是什么公式"，把答案换回正文。

为什么要这么绕：PDF 里的公式是一堆带位置的字形，直接抽文字只能得到
"𝑆𝑙 𝑖= 𝛼𝑙 𝑖· 𝑊𝑙" 这种碎片，模型看不懂。而现在的对话模型本身就能看图，
把那一小块图直接给它，它能把整条公式原样写回 LaTeX。

调用方会传进来一个已经装配好的模型（和精读用的是同一个），这里不关心它是谁。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any, Callable

from src.utils.llm_json import parse_llm_json
from src.utils.read_utils.pdf_parsers import PdfFormulaRegion

logger = logging.getLogger(__name__)

# 截图用的清晰度。中文注释：论文公式的字号大概 10 点，上下标更小。用 300 点
# 截出来上下标还能看清，模型认得更准；再高只是白白撑大图片。
FORMULA_OCR_DPI = 300
# 图片宽度上限。中文注释：个别横跨整栏的长公式按 300 点截会非常宽，
# 既费流量又没必要，超过这个宽度就按比例把清晰度降下来。
FORMULA_OCR_MAX_PX_WIDTH = 1600
# 一次请求最多带几个公式。中文注释：同一页的公式合并成一次请求，能少调好几次模型；
# 但一次带太多图，模型容易张冠李戴，所以设个上限。
FORMULAS_PER_REQUEST = 6
# 同时发几个请求。中文注释：和精读分段阅读用的是同一个量级。
FORMULA_OCR_CONCURRENCY = 3
# 回复的 token 上限。中文注释：必须给足——这个模型会先写一段"思考"再给正文，
# 给少了会出现"只有思考、正文是空的"，转写就白做了。
FORMULA_OCR_MAX_TOKENS = 8192
# 一条公式的 LaTeX 最长允许多少字符。中文注释：正常公式远不到这个长度，
# 超过基本是模型跑偏了。
MAX_FORMULA_LATEX_CHARS = 800

# 模型输出的 LaTeX 里如果还带着 $ 或 $$，要剥掉——外面那层定界符由我们自己加。
_MATH_FENCE_PATTERN = re.compile(r"\$+")
# 中日韩文字。中文注释：LaTeX 命令里不会出现中文，出现了就说明模型在写解释。
_CJK_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 判断"这看起来像不像一条公式"：有反斜杠命令、或者有上下标/等号这类数学记号。
_MATH_SIGNAL_PATTERN = re.compile(r"\\[a-zA-Z]+|\^|_|=")
# 弱数学记号（单独出现不足以说明是公式，要配合上面的检查）
_WEAK_MATH_CHARS = frozenset("+-*/()[]{}<>|,.")

FORMULA_OCR_SYSTEM_PROMPT = """
你是论文公式识别助手。用户会给你几张从论文 PDF 上截下来的公式图片，每张图片前面会标"公式 N（第 X 页）"。

请把每张图片里的数学公式转写成 LaTeX。

要求：
1. 只输出一个 JSON 对象，形如：{"formulas":[{"index":1,"latex":"..."}]}，index 就是用户给的公式编号。
2. 不要输出任何解释文字，不要用 ``` 代码块包裹。
3. 用标准的 LaTeX 写法：下标用 _，上标用 ^，分数用 \\frac{}{}，希腊字母用 \\alpha \\beta \\lambda 这类命令，乘号用 \\cdot。
4. 图片里如果带着公式编号（形如 (2)），把它写成 \\tag{2} 放在公式最后面。
5. 不要把公式外的英文单词、图注文字一起转写进来；只转写公式本身。
6. 不要自己加 $ 或 $$ 定界符。
7. 如果某张图片里认不出公式，把它的 latex 写成空字符串，不要猜。
8. 多字母下标写成 _{\text{model}}，不要写成 _{model}；min / max / log 写成 \\min \\max \\log，不要写成斜体的 min。
9. 印刷体里连在一起的英文单词（如 lrate）不要拆成 l_{rate}。
""".strip()


async def transcribe_formula_regions(
    *,
    pdf_path: Path,
    regions: list[PdfFormulaRegion],
    markdown_text: str,
    llm: Any | None,
    on_progress: Callable[[str], None] | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
) -> tuple[str, int, int]:
    """把正文里的公式占位符换成真 LaTeX，返回 (换好的正文, 输入 token, 输出 token)。

    中文注释：不管转写成不成功，占位符都必须被换掉——转写失败的就换回"把原始字形
    原样放进 $$ 块"的兜底内容，绝不能让 <!-- formula: 5_1 --> 这种东西留在正文里。

    模型没给（比如没配、或者开关关了）时不调模型，全部走兜底，返回的 token 数是 0。
    """

    # 中文注释：只有"单独占一行"的公式才值得截图转写。夹在句子里的公式留在正文里，
    # 加个 $ 就够了，为它单独截一次图不划算。
    targets = [region for region in regions if region.is_display]
    replacements: dict[str, str] = {}
    total_input = 0
    total_output = 0

    if targets and llm is not None:
        try:
            crops = await asyncio.to_thread(_render_crops, pdf_path, targets)
        except Exception as exc:
            # 中文注释：截图失败不影响大局，下面会把所有占位符换成兜底内容。
            logger.warning("公式截图失败，本次全部退回展平文本", extra={"reason": str(exc)})
            crops = {}
        if crops:
            batches = _make_batches([region for region in targets if region.placeholder in crops])
            if batches and on_progress is not None:
                on_progress(f"正在识别论文公式（共 {len(batches)} 批）")
            transcribed, total_input, total_output = await _transcribe_batches(
                batches, crops, llm, on_progress, raise_if_cancelled
            )
            replacements.update(transcribed)

    for region in targets:
        # 中文注释：转写成功的用 LaTeX，没成功的用兜底。两个都写成"$$ 独占一行"的样子，
        # 这样切分那一步的公式保护逻辑（靠成对的 $$ 判断）才认得出来。
        latex = replacements.get(region.placeholder)
        if latex:
            markdown_text = markdown_text.replace(region.placeholder, f"$$\n{latex}\n$$")
        else:
            markdown_text = markdown_text.replace(region.placeholder, region.fallback)
    return markdown_text, total_input, total_output


def _render_crops(pdf_path: Path, regions: list[PdfFormulaRegion]) -> dict[str, bytes]:
    """按公式区域把 PDF 截成一张张小图，返回 {占位符: 图片字节}。

    中文注释：这个方法会卡住（读文件、渲染图片都是重活），所以调用方用 to_thread
    把它丢到别的线程去跑，别堵住主流程。
    """

    import pymupdf

    crops: dict[str, bytes] = {}
    with pymupdf.open(str(pdf_path)) as doc:
        for region in regions:
            page = doc[region.page_number - 1]
            clip = pymupdf.Rect(*region.rect)
            if clip.is_empty or clip.is_infinite:
                continue
            crops[region.placeholder] = page.get_pixmap(
                clip=clip, dpi=_capped_dpi(clip)
            ).tobytes("png")
    return crops


def _capped_dpi(clip: Any) -> int:
    """算这张图该用多少清晰度，太宽的公式就降一点。"""

    width_points = float(clip.x1) - float(clip.x0)
    if width_points <= 0:
        return FORMULA_OCR_DPI
    if width_points * FORMULA_OCR_DPI / 72 <= FORMULA_OCR_MAX_PX_WIDTH:
        return FORMULA_OCR_DPI
    return max(72, int(FORMULA_OCR_MAX_PX_WIDTH * 72 / width_points))


def _make_batches(regions: list[PdfFormulaRegion]) -> list[list[PdfFormulaRegion]]:
    """把公式按页分组，每组的数量不超过一次请求能带的张数。"""

    by_page: dict[int, list[PdfFormulaRegion]] = {}
    for region in regions:
        by_page.setdefault(region.page_number, []).append(region)
    batches: list[list[PdfFormulaRegion]] = []
    for page_number in sorted(by_page):
        on_page = sorted(by_page[page_number], key=lambda item: item.index)
        for start in range(0, len(on_page), FORMULAS_PER_REQUEST):
            batches.append(on_page[start:start + FORMULAS_PER_REQUEST])
    return batches


async def _transcribe_batches(
    batches: list[list[PdfFormulaRegion]],
    crops: dict[str, bytes],
    llm: Any,
    on_progress: Callable[[str], None] | None,
    raise_if_cancelled: Callable[[], None] | None,
) -> tuple[dict[str, str], int, int]:
    """一批一批地并发去问模型，返回 {占位符: LaTeX} 和累计的 token 用量。"""

    semaphore = asyncio.Semaphore(FORMULA_OCR_CONCURRENCY)
    results: dict[str, str] = {}
    total_input = 0
    total_output = 0
    completed = 0
    # 中文注释：如果这个模型压根不支持看图（网关直接报请求不合法），那后面几十批
    # 注定全部失败，没必要一批批去撞。这里记一个开关，撞到一次就全部走兜底。
    vision_unsupported = False

    async def run_one(batch: list[PdfFormulaRegion]) -> None:
        nonlocal total_input, total_output, completed, vision_unsupported
        async with semaphore:
            # 中文注释：并发跑的时候也要能中途停下。每个任务真正开跑前查一次取消，
            # 用户点了停止就能及时退出，不用等所有批次跑完。
            if raise_if_cancelled is not None:
                raise_if_cancelled()
            if vision_unsupported:
                return
            outcome, input_tokens, output_tokens = await _transcribe_one(batch, crops, llm)
            total_input += input_tokens
            total_output += output_tokens
            if outcome is None:
                vision_unsupported = True
                return
            results.update(outcome)
            completed += 1
            if on_progress is not None:
                on_progress(f"正在识别论文公式（{completed}/{len(batches)} 批）")

    await asyncio.gather(*(run_one(batch) for batch in batches))
    return results, total_input, total_output


async def _transcribe_one(
    batch: list[PdfFormulaRegion], crops: dict[str, bytes], llm: Any
) -> tuple[dict[str, str] | None, int, int]:
    """问一次模型。返回 (占位符到 LaTeX 的对应, 输入 token, 输出 token)。

    中文注释：返回的对应是 None，表示"这个模型不支持看图"，调用方据此停止后续请求。
    返回空字典则表示"这一批没成功"，只是这一批走兜底，后面的照常尝试。
    """

    try:
        response = await llm.provider.chat(
            _build_messages(batch, crops), temperature=0, max_tokens=FORMULA_OCR_MAX_TOKENS
        )
    except Exception as exc:
        logger.warning("公式转写请求异常，这一批退回展平文本", extra={"reason": str(exc)})
        return {}, 0, 0

    usage = _usage_of(response)
    if not _is_ok(response):
        # 中文注释：请求被判为不合法（多半是网关不认图片），标记成"不支持看图"。
        if _looks_like_invalid_request(response):
            logger.warning(
                "当前模型似乎不接受图片输入，本次不再尝试公式转写",
                extra={"error_kind": getattr(response, "error_kind", None)},
            )
            return None, usage[0], usage[1]
        logger.warning(
            "公式转写失败，这一批退回展平文本",
            extra={"error_kind": getattr(response, "error_kind", None)},
        )
        return {}, usage[0], usage[1]

    parsed = await parse_llm_json(getattr(response, "content", "") or "", fallback={})
    entries = parsed.get("formulas") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        logger.warning("公式转写返回的内容不是预期的 JSON，这一批退回展平文本")
        return {}, usage[0], usage[1]

    by_index: dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[index] = str(entry.get("latex") or "")

    outcome: dict[str, str] = {}
    for position, region in enumerate(batch, start=1):
        latex = by_index.get(position, "")
        problem = _validate_latex(latex)
        if problem is not None:
            logger.warning(
                "公式转写结果没通过检查，退回展平文本",
                extra={"page": region.page_number, "index": region.index, "reason": problem},
            )
            continue
        outcome[region.placeholder] = _with_equation_tag(region, latex.strip())
    return outcome, usage[0], usage[1]


def _build_messages(
    batch: list[PdfFormulaRegion], crops: dict[str, bytes]
) -> list[dict[str, Any]]:
    """拼出这一次请求的消息。

    中文注释：图片用的是 Anthropic 那套原生格式（{"type": "image", ...}），
    项目里发消息那条链路对它是原样透传的。不要写成 OpenAI 的 image_url，
    项目里没有做那种格式的转换，写了模型收不到图。
    """

    content: list[dict[str, Any]] = []
    for position, region in enumerate(batch, start=1):
        label = f"公式 {position}（第 {region.page_number} 页）"
        if region.number:
            label += f"，论文里编号是 ({region.number})"
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
                '{"formulas":[{"index":1,"latex":"..."}]}。'
                "index 用上面给出的公式编号。认不出的把 latex 写成空字符串。"
            ),
        }
    )
    return [
        {"role": "system", "content": FORMULA_OCR_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _validate_latex(text: str) -> str | None:
    """检查模型给的 LaTeX 能不能用。能用返回 None，不能用返回原因。"""

    stripped = text.strip()
    if not stripped:
        return "空的"
    if len(stripped) > MAX_FORMULA_LATEX_CHARS:
        return "太长了"
    if _MATH_FENCE_PATTERN.search(stripped):
        # 中文注释：定界符由我们自己加。模型自己带 $ 的话，外面再包一层就变成
        # 嵌套的 $，前端渲染会乱。
        return "自带了 $ 定界符"
    if _CJK_PATTERN.search(stripped):
        return "含中文，像是解释文字"
    if "\n" in stripped:
        # 中文注释：$$ 块要求内容在一行里。换行会把"$$ 独占一行"的形状破坏掉，
        # 切分那一步靠成对 $$ 做的公式保护就会失灵。
        return "含换行"
    if not _MATH_SIGNAL_PATTERN.search(stripped):
        return "看不出是公式"
    return None


def _with_equation_tag(region: PdfFormulaRegion, latex: str) -> str:
    """模型漏写公式编号时，把我们从原文里认到的编号补上。

    中文注释：正文里经常写"见式(2)"，公式要是没带编号，模型就对不上号。
    优先信模型自己写的 \\tag；它没写、而我们又从原文里认到了编号，就补一个。
    """

    if not region.number or "\\tag" in latex:
        return latex
    separator = "" if latex.endswith((",", ".")) else " "
    return f"{latex}{separator}\\tag{{{region.number}}}"


def _is_ok(response: Any) -> bool:
    """这次请求算不算成功。"""

    # LLMResponse.ok 是个只读属性，直接取就是布尔值。
    ok = getattr(response, "ok", None)
    if ok is None:
        return not getattr(response, "error_kind", None)
    return bool(ok)


def _looks_like_invalid_request(response: Any) -> bool:
    """这次失败是不是"请求本身不合法"（多半是这个模型不认图片）。"""

    status = getattr(response, "error_status_code", None)
    if status in (400, 415, 422):
        return True
    return getattr(response, "error_kind", None) in {"invalid_request", "unsupported_content"}


def _usage_of(response: Any) -> tuple[int, int]:
    """从响应里取这次用掉多少 token。

    中文注释：不同厂商的字段名不一样，Anthropic 叫 input/output_tokens，
    OpenAI 那边叫 prompt/completion_tokens，这里两种都认。
    """

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
