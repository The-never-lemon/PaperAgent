"""用 Nougat 试读几页论文，看独立公式能不能写成 $$。

主程序的公式识别仍然是截图交给精读模型。这里单独开一个环境，
是因为 Nougat 要的旧依赖装进主程序会把现有的包拽乱。

用法（在仓库根目录）：
    uv run python scripts/try_nougat.py

环境还没建时，先在仓库根目录执行：
    uv sync --project tools/nougat_trial

第一次会下载模型权重。结果写在 data/nougat_trial/，不改精读用的公式转写。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
# 中文注释：这个环境和主程序的 .venv 分开，装在试验目录下面。
NOUGAT = ROOT / "tools" / "nougat_trial" / ".venv" / "Scripts" / "nougat.exe"
OUT_ROOT = ROOT / "data" / "nougat_trial"

# 中文注释：两篇都是已经下在本地的 PDF。页码按论文印出来的页，从 1 起。
TRIALS = [
    {
        "name": "lra_2023_p4",
        "pdf": ROOT / "data" / "paper_cache" / "10.1109_LRA.2023.3331892" / "original.pdf",
        "pages": "4",
    },
    {
        "name": "arxiv_2406_03482_p1-2",
        "pdf": ROOT / "data" / "paper_cache" / "10.48550_arXiv.2406.03482" / "original.pdf",
        "pages": "1-2",
    },
]


def main() -> int:
    """跑完两篇试读，并把认出来的公式打在屏幕上。"""

    if not NOUGAT.exists():
        print("还没有 Nougat 环境。请先在仓库根目录执行：")
        print("    uv sync --project tools/nougat_trial")
        return 1
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    failed = False
    for trial in TRIALS:
        pdf = trial["pdf"]
        if not pdf.exists():
            print(f"找不到 PDF：{pdf}")
            failed = True
            continue
        out_dir = OUT_ROOT / trial["name"]
        out_dir.mkdir(parents=True, exist_ok=True)
        # 中文注释：两篇缓存文件都叫 original.pdf。分开目录，避免后一篇盖掉前一篇。
        # -m 用较小的模型，-b 1 是一次只看一页，显存更稳。
        command = [
            str(NOUGAT),
            str(pdf),
            "-o",
            str(out_dir),
            "-p",
            trial["pages"],
            "-m",
            "0.1.0-small",
            "-b",
            "1",
            "--recompute",
        ]
        print("开始试读", trial["name"], "页", trial["pages"])
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode != 0:
            print("这一篇没有跑完，退出码", completed.returncode)
            failed = True
            continue
        written = out_dir / "original.mmd"
        if not written.exists():
            print("没有写出结果文件", written)
            failed = True
            continue
        _print_formulas(trial["name"], written)
    return 1 if failed else 0


def _print_formulas(name: str, path: Path) -> None:
    """把结果里的公式摘出来，方便直接看认没认出来。"""

    text = path.read_text(encoding="utf-8")
    if "MISSING_PAGE" in text:
        print(name, "有页面被跳过：", path)
    # 中文注释：Nougat 的独立公式是 \[...\]，不是 $$。两种都数，避免看成没认出来。
    blocks = re.findall(r"\\\[(.+?)\\\]", text, flags=re.DOTALL)
    blocks += re.findall(r"\$\$(.+?)\$\$", text, flags=re.DOTALL)
    print(f"{name}：独立公式 {len(blocks)} 条，全文在 {path}")
    for index, block in enumerate(blocks, start=1):
        one_line = " ".join(block.split())
        print(f"  ({index}) {one_line[:240]}")


if __name__ == "__main__":
    sys.exit(main())
