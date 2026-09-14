from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.paper_retrieval.models import PaperDocument


JsonObject = dict[str, Any]

# 缓存目录里"原始全文文件"允许使用的名字，按优先级排列。
# 中文注释：不管是网上下回来的，还是用户自己上传的，一篇论文的全文在磁盘上就是
# 这个目录里的某一个文件。找全文的地方（paper_retrieval/download.py）会先在这个
# 元组里挨个找一圈，找到就用本地这份、不再联网。
# 注意：写文件和找文件的地方都必须引用这个元组，不要再各自手写文件名字符串。
# 两处写得不一样，就会出现"明明本地有全文却被当成没有"，于是白跑一次网络下载，
# 下不动的时候还会退化成只看着摘要瞎猜的报告。
CACHED_FULLTEXT_NAMES = ("original.pdf", "original.html", "source.pdf", "source.html")

# 新存一份 PDF 全文时统一使用的文件名。用户上传本地 PDF 时也用它。
# 中文注释：它必须是上面那个元组里的第一个，否则"刚存进去的文件"不会被找回来。
PRIMARY_PDF_NAME = "original.pdf"


def paper_cache_dir(base_dir: str | Path, paper: PaperDocument) -> Path:
    """返回单篇论文的缓存目录。

    中文注释：需求里希望用 paperId 作为目录名。Windows 文件名不能包含斜杠、
    冒号这类字符，所以这里只做很薄的一层清理，不再用哈希隐藏原始编号。
    同一篇论文后来换了编号时，请用 src.services.paper_memory.resolve_paper_cache_dir
    先查长期记忆里记下的目录，再退回这个按当前编号起名的结果。
    """

    paper_id = str(paper.paperId or paper.id or paper.doi or paper.title).strip()
    return Path(base_dir) / safe_cache_name(paper_id)


def safe_cache_name(value: str) -> str:
    """把论文编号变成适合放进路径里的名字。

    中文注释：这里保留字母、数字、短横线、下划线和点号；其它字符统一换成
    下划线。这样目录名仍然能看出 paperId，大多数情况下也能直接复制查看。
    """

    cleaned = "".join(character if character.isalnum() or character in {"-", "_", "."} else "_" for character in value)
    return cleaned[:160] or "paper"


def write_metadata(cache_dir: Path, paper: PaperDocument, *, source_url: str | None, content_type: str | None) -> Path:
    """把论文元数据写入缓存目录里的 metadata.json。

    中文注释：只有下载到全文后才会调用这个函数，所以不会给下载失败的论文
    创建空缓存。metadata 里同时放论文原始信息和全文来源，后面分析节点不用
    再回头猜这篇论文是从哪里来的。
    """

    payload: JsonObject = {
        "paperId": paper.paperId or paper.id,
        "paper": paper.to_dict(),
        "fulltext": {
            "source_url": source_url,
            "content_type": content_type,
        },
    }
    path = cache_dir / "metadata.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_cached_source_url(cache_dir: Path) -> str | None:
    """从缓存里的 metadata.json 读回全文来源地址。

    中文注释：旧缓存里可能还有 source.json，所以这里顺手兼容一下。主流程新写入
    的都是 metadata.json。
    """

    for name in ("metadata.json", "source.json"):
        try:
            payload = json.loads((cache_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fulltext = payload.get("fulltext") if isinstance(payload, dict) else None
        if isinstance(fulltext, dict) and fulltext.get("source_url"):
            return str(fulltext["source_url"])
        if isinstance(payload, dict) and payload.get("source_url"):
            return str(payload["source_url"])
    return None
