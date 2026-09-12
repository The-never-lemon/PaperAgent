from __future__ import annotations

from typing import Any, Callable


JsonObject = dict[str, Any]
Transport = Callable[[str, str, JsonObject | None, int], tuple[int, JsonObject]]


class ApiError(Exception):
    """测试侧统一 API 错误模型。"""

    def __init__(self, message: str, status: int = 0, payload: JsonObject | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class FrontendApiClient:
    """测试使用的轻量前端 API 客户端。"""

    def __init__(self, transport: Transport, timeout_ms: int = 15000):
        self.transport = transport
        self.timeout_ms = timeout_ms

    def request(self, method: str, url: str, body: JsonObject | None = None) -> JsonObject:
        status, payload = self.transport(method, url, body, self.timeout_ms)
        if status < 200 or status >= 300:
            message = payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else None
            detail = payload.get("detail")
            if isinstance(detail, dict):
                message = detail.get("message") or message
            raise ApiError(message or f"request failed: {status}", status, payload)
        return payload

    def bootstrap(self) -> JsonObject:
        return self.request("GET", "/webui/bootstrap")

    def list_sessions(self) -> JsonObject:
        return self.request("GET", "/api/sessions")

    def create_session(self, title: str = "New chat", workspace_scope: JsonObject | None = None) -> JsonObject:
        return self.request("POST", "/api/sessions", {"title": title, "workspace_scope": workspace_scope})

    def fetch_thread(self, session_key: str) -> JsonObject:
        return self.request("GET", f"/api/sessions/{session_key}/webui-thread")

    # 中文注释：这里原来有一个 send_message 方法，它调用的是旧版"同步提交消息"
    # 接口 POST /api/sessions/{key}/messages。那个旧接口已经在后端整体删掉了
    # （现在发消息统一走 POST /api/sessions/{key}/runs 创建后台运行，再用 SSE
    # 实时流收结果），所以这个方法也一并删除。留着它的话，就是一个指向"已经不
    # 存在的接口"的死方法，谁调用谁报 404，纯粹误导人。删除前已用全局搜索确认：
    # 整个仓库（含前端和测试）没有任何地方调用 send_message，删掉不影响任何功能。

    def delete_session(self, session_key: str) -> JsonObject:
        return self.request("DELETE", f"/api/sessions/{session_key}")

    def fetch_settings(self) -> JsonObject:
        return self.request("GET", "/api/settings")
