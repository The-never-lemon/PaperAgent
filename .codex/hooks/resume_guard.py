# -*- coding: utf-8 -*-
"""写简历前的提醒 hook（非阻塞，只提醒不拦截）。

工作方式：Claude Code 在每次调用 Write/Edit 前会运行本脚本，工具调用的信息
以 JSON 形式从标准输入传进来。我们检查要写的文件路径里有没有"简历"两个字，
有就在标准错误输出一句提醒，然后正常退出（退出码 0 表示放行）。

提醒只是打招呼，不阻止写入——真正保证简历材料与代码一致的是 /finish 流程。
"""

import json
import sys


def main():
    # Windows 下 stdin/stderr 默认用本地编码（GBK），中文路径会解错，必须按 UTF-8 处理
    for stream in (sys.stdin, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # 从标准输入读出 Claude Code 传来的工具调用信息
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # 读不出内容就什么都不做，正常放行

    # 工具入参里找文件路径（Write/Edit 的参数名是 file_path）
    tool_input = payload.get("tool_input") or {}
    file_path = str(tool_input.get("file_path", ""))

    if "简历" in file_path:
        print(
            "提醒：即将写入简历材料。请确认先整读了"
            "《简历-Paper-Agent项目经历.md》文末的「附一红线清单」和「附三投递前自检」，"
            "所有表述必须与代码对得上。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
