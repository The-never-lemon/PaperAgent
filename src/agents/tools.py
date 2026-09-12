from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


JsonObject = dict[str, Any]


class ToolCallable(Protocol):
    def __call__(self, **kwargs: Any) -> Any:
        # 中文注释：handler 既可以写成普通同步函数，也可以写成 async 函数。
        # 同步调用方走 Tool.call，异步调用方走 Tool.acall（会自动 await 协程结果）。
        ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Agent 可调用工具的声明，后续可直接映射成 LLM function schema。"""

    name: str
    description: str
    parameters_schema: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class Tool:
    spec: ToolSpec
    handler: ToolCallable

    def call(self, **kwargs: Any) -> Any:
        return self.handler(**kwargs)

    async def acall(self, **kwargs: Any) -> Any:
        """异步执行工具，handler 是同步函数还是 async 函数都可以。

        中文注释：对话式调研的工具 handler 几乎都是 async 的（要等网络检索、
        模型调用完成），主循环统一走这个入口；旧的同步工具不受影响，
        这里检测到协程结果就替调用方 await 掉。
        """

        result = self.handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


class ToolRegistry:
    """按名称管理工具；编排器会为每个 Agent 注入它声明过的工具子集。"""

    def __init__(self, tools: dict[str, Tool] | None = None):
        self._tools = dict(tools or {})

    def register(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def require(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {name}") from exc

    def select(self, names: tuple[str, ...]) -> "ToolRegistry":
        # 只把当前 Agent 声明过的工具传进去，避免工具能力在步骤之间泄漏。
        return ToolRegistry({name: self.require(name) for name in names})

    def as_llm_tools(self) -> list[JsonObject]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.parameters_schema,
                },
            }
            for tool in self._tools.values()
        ]


def not_implemented_tool(name: str, description: str, parameters_schema: JsonObject | None = None) -> Tool:
    """生成占位工具，先把架构接口立住，真实数据库接入后替换 handler 即可。"""

    def _handler(**_: Any) -> Any:
        raise NotImplementedError(f"tool {name} is not implemented")

    return Tool(ToolSpec(name, description, parameters_schema or {}), _handler)
