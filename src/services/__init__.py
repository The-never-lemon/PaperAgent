"""应用服务包。

中文说明（阶段6清理）：SessionError 已下沉到 src/models/sessions.py 定义，
需要时请从那里导入；本包不再转发它，避免又形成一条隐藏的依赖捷径。
"""

from .settings import SettingsError

__all__ = ["SettingsError"]
