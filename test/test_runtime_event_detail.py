"""运行事件详情展示：工具参数应是 JSON 对象本身，而不是套一层 arguments_summary 字符串。"""

import json
import unittest
from typing import Any

from src.graph.runtime import InlineWorkflowSyncPort, WorkflowNodeReporter


def _collecting_reporter() -> tuple[list[dict[str, Any]], WorkflowNodeReporter]:
    """构造一个把发出的事件收进列表的工具节点上报器。"""

    events: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> dict[str, Any]:
        events.append(event)
        return event

    port = InlineWorkflowSyncPort(emit, session_key="session-1", turn_id="turn-1")
    return events, port.for_node("tool", "工具执行")


def _step_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """取出工具步骤卡片（子事件），忽略节点根卡片。"""

    steps = [event for event in events if event.get("type") == "workflow_step"]
    if not steps:
        raise AssertionError("没有发出 workflow_step 事件")
    return steps[-1]


class RuntimeEventDetailTests(unittest.TestCase):
    def test_tool_start_detail_content_is_arguments_object(self) -> None:
        """用户展开详情时应看到参数对象本身，而不是 arguments_summary 包着的转义字符串。"""

        arguments = {
            "title": "Attention Is All You Need",
            "topic": "Transformer 架构",
        }
        events, reporter = _collecting_reporter()
        reporter.started(
            "正在执行 deep_read_paper",
            stage="deep_read_paper",
            event_key="deep_read_paper_1",
            arguments_summary=json.dumps(arguments, ensure_ascii=False),
        )
        step = _step_event(events)
        self.assertEqual(step["detail_content"], arguments)
        self.assertEqual(
            step["metadata"]["arguments_summary"],
            json.dumps(arguments, ensure_ascii=False),
        )

    def test_token_progress_does_not_replace_detail_with_token_counts(self) -> None:
        """用量已经显示在卡片右侧，不应再写进详情，否则会把工具参数冲掉。"""

        events, reporter = _collecting_reporter()
        reporter.progress(
            "精读完成",
            stage="deep_read_paper",
            event_key="deep_read_paper_1",
            input_tokens=1200,
            output_tokens=80,
        )
        step = _step_event(events)
        self.assertIsNone(step["detail_content"])
        self.assertEqual(step["input_tokens"], 1200)
        self.assertEqual(step["output_tokens"], 80)
