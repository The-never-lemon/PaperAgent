"""运行期基础设施包。

中文说明：
旧的 LangGraph 固定流水线（检索→阅读→分析→大纲→写作→组装）已随对话式
调研改造整体删除。这个包现在只保留新链路仍在使用的两块运行期组件：
- runtime.py：运行上下文、用户取消控制、节点事件上报器与阶段显示映射；
- runtime_resources.py：单次 run 内共享的并发信号量与 HTTP 客户端资源。
需要它们时请直接 import 具体模块，例如 from src.graph.runtime import ...。
"""
