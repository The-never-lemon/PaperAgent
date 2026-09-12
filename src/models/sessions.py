from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


JsonObject = dict[str, Any]


# 一次 thread 接口最多带回多少条历史运行事件。
# 为什么要有上限：一次长调研会产生成千上万条过程事件（比如 reasoning_delta
# 动辄上万条），前端每次切换会话、每轮结束、每次断流重连都会重新拉一遍这个接口。
# 不设上限的话，光是传输和逐条重放就能让界面卡住。只保留最近的这些条，
# 更早的过程痕迹不影响阅读对话内容（对话正文来自消息表，不依赖过程事件）。
THREAD_MAX_EVENTS = 2000

# 会话被强制中断（比如后端进程被强杀后重启自愈）时的状态值。
# 前端需要把这个状态显示成「已中断」并允许用户继续对话。
SESSION_STATUS_INTERRUPTED = "interrupted"


def utc_now() -> str:
    """返回当前 UTC 时间的 ISO 字符串。"""

    return datetime.now(timezone.utc).isoformat()


class SessionError(Exception):
    """表示会话业务里可预期的异常（会话不存在、正在运行等）。

    中文说明（阶段6清理）：这个类原来定义在 services 层，但仓储层
    （repositories/sessions/sqlite.py）也要抛它，形成了"repositories 反向
    import services"的循环依赖——任何脚本只要先 import services 就会崩。
    现在把它下沉到最底层的 models 包：services / repositories / api 都只
    向下依赖 models，导入顺序不再有任何讲究。
    """

    def __init__(self, message: str, status: int = 400):
        """保存错误信息和对应的 HTTP 状态码。"""

        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class SessionRecord:
    """单个会话的聚合视图模型。

    中文说明：
    这个对象用于承载会话详情的统一内存表示，让服务层不需要知道底层
    是 SQLite、文件系统还是其他存储实现。只要仓储层最终返回这个模型，
    上层逻辑就能稳定消费。
    """

    key: str
    title: str = "New chat"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    status: str = "created"
    summary_text: str = ""
    messages: list[JsonObject] = field(default_factory=list)
    events: list[JsonObject] = field(default_factory=list)
    artifacts: list[JsonObject] = field(default_factory=list)
    workspace_scope: JsonObject | None = None
    run_started_at: str | None = None
    user_id: str = "local-user"
    last_message_at: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def summary(self) -> JsonObject:
        """返回适合会话列表展示的摘要信息。"""

        last_message = self.messages[-1]["content"] if self.messages else self.summary_text
        return {
            "key": self.key,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "preview": (last_message or "")[:120],
            "run_started_at": self.run_started_at,
            "workspace_scope": copy.deepcopy(self.workspace_scope),
            "status": self.status,
            "user_id": self.user_id,
            "last_message_at": self.last_message_at,
        }

    def thread(self) -> JsonObject:
        """返回适合前端线程视图使用的完整会话数据。

        中文说明：
        `active_run_id` 从会话 metadata 里取，前端用它判断「当前会话是否还在跑」、
        以及刷新后要不要自动接回实时流。当后端进程被强杀后自愈时，这个字段会被清空，
        前端因此能正确识别出「这次 run 已经不在后端内存里了」。
        """

        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "messages": copy.deepcopy(self.messages),
            "events": copy.deepcopy(self.events),
            "artifacts": copy.deepcopy(self.artifacts),
            "workspace_scope": copy.deepcopy(self.workspace_scope),
            "has_pending_tool_calls": bool(self.run_started_at),
            "run_started_at": self.run_started_at,
            "active_run_id": self.metadata.get("active_run_id"),
            "page": {"cursor": None, "has_more": False},
        }
