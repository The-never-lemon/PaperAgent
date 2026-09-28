"""智能体包。

按职责分成四块，用的时候直接 import 具体模块：
- common：所有智能体共用的骨架（基类、工具登记、提示词）
- research：主对话
- reading：看摘要、精读、追问
- review：综述流水线
"""

from src.agents.common.base import AgentContext, AgentSpec, BaseAgent
from src.agents.common.tool_registry import Tool, ToolRegistry, ToolSpec, not_implemented_tool
from src.agents.reading.relevance import AbstractReadResult, ReadAgent, build_read_agent

__all__ = [
    "AgentContext",
    "AgentSpec",
    "AbstractReadResult",
    "BaseAgent",
    "ReadAgent",
    "Tool",
    "ToolRegistry",
    "ToolSpec",
    "build_read_agent",
    "not_implemented_tool",
]
