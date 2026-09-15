import unittest

from src.agents.researchAgent import note_streaming_tool_call


class StreamingToolPreviewTest(unittest.TestCase):
    def test_first_delta_with_name_opens_a_preview_card(self):
        preview_keys: dict[int, str] = {}
        result = note_streaming_tool_call(
            preview_keys,
            {"index": 0, "function": {"name": "search_papers", "arguments": ""}},
            round_no=1,
        )
        self.assertEqual(result, (0, "search_papers", "search_papers_r1_0"))
        self.assertEqual(preview_keys[0], "search_papers_r1_0")

    def test_later_argument_fragments_do_not_open_another_card(self):
        preview_keys: dict[int, str] = {}
        note_streaming_tool_call(
            preview_keys,
            {"index": 0, "function": {"name": "search_papers"}},
            round_no=2,
        )
        result = note_streaming_tool_call(
            preview_keys,
            {"index": 0, "function": {"arguments": "{\"query\":"}},
            round_no=2,
        )
        self.assertIsNone(result)
        self.assertEqual(len(preview_keys), 1)

    def test_anthropic_style_name_at_top_level(self):
        preview_keys: dict[int, str] = {}
        result = note_streaming_tool_call(
            preview_keys,
            {"index": 1, "name": "deep_read_paper", "id": "call_1"},
            round_no=3,
        )
        self.assertEqual(result, (1, "deep_read_paper", "deep_read_paper_r3_1"))
