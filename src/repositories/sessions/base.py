from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from src.models.sessions import SessionRecord


JsonObject = dict[str, Any]


class SessionRepository(ABC):
    """会话仓储抽象接口。"""

    @abstractmethod
    def create(self, title: str = "New chat", workspace_scope: JsonObject | None = None) -> SessionRecord:
        """创建新的会话记录。"""

    @abstractmethod
    def get(self, key: str) -> SessionRecord:
        """根据会话键获取完整会话记录。"""

    @abstractmethod
    def list(self) -> list[JsonObject]:
        """返回会话摘要列表。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除指定会话。"""

    @abstractmethod
    def append_message(self, key: str, role: str, content: str, **extra: Any) -> JsonObject:
        """向指定会话追加一条消息。"""

    @abstractmethod
    def append_event(
        self,
        key: str,
        event_type: str,
        content: str = "",
        metadata: JsonObject | None = None,
        created_at: str | None = None,
    ) -> JsonObject:
        """向指定会话追加一条结构化事件。"""

    @abstractmethod
    def write_artifact(
        self,
        key: str,
        artifact_type: str,
        name: str,
        content: str | bytes,
        *,
        relative_path: str,
        metadata: JsonObject | None = None,
        created_at: str | None = None,
        encoding: str = "utf-8",
    ) -> JsonObject:
        """向指定会话写入产物文件，并在仓储中登记元数据。"""

    @abstractmethod
    def read_artifact_path(self, key: str, artifact_id: str) -> Path | None:
        """根据产物编号返回安全的产物文件路径。

        中文说明：
        校验产物记录中的文件路径是否仍然位于该会话目录内，返回安全的绝对路径；
        记录不存在、文件越界或文件已被删除时返回 None，避免下载接口读到会话外的文件。
        """

    @abstractmethod
    def set_workspace_scope(self, key: str, workspace_scope: JsonObject | None) -> JsonObject:
        """更新会话的工作区范围信息。"""

    @abstractmethod
    def set_run_started_at(self, key: str, started_at: str | None) -> JsonObject:
        """更新当前回合的运行开始时间。"""

    @abstractmethod
    def set_status(self, key: str, status: str) -> JsonObject:
        """更新会话状态。"""

    @abstractmethod
    def set_active_run_id(self, key: str, run_id: str | None) -> JsonObject:
        """更新会话 metadata 里的 active_run_id 字段。

        中文说明：
        前端刷新页面时需要知道「当前这个会话有没有还在跑的 run」以及「去哪个地址接回实时流」。
        这个方法把 run_id 写进 session 的 metadata 列，start_run 时写入、三个 finalizer 清空。
        """

    @abstractmethod
    def reset_stale_runs(self) -> int:
        """启动时把所有处于「正在运行」状态的会话置成「已中断」。

        中文说明：
        后端进程被强杀（kill -9、意外崩溃）时，正在跑的 run 不会正常走完三个 finalizer，
        数据库里的 status 会永远停在 'running'、run_started_at 永远不为空。
        下一次启动如果再让用户往这个会话发消息，start_run 会直接抛 409。
        这个方法在应用启动时调一次，把所有残留的「正在运行」状态全部清掉，
        让用户可以继续对话。返回被修复的会话条数。
        """

    @abstractmethod
    def purge_stream_events(self) -> int:
        """清掉历史上全部流式 token 事件，释放被它们占满的数据库空间。

        中文说明：
        改造前，每一个 reasoning_delta / delta 都会往 session_event 表里插一条记录，
        一次长调研就能产生上万条事件，让数据库膨胀到几十 MB。改造后这些流式 token
        不再落库，但历史数据里已经堆积的旧事件还在。启动时跑一次清理，把数据库
        瘦身。返回被删掉的记录条数。
        """
