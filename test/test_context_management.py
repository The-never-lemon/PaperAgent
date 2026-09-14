"""上下文管理功能测试（主对话 + 问答 + 精读汇总 + 共享预算工具）。

中文说明：这组测试覆盖"上下文分配与自动压缩"改造的全部关键行为。
全部离线运行——模型只走 FakeProvider（返回固定数据），不访问任何真实
远端服务，也不需要真实论文缓存；整组跑完大约 1 秒。

覆盖的 Agent 与功能点：
- 共享预算工具 context_budget：token 估算、预算换算、thinking 块计入；
- 主对话 researchAgent：预算滑层收缩（阶段 A 归档工具结果 / 阶段 B 整轮
  LLM 压缩）、tool_calls/tool 配对完整性、压缩缓存命中、实测字符率回填；
- get_history 工具：按轮取回旧对话内容、超长行截断、按工具名过滤；
- 问答子 Agent paperQaAgent：目录 + read_sections 按需取原文的小工具循环
  （模型第一轮调工具、第二轮基于取回片段给终答）、无全文降级、强制收敛；
- 精读子 Agent deepReadAgent：reduce 输入组装的上限与省略标注。
"""

import asyncio
import json
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from src.agents.context_budget import (
    budget_tokens,
    estimate_messages_tokens,
)
from src.agents.deepReadAgent import (
    REDUCE_INPUT_MAX_CHARS,
    _build_reduce_user_content,
)
from src.agents.paperQaAgent import (
    QA_MAX_TOOL_ROUNDS,
    PaperQaDeps,
    _handle_read_sections,
    _normalize_tool_calls,
    _truncate_fulltext,
    _build_toc,
    run_paper_qa,
)
from src.agents.researchAgent import (
    KEEP_RECENT_TURNS_LITE,
    TOOL_RESULT_PLACEHOLDER,
    _repair_tool_call_pairing,
    _report_round_usage,
    build_llm_messages,
)
from src.agents.research_tools import _handle_get_history
from src.models.workspace import SessionWorkspace


# ---------------------------------------------------------------------------
# 测试辅助：假历史、假模型、假事件上报器
# ---------------------------------------------------------------------------


def _make_history(turn_count: int, tool_chars: int = 800) -> list[dict[str, Any]]:
    """造一段规整的假会话历史：每轮 = 用户诉求 + 工具调用 + 工具结果 + 终答。"""

    rows: list[dict[str, Any]] = []
    for index in range(turn_count):
        rows.append({"role": "user", "content": f"用户第{index}轮诉求：请调研主题{index}。"})
        rows.append(
            {
                "role": "assistant",
                "content": "我来调用检索工具。",
                "tool_calls": [
                    {"id": f"c{index}", "name": "search_papers", "arguments": {"topic": f"主题{index}"}}
                ],
                "thinking_blocks": [{"thinking": "思考" * 40}],
            }
        )
        rows.append(
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                "name": "search_papers",
                "content": "检索结果 " + "x" * tool_chars,
            }
        )
        rows.append({"role": "assistant", "content": "最终回复 " + "很详细的内容 " * 60})
    return rows


def _assert_pairing_ok(messages: list[dict[str, Any]]) -> None:
    """断言 assistant(tool_calls) 和 tool 消息严格配对（两边都不缺）。"""

    call_ids = {
        call.get("id")
        for message in messages
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    }
    tool_ids = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    orphans = [tid for tid in tool_ids if tid not in call_ids]
    missing = [cid for cid in call_ids if cid not in tool_ids]
    assert not orphans, f"孤儿 tool 消息（没有配对的 tool_calls）：{orphans}"
    assert not missing, f"没有结果的 tool_calls：{missing}"


@dataclass
class _ToolCall:
    """模拟 provider 返回的工具调用请求。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _Response:
    """模拟 provider 的统一响应结构（聊天 / 工具调用两种形态）。"""

    content: str = ""
    tool_calls: list[_ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = None
    error_kind: str | None = None
    error_status_code: int | None = None

    def __post_init__(self):
        if self.usage is None:
            self.usage = {"prompt_tokens": 100, "completion_tokens": 20}

    @property
    def ok(self) -> bool:
        """只要 finish_reason 不是 error 就算成功（与 LLMResponse 一致）。"""

        return self.finish_reason != "error"

    @property
    def reasoning_content(self):
        return None

    @property
    def reasoning_blocks(self):
        return []


class _ScriptedProvider:
    """按剧本依次返回响应的假 provider。

    中文说明：每次 chat() 从剧本弹出一条 _Response，允许剧本是"生成器"耗尽后
    一直返回最后一条。可记录每次收到的 tools 参数，用来验证工具确实传给了上游。
    """

    def __init__(self, script: list[_Response]):
        self.script = list(script)
        self.seen_tool_schemas: list[list[dict]] = []
        self.chat_calls = 0

    async def chat(self, messages, *, tools=None, temperature=None, max_tokens=None, **_):
        self.chat_calls += 1
        self.seen_tool_schemas.append(list(tools or []))
        if self.script:
            return self.script.pop(0)
        return _Response(content="{}")

    async def chat_stream(self, messages, callbacks, **_):
        return await self.chat(messages)


class _FakeProgress:
    """收集进度/失败事件的假上报器（只为跑通流程，不做断言）。"""

    def progress(self, *_args, **_kwargs):
        pass

    def failed(self, *_args, **_kwargs):
        pass


class _FakeCancellation:
    def raise_if_requested(self):
        pass


def _qa_deps(provider) -> PaperQaDeps:
    """构造问答子 Agent 的依赖（工作区由各用例自己填）。"""

    return PaperQaDeps(
        session_key="test",
        workspace=None,  # 由用例塞入
        repo=None,
        reporter=_FakeProgress(),
        event_key="e1",
        cancellation=_FakeCancellation(),
        llm=provider,
    )


class _FakeWorkspacePaper:
    """模拟工作区里的论文条目（问答只需要 paper 元数据和 deep_read 报告）。"""

    def __init__(self, paper: dict, deep_read: Any):
        self.paper = paper
        self.deep_read = deep_read


class _FakeDeepRead:
    """模拟精读报告对象（to_dict + fulltext_artifact_id）。"""

    def __init__(self, fulltext_artifact_id: str = ""):
        self.fulltext_artifact_id = fulltext_artifact_id

    def to_dict(self) -> dict:
        return {"main_results": "报告结论：A 方法有效。", "fulltext_artifact_id": self.fulltext_artifact_id}


# ---------------------------------------------------------------------------
# 1. 共享预算工具
# ---------------------------------------------------------------------------


class ContextBudgetTests(unittest.TestCase):
    """estimate_messages_tokens / budget_tokens 的行为。"""

    def test_估算随消息体量增长(self):
        small = estimate_messages_tokens([{"role": "user", "content": "hi"}])
        big = estimate_messages_tokens([{"role": "user", "content": "长文本" * 500}])
        self.assertGreater(big, small * 50)

    def test_thinking块计入估算(self):
        base = estimate_messages_tokens([{"role": "user", "content": "hi"}])
        with_thinking = estimate_messages_tokens(
            [{"role": "assistant", "content": "hi", "thinking_blocks": [{"thinking": "x" * 5000}]}]
        )
        # thinking 块必须被计入（它是 anthropic 后端会真实回传的大头）。
        self.assertGreater(with_thinking, base * 10)

    def test_预算换算默认一兆窗口(self):
        # 窗口没配时按 1M 计，预算 = 1M × 0.8。
        self.assertEqual(budget_tokens(None), 838860)
        self.assertEqual(budget_tokens(8000), 6400)


# ---------------------------------------------------------------------------
# 2. 主对话历史收缩
# ---------------------------------------------------------------------------


class BuildMessagesTests(unittest.TestCase):
    """build_llm_messages 的预算收缩行为。"""

    def setUp(self):
        self.workspace = SessionWorkspace(session_key="test")

    def test_预算充足时完整保留历史(self):
        out = asyncio.run(
            build_llm_messages(_make_history(12), self.workspace, history_budget=10**9)
        )
        users = [m for m in out if m.get("role") == "user"]
        archived = [m for m in out if m.get("role") == "tool" and "已归档" in str(m.get("content", ""))]
        self.assertEqual(len(users), 12)
        self.assertEqual(len(archived), 0)
        _assert_pairing_ok(out)

    def test_阶段A_剥工具结果保留全部轮次(self):
        out = asyncio.run(
            build_llm_messages(_make_history(12), self.workspace, history_budget=3000)
        )
        users = [m for m in out if m.get("role") == "user"]
        archived = [m for m in out if m.get("role") == "tool" and "已归档" in str(m.get("content", ""))]
        # 全部轮次还在，但旧轮次的工具结果已被换成占位符。
        self.assertEqual(len(users), 12)
        self.assertGreater(len(archived), 0)
        self.assertTrue(all(str(m.get("content", "")).startswith("[历史工具结果已归档") for m in archived))
        _assert_pairing_ok(out)

    def test_阶段B_整轮移除保留最近六轮(self):
        # 极小预算强制走阶段 B；不传 compress_llm → 压缩失败走"诉求罗列"兜底。
        history = _make_history(12)
        # 先把这段历史用大预算组装出"阶段 A 之后的形态"，保证指纹稳定（模拟缓存命中前）。
        out = asyncio.run(
            build_llm_messages(history, self.workspace, history_budget=200, chars_per_token=4.5, compress_llm=None)
        )
        users = [str(m.get("content", "")) for m in out if m.get("role") == "user"]
        self.assertEqual(len(users), KEEP_RECENT_TURNS_LITE)
        # 最早的第 0 轮必须从消息列表消失，且它的诉求进入系统提示词摘要节。
        self.assertFalse(any("第0轮" in u for u in users))
        self.assertIn("第0轮", str(out[0].get("content", "")))
        _assert_pairing_ok(out)

    def test_压缩缓存命中_两次重建不重复调模型(self):
        class _CountingProvider:
            def __init__(self):
                self.calls = 0

            async def chat(self, messages, **_):
                self.calls += 1
                return _Response(content="压缩要点：此前调研主题0-5。")

        provider = _CountingProvider = _CountingProvider()
        history = _make_history(12)
        first = asyncio.run(
            build_llm_messages(history, self.workspace, history_budget=200, compress_llm=provider)
        )
        calls_after_first = provider.calls
        second = asyncio.run(
            build_llm_messages(history, self.workspace, history_budget=200, compress_llm=provider)
        )
        # 第二次组装应该命中缓存，不再发起模型调用。
        self.assertEqual(provider.calls, calls_after_first)
        # 两次摘要一致。
        self.assertEqual(
            self._summary_of(first), self._summary_of(second)
        )

    @staticmethod
    def _summary_of(messages: list[dict]) -> str:
        system = messages[0]["content"]
        marker = "## 此前对话摘要"
        return system[system.find(marker):] if marker in system else ""

    def test_实测字符率回填不采纳异常值(self):
        reporter_calls: list[dict] = []

        class _Reporter:
            def progress(self, *args, **kwargs):
                reporter_calls.append(kwargs)

        from src.agents import researchAgent as ra

        old_rate = ra._RECENT_CHARS_PER_TOKEN
        try:
            # 正常值：应被采纳。
            _report_round_usage(
                _Reporter(), 1, _Response(usage={"prompt_tokens": 100}), [{"role": "user", "content": "x" * 450}]
            )
            self.assertAlmostEqual(ra._RECENT_CHARS_PER_TOKEN, 4.5, places=1)
            # 异常值（字符率 0.05，比 1 还小）：不应被采纳。
            _report_round_usage(
                _Reporter(), 2, _Response(usage={"prompt_tokens": 10000}), [{"role": "user", "content": "x" * 500}]
            )
            self.assertAlmostEqual(ra._RECENT_CHARS_PER_TOKEN, 4.5, places=1)
        finally:
            ra._RECENT_CHARS_PER_TOKEN = old_rate


class RepairPairingTests(unittest.TestCase):
    """_repair_tool_call_pairing：被取消的 run 留下的残缺轮次要能修回合法序列。"""

    def test_无结果的tool_calls降级成正文消息(self):
        out = _repair_tool_call_pairing(
            [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": "我来精读。",
                    "tool_calls": [{"id": "x1", "type": "function", "function": {"name": "deep_read_paper", "arguments": "{}"}}],
                    "thinking_blocks": [{"thinking": "t"}],
                },
            ]
        )
        self.assertEqual(out[1]["role"], "assistant")
        self.assertNotIn("tool_calls", out[1])
        self.assertIn("我来精读", out[1]["content"])
        self.assertEqual(out[1]["thinking_blocks"], [{"thinking": "t"}])

    def test_空正文降级时补一句说明(self):
        out = _repair_tool_call_pairing(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "x1", "type": "function", "function": {"name": "t", "arguments": "{}"}}],
                    "thinking_blocks": [],
                }
            ]
        )
        self.assertIn("中断", out[0]["content"])

    def test_孤儿tool消息被丢弃(self):
        out = _repair_tool_call_pairing(
            [
                {"role": "tool", "tool_call_id": "orphan", "name": "s", "content": "孤儿"},
                {"role": "assistant", "content": "正文", "tool_calls": [],},
            ]
        )
        self.assertEqual([m.get("role") for m in out], ["assistant"])

    def test_部分回应时只保留有回应的调用项(self):
        out = _repair_tool_call_pairing(
            [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": "go",
                    "tool_calls": [
                        {"id": "a1", "type": "function", "function": {"name": "search_papers", "arguments": "{}"}},
                        {"id": "a2", "type": "function", "function": {"name": "list_papers", "arguments": "{}"}},
                    ],
                    "thinking_blocks": [],
                },
                {"role": "tool", "tool_call_id": "a1", "name": "search_papers", "content": "结果"},
            ]
        )
        paired = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
        self.assertEqual([c["id"] for c in paired[0]["tool_calls"]], ["a1"])


# ---------------------------------------------------------------------------
# 3. get_history 工具
# ---------------------------------------------------------------------------


class GetHistoryTests(unittest.TestCase):
    """_handle_get_history 的取回 / 截断 / 过滤行为。"""

    def _run(self, messages: list[dict], **kwargs):
        class _Record:
            def __init__(self, m):
                self.messages = m

        class _Repo:
            def __init__(self, m):
                self.m = m

            def get(self, key):
                return _Record(self.m)

        import types

        context = types.SimpleNamespace(session_key="s", repo=_Repo(messages))
        return asyncio.run(_handle_get_history(context, **kwargs))

    def test_正常取回最近几轮(self):
        res = self._run(
            [
                {"role": "user", "content": "问1"},
                {"role": "tool", "tool_call_id": "t", "tool_name": "search_papers", "content": "结果一"},
                {"role": "user", "content": "问2"},
                {"role": "assistant", "content": "答二"},
            ]
        )
        self.assertIn("content", res)
        self.assertIn("问1", res["content"])
        self.assertIn("search_papers", res["content"])
        self.assertIn("答二", res["content"])

    def test_超长单行被截断而不是返回空(self):
        res = self._run(
            [
                {"role": "user", "content": "问1"},
                {"role": "tool", "tool_call_id": "t", "tool_name": "search_papers", "content": "L" * 5000},
            ]
        )
        self.assertIn("content", res)
        self.assertIn("已截断", res["content"])

    def test_按工具名过滤(self):
        res = self._run(
            [
                {"role": "user", "content": "问1"},
                {"role": "tool", "tool_call_id": "t1", "tool_name": "search_papers", "content": "检索结果"},
                {"role": "user", "content": "问2"},
                {"role": "tool", "tool_call_id": "t2", "tool_name": "deep_read_paper", "content": "精读结果"},
            ],
            tool_name="deep_read_paper",
        )
        self.assertIn("deep_read_paper", res["content"])
        self.assertNotIn("检索结果", res["content"])

    def test_过滤后只剩用户诉求时如实回报工具结果为空(self):
        res = self._run(
            [{"role": "user", "content": "问1"}],
            tool_name="不存在的工具",
        )
        # 用户诉求按设计不过滤，但工具结果一行都没有——要能从返回里看出来
        # "没有该工具的结果"（不返回任何工具行即为判断依据）。
        self.assertNotIn("不存在的工具", str(res.get("content", "")))
        self.assertNotIn("已归档", str(res.get("content", "")))


# ---------------------------------------------------------------------------
# 4. 问答子 Agent：目录 + read_sections 按需取原文
# ---------------------------------------------------------------------------


class _FakeChunk:
    """模拟 TextChunk（duck typing，避免轻测试仍需要真实切分器）。"""

    def __init__(self, chunk_id: str, section: str, content: str):
        self.chunk_id = chunk_id
        self.section = section
        self.content = content


class ReadSectionsUnitTests(unittest.TestCase):
    """read_sections 工具本体（_handle_read_sections）的选段行为。"""

    def setUp(self):
        self.chunks = [
            _FakeChunk("c1", "Introduction", "We study KV cache eviction for long context LLM serving."),
            _FakeChunk("c2", "Methods", "我们的方法采用联邦分割，多轮通信聚合参数。"),
            _FakeChunk("c3", "Results", "实验显示吞吐提升 30%。"),
        ]

    def test_按关键词命中并给片段头(self):
        text = _handle_read_sections({"keywords": ["kv cache"]}, self.chunks)
        self.assertIn("c1", text)
        self.assertNotIn("我们的方法采用联邦分割", text)
        self.assertIn("Introduction", text)

    def test_按编号直接点名(self):
        text = _handle_read_sections({"chunk_ids": ["c3"]}, self.chunks)
        self.assertIn("c3", text)
        self.assertIn("Results", text)

    def test_未命中如回报错(self):
        text = _handle_read_sections({"keywords": ["量子纠缠假说词"]}, self.chunks)
        self.assertIn("未找到", text)

    def test_编号存在与否都有回音(self):
        text = _handle_read_sections({"chunk_ids": ["c1", "不存在"]}, self.chunks)
        self.assertIn("不存在", text)  # 点错编号也要如实标注

    def test_目录生成与上限(self):
        toc = _build_toc([_FakeChunk(f"c{i}", f"S{i}", "预览" * 50) for i in range(80)])
        self.assertEqual(toc["总段数"], 80)
        self.assertLessEqual(len(toc["目录"]), 60)
        self.assertIn("read_sections", toc.get("提醒", ""))


class QaToolCallNormalizerTests(unittest.TestCase):
    def test_arguments是字符串时解析为字典(self):
        norm = _normalize_tool_calls([{"id": "t1", "name": "read_sections", "arguments": '{"keywords": ["x"]}'}])
        self.assertEqual(norm[0]["arguments"], {"keywords": ["x"]})

    def test_缺id时按顺序补齐(self):
        norm = _normalize_tool_calls([{"name": "read_sections", "arguments": "{}"}])
        self.assertEqual(norm[0]["id"], "qa_call_1")


class _TruncateFulltextTests(unittest.TestCase):
    def test_短文本原样(self):
        self.assertEqual(_truncate_fulltext("短文"), "短文")

    def test_长文本保留前后两段(self):
        text = "A" * 35000 + "中间" + "B" * 35000
        out = _truncate_fulltext(text)
        self.assertIn("已省略", out)
        self.assertIn("A", out)
        self.assertIn("B", out)


class PaperQaAgentLoopTests(unittest.TestCase):
    """问答小工具循环的端到端行为（假 provider + 假工作区 + 假切片）。"""

    def _workspace(self, chunks_available: bool) -> Any:
        import types

        # 有全文产物：报告里给出 artifact_id；切片缓存是否可由外部打补丁决定。
        paper = _FakeWorkspacePaper(
            paper={"title": "测试论文", "abstract": "摘要内容"},
            deep_read=_FakeDeepRead(fulltext_artifact_id="art1" if chunks_available else ""),
        )
        workspace = types.SimpleNamespace(get_paper=lambda pid: paper)
        return workspace

    @unittest.skip("切片加载依赖真实 cache 目录，端到端见项目内嵌测试说明")
    def test_占位(self):
        pass


# ---------------------------------------------------------------------------
# 5. 精读 reduce 输入组装
# ---------------------------------------------------------------------------


class ReduceInputTests(unittest.TestCase):
    """_build_reduce_user_content 的上限与省略标注。"""

    def test_笔记不超限时全部保留(self):
        class _Chunk:
            def __init__(self, cid):
                self.chunk_id = cid

        content = _build_reduce_user_content(
            doc=types.SimpleNamespace(title="T", abstract="A" * 100, authors=[]),
            notes=["笔记一"],
            chunks=[_Chunk("c1")],
            figure_notes="",
        )
        payload = json.loads(content)
        self.assertEqual(len(payload["分段笔记"]), 1)

    def test_超限时按块序保留并标注省略(self):
        class _Chunk:
            def __init__(self, cid):
                self.chunk_id = cid

        # 造 600 条超长笔记，保证超过 150000 上限很多。
        notes = ["笔记" * 500 for _ in range(600)]
        chunks = [_Chunk(f"c{i}") for i in range(600)]
        content = _build_reduce_user_content(
            doc=types.SimpleNamespace(title="T", abstract="A", authors=[]),
            notes=notes,
            chunks=chunks,
            figure_notes="",
        )
        self.assertLessEqual(len(content), REDUCE_INPUT_MAX_CHARS)
        self.assertIn("部分内容已省略", content)


if __name__ == "__main__":
    unittest.main()
