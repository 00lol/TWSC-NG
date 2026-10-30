# -*- coding:utf-8 -*-
"""路由包

- ``get_base_url``：统一计算路由前缀（value.config["server"]["base_url"]，默认空串）；
- 导出路由类：Index（引导）、Auth（身份验证）、Admin（后台认证）。

约定：各路由类在自己的 ``__init__`` 里用 ``f"{get_base_url(value)}/xxx"`` 组装 prefix，
Routing 只负责实例化与 include_router，避免前缀重复拼接。
"""
from typing import Any


def get_base_url(value: Any) -> str:
    """读取并规范化 base_url 前缀（默认 ""）"""
    try:
        config = getattr(value, "config", None)
        if not isinstance(config, dict):
            return ""
        server = config.get("server")
        if not isinstance(server, dict):
            server = config.get("Sever")  # 兼容旧 TWSC 配置结构
        if not isinstance(server, dict):
            return ""
        base = server.get("base_url") or server.get("BaseUrl") or ""
        base = str(base).strip().rstrip("/")
        if base and not base.startswith("/"):
            base = "/" + base
        return base
    except Exception:
        return ""


from .index import Index  # noqa: E402
from .auth import Auth  # noqa: E402
from .admin import Admin  # noqa: E402

__all__ = ["Index", "Auth", "Admin", "get_base_url"]
