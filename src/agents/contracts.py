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
