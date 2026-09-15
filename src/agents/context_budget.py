"""上下文预算小工具（主 Agent 与各子 Agent 共用）。

中文注释：对话历史、问答材料、精读笔记汇总都要回答同一个问题——
「这些内容发给模型大概占多少 token？超没超过预算？」。这里放一个
不依赖第三方库的粗估实现：

- 不做精确 token 计数（中文/英文混合没有免费又准的本地 tokenizer）；
- 用「字符数 ÷ 字符率」近似，字符率默认 4.5 字符 ≈ 1 token（中文场景
  1 token ≈ 1.5~2 字符、英文场景 ≈ 4~5 字符，混合文本取一个偏保守的
  中间值），主 Agent 每轮调用后可以用真实 usage 回填修正；
- 所有调用方只做「超没超预算」的粗判断，就算是误差 ±20% 也不影响
  正确性——真正的兜底（LLM 压缩历史、按需取原文）在各自的模块里。

这一个文件只放纯函数，不 import 任何业务模块，保证谁都能安全引用。
"""

from __future__ import annotations

from typing import Any, Sequence


# 首轮没有任何实测数据时用的保守字符率（1 token ≈ 4.5 字符）。
INITIAL_CHARS_PER_TOKEN = 4.5

# 中文注释：预算占模型上下文窗口的比例。留 20% 给本轮输出、思考块和
# 供应商统计出入，日常会话基本不会触发压缩。
HISTORY_BUDGET_RATIO = 0.8


def estimate_tokens(text: str, chars_per_token: float = INITIAL_CHARS_PER_TOKEN) -> int:
    """把一段文本按「字符数 ÷ 字符率」粗估成 token 数。

    中文注释：chars_per_token 是「多少个字符算一个 token」，主 Agent
    每轮拿到真实 usage 后会回填更准的值；子 Agent 没有实测渠道，就一直
    用保守默认值。估算只用于判断超没超预算，不追求精确。
    """

    try:
        rate = float(chars_per_token)
    except (TypeError, ValueError):
        rate = INITIAL_CHARS_PER_TOKEN
    if rate <= 0:
        rate = INITIAL_CHARS_PER_TOKEN
    return max(1, int(len(text or "") / rate))


def estimate_messages_tokens(
    messages: Sequence[Any],
    chars_per_token: float = INITIAL_CHARS_PER_TOKEN,
) -> int:
    """粗估一整组消息（system/user/assistant/tool 混合）的总 token 占用。

    中文注释：每条消息取 role + content（assistant 带 tool_calls 时再把
    工具名和参数也算上）拼成文本估算。中英文混合下这个估算偏保守
    （把 JSON 结构也算进去了），宁可高估也别低估——低估会让超预算的
    请求直接被供应商拒绝，高估只是早一点触发压缩。
    """

    total_chars = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        total_chars += len(role) + 8  # 角色名和消息框架本身的固定开销
        content = str(message.get("content") or "")
        total_chars += len(content)
        # 中文注释：推理模型的 thinking 块也会原样回传给供应商（anthropic 后端
        # 会把整个块发上去），它恰恰是历史里最占空间的部分，必须计入。
        thinking = message.get("thinking_blocks")
        if isinstance(thinking, list):
            for block in thinking:
                if isinstance(block, dict):
                    total_chars += len(str(block.get("thinking") or block.get("text") or ""))
                elif isinstance(block, str):
                    total_chars += len(block)
        reasoning = str(message.get("reasoning_content") or "")
        total_chars += len(reasoning)
        # assistant 带 tool_calls 的消息：工具名和参数 JSON 也是上下文的一部分。
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                total_chars += len(str(call.get("name") or ""))
                total_chars += len(str(call.get("arguments") or ""))
    return estimate_tokens("a" * total_chars, chars_per_token)


def budget_tokens(context_window_tokens: int | None) -> int:
    """根据模型上下文窗口算出历史/材料允许占用的 token 预算。

    中文注释：窗口值没配时按 1M 计（DeepSeek 等长窗口模型的实际量级），
    再乘 HISTORY_BUDGET_RATIO 留出输出余量。
    """

    window = int(context_window_tokens) if context_window_tokens else 1048576
    return int(window * HISTORY_BUDGET_RATIO)
