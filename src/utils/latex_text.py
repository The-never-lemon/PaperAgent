"""清洗论文元数据里的「裸 LaTeX 标记」。

中文说明：
检索回来的论文元数据经常自带 LaTeX 标记，而且**不带 $ 定界符**，例如：

    The \\textit{PIMAEX} reward, short for Peer-Incentivized Multi-Agent…
    …achieves 88.5\\% of the baseline…
    代码在 \\href{https://github.com/xxx/yyy}{https://github.com/xxx/yyy}

这些字符如果原样留着，会同时污染四个地方：前端展示（用户看到一串反斜杠）、
喂给模型的提示词、综述的参考文献、以及导出的 bibtex/csv/markdown。所以统一在
论文对象构造的时候洗干净，一处生效、处处干净。

前端有一份同样规则的实现（front/src/lib/math-text.ts 的 stripBareLatex），
负责兜住"没经过这里"的文本（旧的会话记录、模型自己写的回复、用户手打的字）。
**两份的规则必须一致，改一处就要改另一处。**

## 判据：闭集白名单，不是「反斜杠加英文单词」

只认下面三类，认不出的一律原样保留：

  1. 文字样式命令：\\textit{...} \\textbf{...} \\emph{...} 等（见 _TEXT_COMMANDS），
     剥掉外壳只留里面的字。花括号必须配平、不能跨行。
  2. 链接命令：\\href{网址}{显示文字} 留显示文字；\\url{网址} 留网址。
  3. 转义还原：\\% \\& \\_ \\# 还原成字面字符；句点后的「反斜杠+空格」还原成一个空格
     （LaTeX 里它表示句子结束后的正常间距，所以 `vs.\\ exploitation` 要变成 `vs. exploitation`）。

## 为什么必须用白名单

如果判据放宽成「反斜杠加一个英文单词」，下面这些正常文本会被改坏——而它们在本项目里
真实存在（这是 Windows 上的项目，目录树里就有 src/models）：

    C:\\Users\\Admin                       ->  \\Users 不是命令，原样保留
    正则 \\d+\\w*\\s                        ->  \\d \\w \\s 都不是命令
    \\d{2,4}                               ->  就算带花括号，也不在白名单里
    \\Users{Admin}                         ->  同样不在白名单

## 刻意不做的两件事

- **不认裸数学命令**（\\alpha、\\lambda、\\times、\\frac 这种没被 $ 包起来的）。
  实测本项目 143 篇论文的元数据里，这类写法出现 0 次——收益为零，而误伤风险
  全部来自放宽白名单。所以宁可让裸 \\alpha 保持原样（改之前本来也是原样）。
- **不还原 \\{ \\} \\$ 和 \\***。前三个还原会改坏 `C:\\Users\\{username}\\AppData`、
  `C:\\$Recycle.Bin` 这类路径；`\\*` 在前端的 Markdown 里表示斜体，还原会凭空造出斜体。
"""

from __future__ import annotations

import re

# 允许被剥壳的文字样式命令。按长度从长到短排，避免 \text 抢在 \textit 前面匹配。
_TEXT_COMMANDS = (
    "textnormal",
    "textit",
    "textbf",
    "texttt",
    "textrm",
    "textsf",
    "textsc",
    "mbox",
    "text",
    "emph",
)

# 允许还原成字面字符的转义（刻意只有这四个，不含 { } $）。
_ESCAPED_CHARACTERS = "%&_#"

# 单个命令括号里最多允许多少字（超过就认为不是一条样式命令，原样保留）。
_MAX_COMMAND_ARG_CHARS = 200

# 带定界符的公式，整段原样搬走。四条规则与前端 math-text.ts 里的完全一致。
_DELIMITED_MATH_PATTERNS = (
    re.compile(r"\$\$[\s\S]+?\$\$"),
    re.compile(r"\\\[[\s\S]+?\\\]"),
    re.compile(r"\\\([\s\S]+?\\\)"),
    re.compile(r"\$(?![\s$])(?:\\.|[^$\\])+?(?<![\s\\])\$"),
)


def clean_latex_text(text: str) -> str:
    """把一段文本里的裸 LaTeX 标记剥掉，返回清洗后的文本。"""

    # 绝大多数文本里没有反斜杠，直接返回，不做任何扫描。
    if not text or "\\" not in text:
        return text

    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]

        # 先看这里是不是一个完整的公式（$...$ 这类）。是就整段原样搬走，一个字都不动——
        # 公式里的 \alpha、\% 归渲染器解释，不能在这里被当成裸标记剥掉。
        if char in "$\\":
            math = _match_delimited_math(text, index)
            if math:
                out.append(math)
                index += len(math)
                continue

        if char != "\\":
            out.append(char)
            index += 1
            continue

        span = _match_href(text, index)
        if span:
            text_value, index = span
            out.append(text_value)
            continue

        span = _match_url(text, index)
        if span:
            text_value, index = span
            out.append(text_value)
            continue

        span = _match_text_command(text, index)
        if span:
            inner, index = span
            # 内容里可能还套着别的命令（\textbf{\textit{X}}），递归再剥一遍。
            out.append(clean_latex_text(inner))
            continue

        span = _match_escape(text, index)
        if span:
            text_value, index = span
            out.append(text_value)
            continue

        # 认不出来：这个反斜杠原样保留，继续往后看。
        out.append(char)
        index += 1

    return "".join(out)


def _match_delimited_math(text: str, index: int) -> str | None:
    """判断 index 处是不是一个以定界符包起来的公式；是就返回整段原文。"""

    for pattern in _DELIMITED_MATH_PATTERNS:
        match = pattern.match(text, index)
        if match:
            return match.group(0)
    return None


def _match_href(text: str, index: int) -> tuple[str, int] | None:
    """尝试读一个 \\href{网址}{显示文字}，返回 (显示文字, 结束位置)。"""

    if not text.startswith("\\href", index):
        return None
    target = _read_braced_group(text, _skip_spaces(text, index + len("\\href")))
    if target is None:
        return None
    label_text, label_end = target
    label = _read_braced_group(text, _skip_spaces(text, label_end))
    if label is None:
        return None
    # 有显示文字就用显示文字，没有就退回网址本身。
    return (label[0] or label_text, label[1])


def _match_url(text: str, index: int) -> tuple[str, int] | None:
    """尝试读一个 \\url{网址}，返回 (网址, 结束位置)。"""

    if not text.startswith("\\url", index):
        return None
    group = _read_braced_group(text, _skip_spaces(text, index + len("\\url")))
    if group is None:
        return None
    return group


def _match_text_command(text: str, index: int) -> tuple[str, int] | None:
    """尝试读一个白名单里的文字样式命令，返回 (括号内容, 结束位置)。

    中文说明：三个条件同时满足才算数——命令在白名单里、后面紧跟一对配平的花括号、
    括号内容不跨行且不太长。任一条不满足就返回 None，让上层原样保留这段文字。
    """

    for command in _TEXT_COMMANDS:
        token = "\\" + command
        if not text.startswith(token, index):
            continue
        # 避免把 \textitX 这种「命令名后面还粘着字母」的写法误认成 \textit。
        following = text[index + len(token): index + len(token) + 1]
        if following and following.isascii() and following.isalpha():
            return None
        group = _read_braced_group(text, _skip_spaces(text, index + len(token)))
        if group is None:
            return None
        content, end = group
        if "\n" in content or len(content) > _MAX_COMMAND_ARG_CHARS:
            return None
        return group
    return None


def _match_escape(text: str, index: int) -> tuple[str, int] | None:
    """尝试还原一个转义字符，返回 (还原后的字符, 结束位置)。"""

    following = text[index + 1: index + 2]
    if following and following in _ESCAPED_CHARACTERS:
        return (following, index + 2)
    # 句点后面的「反斜杠 + 空格」：LaTeX 用它表示句子结束后的正常间距。
    if following == " " and index > 0 and text[index - 1] == ".":
        return (" ", index + 2)
    return None


def _skip_spaces(text: str, index: int) -> int:
    """跳过若干空格，返回第一个非空格字符的位置。"""

    while index < len(text) and text[index] == " ":
        index += 1
    return index


def _read_braced_group(text: str, index: int) -> tuple[str, int] | None:
    """读一对花括号包起来的内容，返回 (内容, 结束位置)。括号不配平就返回 None。"""

    if index >= len(text) or text[index] != "{":
        return None
    depth = 0
    cursor = index
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            # 跳过被转义的下一个字符，免得把 \{ 当成一层新括号。
            cursor += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return (text[index + 1: cursor], cursor + 1)
        cursor += 1
    return None
