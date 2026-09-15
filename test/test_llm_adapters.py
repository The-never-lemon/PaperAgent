import unittest

from src.llm import ModelConfig, StreamCallbacks, make_provider


class _Create:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response):
        create = _Create(response)
        self.calls = create.calls
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {"create": create.create})()


class FakeAnthropicClient:
    def __init__(self, response):
        create = _Create(response)
        self.calls = create.calls
        self.messages = type("Messages", (), {"create": create.create})()


class LLMAdaptersTest(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compat_uses_sdk_chat_completion_request(self):
        client = FakeOpenAIClient({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        config = ModelConfig.from_dict(
            {
                "providers": {"openai": {"api_key": "k"}},
                "agents": {"default_agent": {"model_name": "gpt-5-mini", "provider": "openai", "max_tokens": 100, "temperature": 0.7}},
            }
        )

        snapshot = make_provider(config, client=client)
        response = await snapshot.provider.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.content, "ok")
        kwargs = client.calls[0]
        self.assertEqual(kwargs["max_completion_tokens"], 100)
        self.assertNotIn("temperature", kwargs)

    async def test_anthropic_converts_tools_and_system_message(self):
        client = FakeAnthropicClient(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1},
            }
        )
        config = ModelConfig.from_dict(
            {
                "providers": {"anthropic": {"api_key": "k"}},
                "agents": {"default_agent": {"model_name": "claude-3-5-sonnet-latest", "provider": "anthropic", "max_tokens": 50}},
            }
        )

        snapshot = make_provider(config, client=client)
        response = await snapshot.provider.chat(
            [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}],
        )

        self.assertEqual(response.content, "ok")
        kwargs = client.calls[0]
        self.assertEqual(kwargs["system"], "be brief")
        self.assertEqual(kwargs["tools"][0]["input_schema"], {"type": "object"})

    def test_anthropic_compat_accepts_custom_base(self):
        config = ModelConfig.from_dict(
            {
                "providers": {"anthropic_compat": {"api_key": "k", "apiBase": "https://proxy.example/v1"}},
                "agents": {"default_agent": {"model_name": "anthropic_compat/claude-test", "provider": "anthropic_compat"}},
            }
        )

        snapshot = make_provider(config, client=FakeAnthropicClient({"content": []}))

        self.assertEqual(snapshot.provider.api_base, "https://proxy.example/v1")

    def test_stream_callback_contract_is_stable(self):
        seen = []
        callbacks = StreamCallbacks(on_content_delta=seen.append)
        callbacks.on_content_delta("x")
        self.assertEqual(seen, ["x"])

    async def test_openai_stream_forwards_content_and_tool_name_deltas(self):
        chunks = [
            _openai_stream_chunk(content="你好"),
            _openai_stream_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "search_papers", "arguments": ""},
                    }
                ]
            ),
            _openai_stream_chunk(
                tool_calls=[{"index": 0, "function": {"arguments": "{\"query\": \"llm\"}"}}],
                finish_reason="tool_calls",
            ),
        ]
        client = FakeOpenAIClient(chunks)
        config = ModelConfig.from_dict(
            {
                "providers": {"openai": {"api_key": "k"}},
                "agents": {"default_agent": {"model_name": "gpt-4o-mini", "provider": "openai"}},
            }
        )
        texts: list[str] = []
        tool_deltas: list[dict] = []
        snapshot = make_provider(config, client=client)
        response = await snapshot.provider.chat_stream(
            [{"role": "user", "content": "hi"}],
            StreamCallbacks(on_content_delta=texts.append, on_tool_call_delta=tool_deltas.append),
        )

        self.assertEqual(texts, ["你好"])
        self.assertEqual(tool_deltas[0]["function"]["name"], "search_papers")
        self.assertEqual(response.tool_calls[0].name, "search_papers")
        self.assertEqual(response.tool_calls[0].arguments["query"], "llm")

    async def test_anthropic_stream_emits_tool_name_when_block_starts(self):
        events = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "list_papers"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        ]
        client = FakeAnthropicClient(events)
        config = ModelConfig.from_dict(
            {
                "providers": {"anthropic": {"api_key": "k"}},
                "agents": {"default_agent": {"model_name": "claude-3-5-sonnet-latest", "provider": "anthropic"}},
            }
        )
        tool_deltas: list[dict] = []
        snapshot = make_provider(config, client=client)
        response = await snapshot.provider.chat_stream(
            [{"role": "user", "content": "hi"}],
            StreamCallbacks(on_tool_call_delta=tool_deltas.append),
        )

        self.assertEqual(tool_deltas[0]["name"], "list_papers")
        self.assertEqual(tool_deltas[0]["index"], 0)
        self.assertEqual(response.tool_calls[0].name, "list_papers")


class _OpenAIStreamChunk:
    def __init__(self, *, content="", reasoning_content="", tool_calls=None, finish_reason=None):
        delta = type("Delta", (), {})()
        delta.content = content
        delta.reasoning_content = reasoning_content
        delta.tool_calls = tool_calls or []
        choice = type("Choice", (), {})()
        choice.delta = delta
        choice.finish_reason = finish_reason
        self.choices = [choice]

    def model_dump(self):
        return {}


def _openai_stream_chunk(**kwargs):
    return _OpenAIStreamChunk(**kwargs)


if __name__ == "__main__":
    unittest.main()
