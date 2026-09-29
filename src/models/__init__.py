# 文件作用：导出会话等跨模块共享的数据模型。
"""共享模型包。"""

from .sessions import SessionRecord, utc_now

__all__ = ["SessionRecord", "utc_now"]
