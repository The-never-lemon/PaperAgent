"""一次运行要用的公共设施。

中文说明：
这里只放每次跑任务时都要用的两块东西：
- workflow.py：这次运行的上下文、用户点停止、进度怎么报给界面；
- resources.py：这一次运行里大家一起用的并发上限和网络客户端。
需要它们时请直接写 from src.runtime.workflow import ...。
"""
