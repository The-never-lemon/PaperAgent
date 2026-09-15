# -*- coding: utf-8 -*-
"""一次性实测脚本：验证 OpenCode Go 网关对 parallel_tool_calls 的透传效果。

发两次带工具定义的请求做对照：
1. 显式 parallel_tool_calls=True + 提示词引导并行；
2. 不发该参数 + 同样提示词（基线）。
观察每次响应里 tool_calls 数量是否 > 1。
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")
from src.agents.Prompts import RESEARCH_AGENT_SYSTEM_PROMPT
from src.agents.research_tools import build_research_tool_registry
from src.llm.config import ModelConfig, SystemConfig
from src.llm.factory import make_provider


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_papers",
            "description": "按概念组检索学术论文",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某个城市今天的天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
]

USER = (
    "帮我同时处理两件完全独立的事：1) 检索『diffusion model 训练稳定性』方向的论文；"
    "2) 查北京今天的天气。请在一次回复里同时调用这两个工具，不要串行。"
)


async def probe(tag, send_parallel_flag):
    print("=" * 15, tag, f"发送 parallel_tool_calls={send_parallel_flag}", "=" * 15)
    cfg = ModelConfig.from_dict(json.loads(open("config/model.json", encoding="utf-8").read()), SystemConfig.load())
    snap = make_provider(cfg, "default_agent")
    provider = snap.provider
    try:
        if send_parallel_flag:
            # 走改动后的正常路径（_build_kwargs 现在会带上 parallel_tool_calls=True）
            resp = await provider.chat(
                [{"role": "user", "content": USER}],
                tools=TOOLS,
                temperature=0.3,
                max_tokens=2048,
            )
        else:
            # 基线：手工还原不带该参数的请求
            kwargs = provider._build_kwargs(
                [{"role": "user", "content": USER}], TOOLS, False, 0.3, 2048, None
            )
            kwargs.pop("parallel_tool_calls", None)
            from src.llm.openai_compat import _maybe_await
            raw = await _maybe_await(provider.client.chat.completions.create(**kwargs))
            resp = provider._parse_response(raw)
        calls = resp.tool_calls or []
        print("tool_calls 数量:", len(calls))
        for c in calls:
            print("  -", c.name, "args=", str(c.arguments)[:120])
        print("finish_reason:", resp.finish_reason)
        print("content[:100]:", (resp.content or "")[:100])
        if not resp.ok:
            print("错误:", resp.error_kind, str(resp.error)[:200])
        return len(calls)
    finally:
        await snap.aclose()


async def probe_stream(tag):
    """流式路径探针：主 Agent 实际走 chat_stream，验证流式分片能否聚合出多条 tool_calls。"""
    print("=" * 15, tag, "(流式)", "=" * 15)
    cfg = ModelConfig.from_dict(json.loads(open("config/model.json", encoding="utf-8").read()), SystemConfig.load())
    snap = make_provider(cfg, "default_agent")
    provider = snap.provider

    class _Callbacks:
        on_content_delta = None
        on_thinking_delta = None
        on_tool_call_delta = None

    try:
        resp = await provider.chat_stream(
            [{"role": "user", "content": USER}],
            _Callbacks(),
            tools=TOOLS,
            temperature=0.3,
            max_tokens=2048,
        )
        calls = resp.tool_calls or []
        print("流式聚合后 tool_calls 数量:", len(calls))
        for c in calls:
            print("  -", c.name, "args=", str(c.arguments)[:120])
        print("finish_reason:", resp.finish_reason)
        return len(calls)
    finally:
        await snap.aclose()


async def probe_real(tag):
    """贴近真实场景探针：主 Agent 的系统提示词 + 工具注册表里的真实工具定义。"""
    print("=" * 15, tag, "(真实提示词+真实工具)", "=" * 15)
    cfg = ModelConfig.from_dict(json.loads(open("config/model.json", encoding="utf-8").read()), SystemConfig.load())
    snap = make_provider(cfg, "default_agent")
    provider = snap.provider
    try:
        # 只是借用注册表里的工具定义（as_llm_tools），不真正执行任何工具，
        # handler 不会被调用，所以用一个空壳 context 即可。
        from src.agents.research_tools import ResearchToolContext
        from src.models.workspace import SessionWorkspace

        class _Repo:
            pass

        registry = build_research_tool_registry(
            ResearchToolContext(
                session_key="probe",
                turn_id="probe",
                run_id=None,
                workspace=SessionWorkspace(session_key="probe"),
                repo=_Repo(),
                reporter=None,
            )
        )
        tools = registry.as_llm_tools()
        sys_prompt = RESEARCH_AGENT_SYSTEM_PROMPT
        user = "我最近在调研两个方向，帮我同时检索一下：1) diffusion model 的训练稳定性技巧；2) KV cache 压缩加速。近两年的论文，每个方向先来 10 篇左右就好。"
        resp = await provider.chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            tools=tools,
            temperature=0.3,
            max_tokens=4096,
        )
        calls = resp.tool_calls or []
        print("tool_calls 数量:", len(calls))
        for c in calls:
            print("  -", c.name, "args=", str(c.arguments)[:150])
        print("finish_reason:", resp.finish_reason)
        print("content[:120]:", (resp.content or "")[:120])
        if not resp.ok:
            print("错误:", resp.error_kind, str(resp.error)[:200])
        return len(calls)
    finally:
        await snap.aclose()


async def main():
    with_flag = await probe("实验组", True)
    baseline = await probe("对照组", False)
    stream_count = await probe_stream("流式组")
    real_count = await probe_real("真实场景组")
    print()
    print("结论: 实验组 =", with_flag, "| 对照组 =", baseline, "| 流式组 =", stream_count, "| 真实场景组 =", real_count)


if __name__ == "__main__":
    asyncio.run(main())
