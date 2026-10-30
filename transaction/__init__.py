# -*- coding:utf-8 -*-
"""请求事务层（transaction）

对外导出：
- Middleware     请求上下文 / 会话装载 / CSRF / 安全头 / 访问日志 / 异常兜底
- Security       饼干认证与会话服务（value.security）
- Password       密码哈希（pbkdf2_sha256，标准库）
- CookieSigner   HMAC-SHA256 饼干签名器
- current_user   依赖：当前登录用户（未登录 401）
- require_level  依赖工厂：按等级做权限校验（等级越负越高）
- get_client_ip  获取客户端真实 IP（仅信任可信代理的 X-Forwarded-For）
- Routing        路由聚合入口
"""
from .auth import (
    CookieSigner,
    LEVEL_ADMIN,
    LEVEL_SUPER,
    LEVEL_USER,
    Password,
    Security,
    current_user,
    get_client_ip,
    require_level,
)
from .middleware import Middleware
from .routing import Routing

__all__ = [
    "Middleware",
    "Security",
    "Password",
    "CookieSigner",
    "current_user",
    "require_level",
    "get_client_ip",
    "Routing",
]
