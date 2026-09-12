"""技能文档加载器。

中文说明：
这里的「技能」指的是放在 src/agents/skills/ 下的 markdown 文档。它们不是代码，
而是给模型看的方法论说明：综述该按什么逻辑组织章节、正文该怎么写、精读一段
正文时该先保什么丢什么。

为什么要把这些文字放在单独的 .md 文件里，而不是像以前那样直接写进 Prompts.py：
提示词里其实混着两类东西——「必须输出什么形状」（字段名、纯文本还是 JSON、
防注入边界）和「方法上怎么做」（看什么、按什么标准判断）。前者是代码契约，
改一个字都要跟着改解析代码；后者是纯方法论，改措辞不该动 Python。把后者独立
成文档，两种情况就分开了。

用法只有一句话：skill_section("literature-review", "章节组织结构")
意思是「取出 literature-review.md 里那个叫『章节组织结构』的小节」。
"""

from __future__ import annotations

from pathlib import Path

# 技能文档所在目录。
# 中文说明：用 __file__ 定位，而不是写 "src/agents/skills" 这种相对路径。
# 相对路径依赖「启动程序时的工作目录」，从别处启动就会找不到；__file__ 永远
# 指向这个文件自己所在的位置。
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# 二级标题的行首标记。文档里所有 "## " 开头的行都被当成一个小节的开始。
_SECTION_PREFIX = "## "


def skill_section(document: str, heading: str) -> str:
    """从技能文档里取出一个小节的正文。

    Args:
        document: 文档名，不带 .md 后缀，例如 "literature-review"。
        heading: 二级标题的文字，例如 "章节组织结构"。

    Returns:
        该小节标题下方的内容（已去掉首尾空白）。

    中文说明：找不到文档、或文档里没有这个小节，都直接抛异常，不返回空字符串。
    这里刻意不「宽容」——技能没加载成功，提示词就会悄悄少一整段规则，而模型
    照样能编出看起来像模像样的结果，跑完一整轮也未必有人发现问题。让程序在
    启动时就炸掉，比事后排查便宜得多。
    （同样的思路本项目已有先例：base.py 里的 AgentSpec 就会在导入期直接拦下
    拼错的模型档位名。）
    """

    path = SKILLS_DIR / f"{document}.md"
    if not path.is_file():
        raise FileNotFoundError(f"找不到技能文档：{path}")

    sections = _split_sections(path.read_text(encoding="utf-8"))
    if heading not in sections:
        available = "、".join(sections) if sections else "（文档里没有任何二级标题）"
        raise KeyError(
            f"技能文档 {document}.md 里没有「{heading}」小节。现有小节：{available}"
        )
    return sections[heading]


def _split_sections(text: str) -> dict[str, str]:
    """把文档按二级标题切成若干段，返回 {标题文字: 正文}。

    中文说明：只认二级标题（行首是 "## "）。文档开头到第一个二级标题之间的
    内容（通常是一句「这份文档是干什么的」）会被丢掉，因为它不属于任何一个小节，
    也不会被注入到提示词里。
    """

    sections: dict[str, str] = {}
    current_heading: str | None = None
    buffer: list[str] = []

    for line in text.splitlines():
        if line.startswith(_SECTION_PREFIX):
            # 遇到新的二级标题：先把上一节的正文收好，再开始收集新的一节。
            if current_heading is not None:
                sections[current_heading] = "\n".join(buffer).strip()
            current_heading = line[len(_SECTION_PREFIX):].strip()
            buffer = []
        elif current_heading is not None:
            buffer.append(line)

    # 文件末尾那一节没有后续标题来触发收尾，这里补一次。
    if current_heading is not None:
        sections[current_heading] = "\n".join(buffer).strip()
    return sections
