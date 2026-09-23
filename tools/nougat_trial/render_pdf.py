"""用已经装好的 Nougat 把一篇 PDF 按页写成 Markdown。

这个脚本跑在 tools/nougat_trial 的单独环境里，主程序不要直接 import 它。
模型只加载一次。每一页前面写上 <!-- page: N -->，方便后面按页装箱。
Nougat 自己的公式定界是 \\[ \\] 和 \\( \\)，这里换成 $$ 和 $，
现有的分块才认得哪一块是公式。
"""

from __future__ import annotations

import argparse
import re
from functools import partial
from pathlib import Path

import pypdfium2
import torch
from PIL import Image

from nougat import NougatModel
from nougat.postprocessing import markdown_compatible
from nougat.utils.checkpoint import get_checkpoint
from nougat.utils.device import move_to_device


def main() -> None:
    """读命令行参数，写出带页码的 Markdown。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.pdf.exists():
        raise SystemExit(f"找不到 PDF：{args.pdf}")
    text = render_pdf(args.pdf)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")


def render_pdf(pdf_path: Path) -> str:
    """把每一页交给 Nougat，拼成一份带页码的 Markdown。"""

    checkpoint = get_checkpoint(None, model_tag="0.1.0-small")
    model = NougatModel.from_pretrained(checkpoint)
    use_gpu = torch.cuda.is_available()
    model = move_to_device(model, bf16=use_gpu, cuda=use_gpu)
    model.eval()
    prepare = partial(model.encoder.prepare_input, random_padding=False)

    pieces: list[str] = []
    document = pypdfium2.PdfDocument(str(pdf_path))
    try:
        for index in range(len(document)):
            page_number = index + 1
            image = _render_page(document, index)
            body = ""
            if image is not None:
                tensor = prepare(image)
                if tensor is not None:
                    if tensor.dim() == 3:
                        tensor = tensor.unsqueeze(0)
                    result = model.inference(image_tensors=tensor, early_stopping=True)
                    predictions = result.get("predictions") or []
                    if predictions:
                        body = str(predictions[0] or "")
            if body.strip() == "[MISSING_PAGE_POST]":
                body = ""
            if body.strip():
                body = _to_dollar_math(markdown_compatible(body))
            pieces.append(f"<!-- page: {page_number} -->\n\n{body.strip()}".rstrip())
    finally:
        document.close()
    return "\n\n".join(pieces).strip() + "\n"


def _render_page(document: pypdfium2.PdfDocument, index: int) -> Image.Image | None:
    """把一页 PDF 画成图。画不出来就跳过这一页，页码仍然留着。"""

    try:
        renderer = document.render(
            pypdfium2.PdfBitmap.to_pil,
            page_indices=[index],
            scale=96 / 72,
        )
        return next(iter(renderer))
    except Exception:
        return None


def _to_dollar_math(text: str) -> str:
    """把 Nougat 的 \\[ \\] 和 \\( \\) 换成现有分块认得的 $$ 和 $。"""

    text = re.sub(
        r"\\\[(.*?)\\\]",
        lambda match: "\n\n$$\n" + match.group(1).strip() + "\n$$\n\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\\\((.*?)\\\)",
        lambda match: "$" + match.group(1).strip() + "$",
        text,
        flags=re.DOTALL,
    )
    return text


if __name__ == "__main__":
    main()
