# 文件作用：定义智能体之间共享的类型名称和返回结构约束。
from __future__ import annotations

from typing import Any, Literal


JsonObject = dict[str, Any]
AgentRole = Literal[
    "search",
    "screen",
    "read",
    "analyze",
    "plan",
    "write",
    "critique",
]
