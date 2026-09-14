"""访问 arXiv 时共用的身份、限速和路径规则。

中文说明：
arXiv 对脚本访问很敏感。以前检索、下 PDF 各走各的 HTTP 客户端：
有的请求没带自己的名字、有的一次发出好几个、有的可能打到不允许的路径。
结果就是 429、连接被掐、403，看起来像「arXiv 上搜不到论文」。

这个文件把规则收成一处，检索和下 PDF 都走这里：
1. 每次请求带上程序名字和来源页，避免被当成无名脚本；
2. 同一时刻只允许 1 个请求在飞，避免多线程一起打；
3. 检索接口每秒最多 1 次；论文站点按 robots.txt 两次至少隔 15 秒；
4. 只访问允许的路径：检索走官方查询接口，全文只下 /pdf、/html、/abs，
   不去 /search、/find、源码包等禁止路径。

检索不用网页搜索页，也不用 OAI-PMH。OAI-PMH 是把整库元数据收走的接口，
不适合用户按主题找几篇论文；网页搜索页在 robots.txt 里是禁止的。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator
from urllib.parse import urlparse


# 官方检索接口。只查元数据，不解析 arXiv 网页。
ARXIV_SEARCH_ENDPOINT = "https://export.arxiv.org/api/query"

# 中文说明：写明这是 Paper-Agent 的学术检索请求，不要用空的或浏览器默认名字。
# Referer 告诉对方请求从 arXiv 站点过来，减少被直接拒绝的情况。
ARXIV_USER_AGENT = "Paper-Agent/1.0 (academic paper retrieval; +https://arxiv.org/help/api)"
ARXIV_REFERER = "https://arxiv.org/"

# 检索接口：每秒不超过 1 次。
_EXPORT_MIN_INTERVAL = 1.0
# 论文站点（arxiv.org）robots.txt 对普通访问者要求两次间隔 15 秒。
_SITE_MIN_INTERVAL = 15.0

# 检索主机只允许查询接口这一条路径。
_EXPORT_ALLOWED_PREFIXES = ("/api/query",)
# 论文站点只允许摘要页、PDF、HTML 全文。其它路径一律不发请求。
_SITE_ALLOWED_PREFIXES = ("/abs", "/pdf", "/html")

_sync_lock = threading.Lock()
_async_lock: asyncio.Lock | None = None
_last_start = {"export": 0.0, "site": 0.0}


class ArxivAccessDenied(RuntimeError):
    """这个地址不在允许访问的路径里，发请求前就拦住。"""


def arxiv_headers(*, accept: str) -> dict[str, str]:
    """拼出访问 arXiv 时必须带上的请求头。"""

    return {
        "User-Agent": ARXIV_USER_AGENT,
        "Referer": ARXIV_REFERER,
        "Accept": accept,
    }


def is_arxiv_url(url: str) -> bool:
    """判断地址是不是打到 arXiv 名下的主机。"""

    return _host_kind(url) is not None


def is_allowed_arxiv_url(url: str) -> bool:
    """判断这个 arXiv 地址能不能发。不是 arXiv 的地址视为与本规则无关，返回 True。"""

    kind = _host_kind(url)
    if kind is None:
        return True
    path = urlparse(url).path or "/"
    prefixes = _EXPORT_ALLOWED_PREFIXES if kind == "export" else _SITE_ALLOWED_PREFIXES
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


@contextmanager
def arxiv_request_slot(url: str) -> Iterator[None]:
    """同步请求排队：检查路径、限速，并独占到这次请求结束。"""

    kind = _prepare_kind(url)
    if kind is None:
        yield
        return
    with _sync_lock:
        _wait_locked(kind)
        yield


@asynccontextmanager
async def arxiv_request_slot_async(url: str) -> AsyncIterator[None]:
    """异步请求排队：检查路径、限速，并独占到这次请求结束。"""

    kind = _prepare_kind(url)
    if kind is None:
        yield
        return
    async with _get_async_lock():
        await _wait_locked_async(kind)
        yield


def _prepare_kind(url: str) -> str | None:
    """不是 arXiv 就放行；是 arXiv 但不在允许路径里就直接拒绝。"""

    kind = _host_kind(url)
    if kind is None:
        return None
    if not is_allowed_arxiv_url(url):
        raise ArxivAccessDenied(f"arXiv 不允许访问这条路径：{url}")
    return kind


def _host_kind(url: str) -> str | None:
    """区分检索接口主机和论文站点。认不出来就不是 arXiv。"""

    host = (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    if host == "export.arxiv.org":
        return "export"
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return "site"
    return None


def _interval_for(kind: str) -> float:
    """检索接口 1 秒一次，论文站点 15 秒一次。"""

    return _EXPORT_MIN_INTERVAL if kind == "export" else _SITE_MIN_INTERVAL


def _wait_locked(kind: str) -> None:
    """已经拿到同步锁之后：等到这个主机种类的间隔够了再记下本次开始时间。"""

    last = _last_start[kind]
    if last > 0:
        wait_seconds = _interval_for(kind) - (time.monotonic() - last)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    _last_start[kind] = time.monotonic()


async def _wait_locked_async(kind: str) -> None:
    """已经拿到异步锁之后：等到这个主机种类的间隔够了再记下本次开始时间。"""

    last = _last_start[kind]
    if last > 0:
        wait_seconds = _interval_for(kind) - (time.monotonic() - last)
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
    _last_start[kind] = time.monotonic()


def _get_async_lock() -> asyncio.Lock:
    """取出异步锁。锁必须在已经有事件循环时才创建，所以第一次用到再新建。"""

    global _async_lock
    if _async_lock is None:
        _async_lock = asyncio.Lock()
    return _async_lock
