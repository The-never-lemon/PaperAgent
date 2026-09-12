"""模型输出 JSON 的统一解析入口（工程规范 8.3）。

模型返回的"JSON"经常不干净：外面包着 ```json 代码块、前后混着解释文字、
偶尔还有语法错误。所有需要模型产出结构化数据的 Agent（评价、精读、问答……）
都统一调用这里的 parse_llm_json，不允许各自手写解析和重试逻辑。

解析规则：
1. 先尝试提取 ```json 代码块或裸 JSON 对象；
2. 解析失败且调用方提供了 repair 回调时，把错误信息交回给模型重试一次；
3. 再失败就返回 fallback 结构（附带 parse_error 说明），绝不向上抛异常。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from src.utils import get_logger


JsonObject = dict[str, Any]
logger = get_logger(__name__)


async def parse_llm_json(
    text: str,
    fallback: JsonObject | None = None,
    *,
    repair: Callable[[str], Awaitable[str]] | None = None,
) -> JsonObject:
    """把模型输出解析成 JSON 字典，失败时按约定兜底，永不抛异常。

    Args:
        text: 模型返回的原始文本。
        fallback: 解析彻底失败时返回的兜底结构；返回前会在它基础上
            附加一个 parse_error 字段说明失败原因。
        repair: 可选的"补救回调"。解析失败时会把错误信息传给它，
            由调用方（持有模型 provider 的 Agent）带着错误信息再问一次模型，
            返回模型的新输出文本。这是唯一一次重试，业务层不要再套重试循环。

    Returns:
        解析成功时返回模型输出的字典；失败时返回 fallback + parse_error。
        调用方通过是否存在 parse_error 字段判断这次解析是否成功。
    """

    payload = dict(fallback) if isinstance(fallback, dict) else {}

    parsed, error = _try_parse_json_object(text)
    if parsed is not None:
        return parsed

    if repair is not None:
        # 带着错误信息让模型重新生成一次（工程规范 8.3 的"失败带错误信息重试 1 次"）。
        try:
            repaired_text = await repair(error)
        except Exception as exc:
            repaired_text = ""
            error = f"{error}；补救调用也失败了：{exc}"
        parsed, retry_error = _try_parse_json_object(repaired_text)
        if parsed is not None:
            logger.info("模型 JSON 输出经补救后解析成功", extra={"first_error": error[:200]})
            return parsed
        error = f"{error}；补救输出仍无法解析：{retry_error}"

    logger.warning(
        "模型 JSON 输出解析失败，返回兜底结构",
        extra={"error": error[:300], "text_length": len(text or "")},
    )
    payload["parse_error"] = error
    return payload


def _try_parse_json_object(text: Any) -> tuple[JsonObject | None, str]:
    """尝试从一段文本里提取并解析出 JSON 对象。

    Returns:
        (解析结果, 错误说明)。成功时错误说明为空字符串；失败时解析结果为 None。
    """

    raw = str(text or "").strip()
    if not raw:
        return None, "模型输出为空"

    candidate = _strip_code_fence(raw)
    try:
        payload = json.loads(candidate)
        if isinstance(payload, dict):
            return payload, ""
        return None, f"模型输出的是 {type(payload).__name__}，不是 JSON 对象"
    except json.JSONDecodeError as first_error:
        # 整段解析失败后，再试试截取第一个 { 到最后一个 } 之间的内容，
        # 兼容模型在 JSON 前后夹杂解释文字的情况。
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            try:
                payload = json.loads(candidate[start : end + 1])
                if isinstance(payload, dict):
                    return payload, ""
            except json.JSONDecodeError:
                pass
        return None, f"JSON 语法错误：{first_error}"


def _strip_code_fence(text: str) -> str:
    """去掉模型输出常见的 ```json ... ``` 代码块外壳。"""

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # 去掉第一行 ```json（或 ```）和最后一行 ```。
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
