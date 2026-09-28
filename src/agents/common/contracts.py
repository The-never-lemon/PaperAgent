# 这个文件放智能体之间共用的类型名字，从 src/agents/contracts.py 挪到这里。
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
