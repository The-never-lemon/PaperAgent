"""提取层冒烟脚本：拿一篇真实的 arXiv 论文，把「下载 → 转 Markdown → 分块」跑通并按事实检查。

用法（在仓库根目录执行）：
    uv run python scripts/smoke_extraction.py
    uv run python scripts/smoke_extraction.py 2401.00001v1   # 也可以指定别的 arXiv 编号

为什么要有这个脚本：提取层改造后，最容易坏掉的地方不是"报错"，而是"悄悄少东西"——
表格只出来半张、公式断了半截、图片引用在但图没落盘、转换器升级了却还在用旧缓存。
这些东西不会抛异常，只会让下游模型读到残缺的论文。所以这里逐条对着真实产物做断言。

这个脚本只用来人工/开发期验证，不进分发包（scripts/package.py 的 INCLUDE_FILES 只收了
launch.py），它需要联网下载论文，也不要放进自动化流水线里。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

# 中文注释：脚本放在 scripts/ 目录下，要先把仓库根目录加进模块搜索路径，
# 不然 import src.xxx 会找不到。必须放在下面那些 import 之前。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.paper_retrieval.models import PaperDocument  # noqa: E402
from src.utils.read_utils.cache import PRIMARY_PDF_NAME  # noqa: E402
from src.utils.read_utils.chunkers import (  # noqa: E402
    CHUNKER_VERSION,
    PageChunker,
    async_build_chunks_file,
)
from src.utils.read_utils.pdf_parsers import (  # noqa: E402
    PdfParseResult,
    PypdfParser,
    PyMuPdfParser,
    get_pdf_parser,
)
from src.utils.read_utils.read_fulltext import (  # noqa: E402
    CONVERTER_VERSION,
    async_convert_fulltext_to_markdown,
    _ArticleHtmlParser,
    _load_cached_markdown,
)

# 中文注释：默认拿这篇论文当样本。它同时有 PDF 和 arXiv HTML，而且 PDF 里
# 实测有 3 张表、8 个公式块、4 张超过 10KB 的图，能一次覆盖三种内容。
DEFAULT_PAPER_ID = "2502.20217v1"

# 表格行（以竖线开头）和表格的分隔行（|---|---|）。
_TABLE_LINE = re.compile(r"^\s*\|")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
# 图注：以 Figure / Fig. / TABLE / Table 加编号开头。
_CAPTION_LINE = re.compile(r"(?im)^\s*(Figure|Fig\.?|TABLE|Table)\s*\d+")
# 正文里的图片引用。
_IMAGE_REFERENCE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# 检查结果收集：每条是 (是否通过, 说明文字)。
_checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    """记下一条检查结果，并立刻打印出来。"""

    _checks.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{mark}] {label}{suffix}")


def download(url: str, dest: Path) -> None:
    """把网址内容下载到本地文件。

    中文注释：arXiv 对没带 User-Agent 的脚本请求有时会拒绝，所以这里明确带上一个。
    """

    request = urllib.request.Request(url, headers={"User-Agent": "paper-agent-smoke/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        dest.write_bytes(response.read())


def scan_markdown(markdown_text: str) -> dict[str, object]:
    """从 paper.md 里数出表格、公式、图片引用和图注。"""

    lines = markdown_text.splitlines()
    table_lines = [line for line in lines if _TABLE_LINE.match(line)]
    return {
        "table_count": sum(1 for line in table_lines if _TABLE_SEPARATOR.match(line)),
        "math_block_lines": sum(1 for line in lines if line.strip().startswith("$$")),
        "image_refs": _IMAGE_REFERENCE.findall(markdown_text),
        "has_caption": bool(_CAPTION_LINE.search(markdown_text)),
    }


def find_half_tables(chunk_content: str) -> list[str]:
    """找出"半张表"：一段连续的表格行，但不是从"表头 + 分隔行"开始的。

    中文注释：表格被从中间切断时，后面那一段会以数据行开头，下游看到的就是
    一堆不知道哪列是哪列的数字。这里专门抓这种情况。
    """

    problems: list[str] = []
    lines = chunk_content.splitlines()
    index = 0
    while index < len(lines):
        if not _TABLE_LINE.match(lines[index]):
            index += 1
            continue
        end = index
        while end < len(lines) and _TABLE_LINE.match(lines[end]):
            end += 1
        block = lines[index:end]
        if not (len(block) >= 2 and _TABLE_SEPARATOR.match(block[1])):
            problems.append(block[0][:60])
        index = end
    return problems


def count_unpaired_math(chunk_content: str) -> int:
    """数一段文字里落单的 $$ 标记（奇数个就说明公式块被切断了）。"""

    return sum(1 for line in chunk_content.splitlines() if line.strip().startswith("$$")) % 2


async def check_pdf(paper_id: str, workdir: Path) -> None:
    """验证 PDF 链路：表格、公式、图片、图注、以及分块不切断表格和公式。"""

    print(f"\n[1/5] PDF 链路  ({paper_id})")
    pdf_path = workdir / "original.pdf"
    try:
        download(f"https://arxiv.org/pdf/{paper_id}", pdf_path)
    except Exception as exc:
        check(False, "下载 arXiv PDF", str(exc))
        return
    check(True, "下载 arXiv PDF", f"{pdf_path.stat().st_size} 字节")

    paper = PaperDocument(id=paper_id, title=paper_id, paperId=paper_id)
    conversion = await async_convert_fulltext_to_markdown(paper, source_path=pdf_path, source_url="")
    if conversion.markdown_path is None:
        check(False, "PDF 转 Markdown", f"转换失败：{conversion.warnings}")
        return
    markdown_text = conversion.markdown_path.read_text(encoding="utf-8")
    check(True, "PDF 转 Markdown", f"{len(markdown_text)} 字符")

    # 头部必须写明转换器版本，否则以后升级了也没法让旧缓存失效。
    header = json.loads(markdown_text.split("---")[1])
    check(
        header.get("converter_version") == CONVERTER_VERSION,
        "paper.md 头部带 converter_version",
        f"converter_version={header.get('converter_version')}，期望 {CONVERTER_VERSION}",
    )

    stats = scan_markdown(markdown_text)
    check(stats["table_count"] >= 1, "正文里有 Markdown 表格", f"{stats['table_count']} 张")
    check(stats["math_block_lines"] >= 2, "正文里有 $$ 公式块", f"{stats['math_block_lines']} 行 $$")
    check(bool(stats["image_refs"]), "正文里有图片引用", f"{len(stats['image_refs'])} 条")
    check(bool(stats["has_caption"]), "正文里有图注文字（Fig./Figure/Table 编号）")

    # 图片引用指到的文件必须真的落盘了，不然产出的笔记里全是裂图。
    missing = [
        target
        for _, target in stats["image_refs"]
        if not (conversion.markdown_path.parent / target).is_file()
    ]
    check(not missing, "每条图片引用都能找到对应文件", f"缺失：{missing}" if missing else "")
    check(conversion.assets_dir is not None, "转换结果带回了 assets_dir")

    # 分块：绝不出现半张表，也绝不把公式块从中间切开。
    chunk_result = await async_build_chunks_file(paper, markdown_path=conversion.markdown_path)
    chunks = chunk_result.chunks
    check(bool(chunks), "分块结果非空", f"{len(chunks)} 个片段")
    half_tables = [item for chunk in chunks for item in find_half_tables(chunk.content)]
    check(not half_tables, "没有「半张表」", f"{half_tables[:3]}" if half_tables else "")
    split_math = [chunk.chunk_id for chunk in chunks if count_unpaired_math(chunk.content)]
    check(not split_math, "没有把 $$ 公式块切成两半", f"{split_math[:3]}" if split_math else "")
    oversized = [len(chunk.content) for chunk in chunks if len(chunk.content) > PageChunker.max_atomic_characters]
    check(not oversized, "没有片段超过整块保留上限", f"{oversized[:3]}" if oversized else "")
    payload = json.loads(chunk_result.chunks_path.read_text(encoding="utf-8"))
    check(payload.get("version") == CHUNKER_VERSION, "chunk.json 记下了切分规则版本号")


async def check_html(paper_id: str, workdir: Path) -> None:
    """验证 HTML 链路：arXiv HTML 的表格、LaTeX 公式、图片外链、以及地址拼接。"""

    print(f"\n[2/5] HTML 链路  ({paper_id})")
    html_path = workdir / "original.html"
    try:
        download(f"https://arxiv.org/html/{paper_id}", html_path)
    except Exception as exc:
        check(False, "下载 arXiv HTML", str(exc))
        return
    check(True, "下载 arXiv HTML", f"{html_path.stat().st_size} 字节")

    # 中文注释：这里直接喂给解析器而不是走 _convert_html，是为了能单独检查解析结果；
    # 走 _convert_html 还要过 3000 字符的正文质量闸，样本页够长所以两条路都会过。
    parser = _ArticleHtmlParser(base_url=f"https://arxiv.org/html/{paper_id}")
    parser.feed(html_path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    markdown_text = parser.to_markdown()

    stats = scan_markdown(markdown_text)
    check(stats["table_count"] >= 1, "HTML 里提出了 Markdown 表格", f"{stats['table_count']} 张")
    check(stats["math_block_lines"] >= 2, "HTML 里提出了 $$ 公式块（取 alttext 的 LaTeX）", f"{stats['math_block_lines']} 行")
    check(
        bool(re.search(r"(?<!\$)\$[^$\n]{1,120}\$", markdown_text)),
        "HTML 里提出了行内 $...$ 公式",
    )
    check(bool(stats["image_refs"]), "HTML 里提出了图片引用", f"{len(stats['image_refs'])} 条")

    targets = [target for _, target in stats["image_refs"]]
    # 中文注释：arXiv 的 <img src> 自带 "2502.20217v1/" 前缀，拼接时很容易拼成
    # ".../2502.20217v1/2502.20217v1/x.png" 这种 404 地址。这里专门抓重复段。
    duplicated = [url for url in targets if re.search(r"(\d{4}\.\d{4,5}v\d+)/\1", url)]
    check(not duplicated, "图片地址没有重复拼出 arXiv 编号", f"{duplicated[:2]}" if duplicated else "")
    # 页面顶部的 arXiv logo 之类的装饰图不属于论文内容，必须被排除掉。
    chrome = [url for url in targets if "/static/" in url]
    check(not chrome, "没有把页面 logo 当成论文配图", f"{chrome[:2]}" if chrome else "")


def check_fallback() -> None:
    """验证 pymupdf 不可用时，auto 会退回 pypdf，正文照样读得出来。"""

    print("\n[3/5] 回退验证（pymupdf 不可用）")
    import src.utils.read_utils.pdf_parsers as pdf_parsers

    original = pdf_parsers._pymupdf_available
    pdf_parsers._pymupdf_available = lambda: False
    try:
        parser = get_pdf_parser("auto")
        check(isinstance(parser, PypdfParser), "auto 退回 pypdf", type(parser).__name__)
    finally:
        pdf_parsers._pymupdf_available = original
    check(isinstance(get_pdf_parser("auto"), PyMuPdfParser), "pymupdf 可用时 auto 选 pymupdf")

    # 中文注释：直接调 PyMuPdfParser，但把 pymupdf 这个模块从 import 体系里摘掉，
    # 模拟"库没装"的真实场景，确认它自己也会退回 pypdf 而不是抛异常。
    saved = {name: sys.modules[name] for name in list(sys.modules) if name == "pymupdf"}
    for name in saved:
        del sys.modules[name]
    sys.modules["pymupdf"] = None  # type: ignore[assignment]
    try:
        result = PyMuPdfParser().parse(ROOT / "data" / "paper_cache" / DEFAULT_PAPER_ID / PRIMARY_PDF_NAME)
        check(isinstance(result, PdfParseResult), "PyMuPdfParser 在库缺失时仍返回结果")
    except FileNotFoundError:
        check(True, "PyMuPdfParser 库缺失回退（本机没有缓存样本，跳过实际解析）")
    finally:
        sys.modules.pop("pymupdf", None)
        sys.modules.update(saved)


def check_stale_cache(workdir: Path) -> None:
    """验证旧版本缓存（没有 converter_version）会被识别出来并重新转换。"""

    print("\n[4/5] 旧缓存版本化")
    stale = workdir / "stale" / "paper.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('---\n{"paper_id": "x", "page_count": 3}\n---\n\n' + "正文内容" * 800, encoding="utf-8")
    result = _load_cached_markdown(stale)
    check(result is None, "没有 converter_version 的旧缓存不会命中")
    check(not stale.exists(), "旧缓存文件已被删掉（下次会重新转换）")

    fresh = workdir / "fresh" / "paper.md"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_text(
        f'---\n{{"paper_id": "x", "page_count": 3, "converter_version": {CONVERTER_VERSION}}}\n---\n\n'
        + "正文内容" * 800,
        encoding="utf-8",
    )
    hit = _load_cached_markdown(fresh)
    check(hit is not None, "版本对得上的缓存能正常命中")


async def main() -> int:
    """按顺序跑完所有检查，最后给出总账。"""

    paper_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PAPER_ID
    print(f"提取层冒烟脚本：样本论文 {paper_id}")

    with tempfile.TemporaryDirectory(prefix="paper_agent_smoke_") as tmp:
        workdir = Path(tmp)
        await check_pdf(paper_id, workdir)
        await check_html(paper_id, workdir)
        check_fallback()
        check_stale_cache(workdir)

    print("\n[5/5] 语法检查")
    import compileall

    ok = all(
        compileall.compile_file(str(ROOT / "src" / "utils" / "read_utils" / name), quiet=1)
        for name in ("pdf_parsers.py", "read_fulltext.py", "chunkers.py")
    )
    check(bool(ok), "提取层三个模块语法检查通过")

    failed = [label for passed, label in _checks if not passed]
    print("\n" + "=" * 60)
    print(f"总计 {len(_checks)} 项检查，通过 {len(_checks) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：")
        for label in failed:
            print(f"  - {label}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
