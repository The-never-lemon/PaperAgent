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

    中文说明：模型给的"JSON"经常有几处小毛病，这里按"先便宜后费劲"的顺序
    一步步补救，任何一步成功就立刻返回：

    1. 原样解析（大部分情况这一步就够了）；
    2. 前后夹杂解释文字时，截取第一个 { 到最后一个 } 之间的内容再试；
    3. 字符串里有没转义的换行/制表符时（模型写多段中文论述最容易犯），
       把字符串内部的这些字符转义掉再试。

    补救只做"修复"，不做"猜测"——比如不会去猜缺失的字段名或补全被截断的内容，
    那些改错了反而更危险，留给上层带错误信息重试更诚实。

    Returns:
        (解析结果, 错误说明)。成功时错误说明为空字符串；失败时解析结果为 None。
    """

    raw = str(text or "").strip()
    if not raw:
        return None, "模型输出为空"

    candidate = _strip_code_fence(raw)

    # 第一步：原样解析。
    # 注意这里的判断顺序很重要：只要整段文本本身就是一个合法 JSON，无论它是对象
    # 还是别的形状（数组、字符串、数字），都到此为止、不再往下剥壳。否则像
    # [{"a":1}] 这种"最外层是数组"的输入，会被下一步把方括号剥掉、摇身变成合法对象，
    # 等于悄悄接受了本该拒绝的形状。
    payload, error, parsed, is_valid_json = _loads(candidate)
    if parsed:
        return payload, ""
    if is_valid_json:
        return None, error

    # 第二步：整段不是合法 JSON，说明前后可能夹着解释文字，截取最外层大括号再试。
    sliced = _slice_outermost_object(candidate)
    if sliced is not None:
        payload, _, parsed, _ = _loads(sliced)
        if parsed:
            return payload, ""
        # 第三步：在剥掉外层文字的基础上，把字符串内部没转义的控制字符修好再试。
        repaired = _escape_control_chars_in_strings(sliced)
        if repaired != sliced:
            payload, _, parsed, _ = _loads(repaired)
            if parsed:
                return payload, ""

    return None, f"JSON 语法错误：{error}"


def _loads(text: str) -> tuple[JsonObject | None, str, bool, bool]:
    """解析一段文本，把三种结果分开报出来。

    中文说明：返回四个值——解析结果、错误说明、是否拿到了能用的字典、
    这段文本是否本身就是一个合法 JSON。

    为什么要把最后一项单独拎出来：解析失败后我们会尝试"剥掉外层解释文字"再试一次，
    但这个补救只应该用在"整段文本根本不是合法 JSON"的情况下。如果整段本身能解析、
    只是形状不对（比如最外层是数组），那就该如实报"形状不对"，而不能靠剥壳把它
    强行变成一个对象——那是悄悄放宽了标准，让本该失败的结果蒙混过关。
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, str(exc), False, False
    if isinstance(payload, dict):
        return payload, "", True, True
    return None, f"模型输出的是 {type(payload).__name__}，不是 JSON 对象", False, True


def _slice_outermost_object(text: str) -> str | None:
    """截取第一个 { 到最后一个 } 之间的内容；没有成对的大括号就返回 None。"""

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return None


def _escape_control_chars_in_strings(text: str) -> str:
    """把 JSON 字符串内部没转义的换行、制表符、回车转义掉。

    中文说明：这一步专门修一个很常见、又很致命的毛病。

    模型写多段中文论述时，经常直接在 JSON 的字符串里敲回车换行，写出这样的东西：

        {"研究现状": "第一段。[P1]
        第二段。[P2]"}

    这在 JSON 里是非法的（字符串里不允许出现真正的换行符），标准解析器会直接
    报错。但这明显是"转义忘了写"，不是"内容有问题"——把它替换成 \\n 就能还原
    模型本来想表达的意思，一个字符都不用丢。

    关键在于**只改字符串内部**。JSON 结构本身的换行（对象和字段之间的缩进换行）
    是合法的，一动就会把整个文档弄坏。所以要一路跟踪"现在是不是在双引号里面"，
    并且正确跳过 \\" 这种已转义的引号，不能把它误当成字符串的结尾。
    """

    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            # 上一个字符是反斜杠，这一个字符已经被转义了，原样保留。
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if char == '"':
            # 只有不在转义状态下遇到的引号，才是字符串的开始或结束。
            in_string = not in_string
            out.append(char)
            continue
        if in_string and char in "\n\r\t":
            # 字符串内部的这些字符非法，换成对应的转义写法。
            out.append({"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
            continue
        out.append(char)
    return "".join(out)


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
