# -*- coding:utf-8 -*-
"""身份验证路由：/auth/*

- POST /auth/login      登录（JSON：username/password），下发会话饼干与 CSRF 饼干
- POST /auth/logout     登出（幂等：未登录也返回成功）
- GET  /auth/me         当前用户
- POST /auth/register   注册（需 config server.register=True）
- POST /auth/password   修改密码（需登录）
- POST /auth/email/code 发送邮箱验证码（需 config server.email=True）

说明：本项目未安装 python-multipart，所有请求体一律 JSON（pydantic BaseModel）。
"""
import asyncio
import inspect
import re
import secrets
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..auth import Password, current_user, get_client_ip, normalize_user, LEVEL_USER
from . import get_base_url

# 邮箱验证码缓存键与有效期
EMAIL_CODE_KEY = "twsc:email:code:{email}:{scene}"
EMAIL_COOLDOWN_KEY = "twsc:email:cooldown:{email}:{scene}"
EMAIL_CODE_TTL = 300      # 验证码 5 分钟有效
EMAIL_COOLDOWN_TTL = 60   # 同一邮箱 60 秒内不重复发送

# 用户名 / 密码长度约束
USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN, PASSWORD_MAX = 6, 64

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_SCENES = {"register", "reset"}


# --------------------------------------------------------------------------- #
# 请求模型（JSON）
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str
    password: str
    email: Optional[str] = None
    code: Optional[str] = None


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


class EmailCodeBody(BaseModel):
    email: str
    scene: str = "register"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _filter_kwargs(method: Any, kwargs: dict) -> dict:
    """按被调方法的签名过滤可选关键字参数（跨包契约未完全对齐时的兜底）"""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return kwargs
    params = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _unpack_result(result: Any) -> tuple:
    """统一数据库操作的返回形态：(bool, str) / bool / dict / (bool, str, extra)"""
    if isinstance(result, tuple) and len(result) >= 2:
        extra = result[2] if len(result) >= 3 else None
        return bool(result[0]), str(result[1]), extra
    if isinstance(result, bool):
        return result, ("操作成功" if result else "操作失败"), None
    if isinstance(result, dict):
        return True, "操作成功", result
    if result is None:
        return False, "操作失败", None
    return bool(result), "操作成功", None


class Auth:
    """身份验证路由类（prefix = {base_url}/auth）"""

    def __init__(self, value):
        if value is None:
            raise ValueError("Auth 路由初始化失败：value 为空")
        self.value = value
        self.logger = getattr(value, "logger", None)
        if self.logger is None:
            raise ValueError("Auth 路由初始化失败：value.logger 未初始化")
        self.base_url = get_base_url(value)
        self.router = APIRouter(prefix=f"{self.base_url}/auth", tags=["身份验证"])
        self._route()

    # ------------------------------------------------------------------ #
    # value 依赖（全部鸭子类型，不顶层 import cache / databases）
    # ------------------------------------------------------------------ #
    @property
    def database(self):
        return getattr(self.value, "database", None)

    @property
    def cache(self):
        return getattr(self.value, "cache", None)

    @property
    def security(self):
        return getattr(self.value, "security", None)

    def _server_config(self) -> dict:
        config = getattr(self.value, "config", None)
        if isinstance(config, dict):
            server = config.get("server")
            if isinstance(server, dict):
                return server
            legacy = config.get("Sever")  # 兼容旧 TWSC 结构
            if isinstance(legacy, dict):
                return legacy
        return {}

    def _require_db(self):
        database = self.database
        if database is None or not getattr(database, "connected", False):
            raise HTTPException(status_code=503, detail="数据库不可用")
        return database

    def _require_cache(self):
        cache = self.cache
        if cache is None:
            raise HTTPException(status_code=503, detail="缓存不可用")
        return cache

    def _require_security(self):
        security = self.security
        if security is None or getattr(security, "session_store", None) is None:
            raise HTTPException(status_code=503, detail="认证服务不可用")
        return security

    async def _call_user(self, name: str, *args, **kwargs):
        """调用 database.user 下的方法（方法缺失时给出明确契约错误）"""
        database = self._require_db()
        module = getattr(database, "user", None)
        if module is None:
            self.logger.error("数据库用户模块未就绪（database.user 为空）")
            raise HTTPException(status_code=501, detail="数据库用户模块尚未实现")
        method = getattr(module, name, None)
        if method is None:
            self.logger.error(f"数据库用户模块缺少方法 {name}（跨包契约未对齐）")
            raise HTTPException(status_code=501, detail=f"数据库用户模块尚未实现 {name}")
        kwargs = _filter_kwargs(method, kwargs)
        try:
            return await method(*args, **kwargs)
        except TypeError as e:
            self.logger.error(f"调用 database.user.{name} 参数不匹配（跨包契约未对齐）: {e}")
            raise HTTPException(status_code=500, detail="数据库用户模块接口不匹配，请检查契约")

    async def _send_email(self, receiver: str, html: str, title: str) -> tuple:
        """调用 value.email(receiver, html_content, html_title)，兼容同步/异步实现

        basic/mail.py 的 send() 成功时返回 None，失败时返回 (False, 原因)。
        smtplib 是同步阻塞调用，放到线程池执行，避免卡住事件循环。
        """
        sender = getattr(self.value, "email", None)
        if sender is None:
            return False, "邮箱服务未启用"
        try:
            if inspect.iscoroutinefunction(sender):
                result = await sender(receiver, html, title)
            else:
                result = await asyncio.to_thread(sender, receiver, html, title)
        except Exception as e:
            self.logger.error(f"发送邮件异常: {e}")
            return False, f"邮件发送异常: {e}"
        if result is None:
            return True, "发送成功"
        if isinstance(result, tuple) and len(result) >= 2:
            return bool(result[0]), str(result[1])
        if isinstance(result, bool):
            return result, ("发送成功" if result else "发送失败")
        return True, str(result)

    # ------------------------------------------------------------------ #
    # 路由注册
    # ------------------------------------------------------------------ #
    def _route(self) -> None:
        @self.router.post("/login", summary="登录")
        async def login(body: LoginBody, request: Request, response: Response):
            username = (body.username or "").strip()
            password = body.password or ""
            if not username or not password:
                raise HTTPException(status_code=400, detail="用户名和密码不能为空")
            self._require_db()
            security = self._require_security()

            user = await self._call_user("verify_user", username)
            if not user or not isinstance(user, dict):
                self.logger.warning(f"登录失败：用户不存在 username={username}")
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            if user.get("is_active") in (0, False, "0"):
                self.logger.warning(f"登录失败：账号已禁用 username={username}")
                raise HTTPException(status_code=403, detail="该账号已被禁用，请联系管理员")

            password_hash = user.get("password_hash") or ""
            if not Password.verify(password, password_hash):
                self.logger.warning(f"登录失败：密码错误 username={username}")
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            normalized = normalize_user(user)
            user_id = normalized.get("user_id")
            if not user_id:
                self.logger.error(f"登录失败：用户记录缺少 user_id username={username}")
                raise HTTPException(status_code=500, detail="登录失败，请稍后重试")

            # 密码哈希算法升级（迭代次数变化时静默重算，不打断登录）
            if Password.needs_update(password_hash):
                try:
                    await self._call_user("update_password", user_id, Password.hash(password))
                    self.logger.info(f"用户 {username} 的密码哈希已升级为最新算法")
                except Exception as e:
                    self.logger.warning(f"升级密码哈希失败（不影响本次登录）: {e}")

            # 记录登录 IP 与时间（失败不影响登录）
            ip = "未知"
            try:
                ip = await get_client_ip(request)
                await self._call_user("update_login", user_id, ip)
            except Exception as e:
                self.logger.warning(f"记录登录信息失败（不影响本次登录）: {e}")

            session = await security.login(user)
            if not session:
                raise HTTPException(status_code=500, detail="登录失败，请稍后重试")
            security.set_cookie(response, session["cookie"], session["csrf"])

            self.logger.info(
                f"用户 {normalized.get('username')}({user_id}) 登录成功，ip={ip}"
            )
            return {
                "success": True,
                "message": "登录成功",
                "data": {
                    "user_id": user_id,
                    "username": normalized.get("username"),
                    "level": normalized.get("level", LEVEL_USER),
                    "csrf_token": session["csrf"],
                },
            }

        @self.router.post("/logout", summary="登出")
        async def logout(request: Request, response: Response):
            """登出保持幂等：无有效会话同样返回成功，并清空饼干"""
            security = getattr(self.value, "security", None)
            destroyed = False
            if security is not None:
                try:
                    destroyed = await security.logout(request)
                except Exception as e:
                    self.logger.warning(f"登出时销毁会话失败: {e}")
                try:
                    security.clear_cookie(response)
                except Exception as e:
                    self.logger.warning(f"登出时清除饼干失败: {e}")
            if destroyed:
                self.logger.info("用户登出成功")
            return {
                "success": True,
                "message": "登出成功",
                "data": {"session_destroyed": bool(destroyed)},
            }

        @self.router.get("/me", summary="当前用户")
        async def me(request: Request, user: dict = Depends(current_user)):
            return {
                "success": True,
                "message": "获取成功",
                "data": {
                    "user_id": user.get("user_id"),
                    "username": user.get("username"),
                    "level": user.get("level", LEVEL_USER),
                    "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "request_id": getattr(getattr(request, "state", None), "request_id", ""),
                },
            }

        @self.router.post("/register", summary="注册")
        async def register(body: RegisterBody):
            server = self._server_config()
            if not _as_bool(server.get("register", False), False):
                raise HTTPException(status_code=403, detail="注册功能未开放")

            username = (body.username or "").strip()
            password = body.password or ""
            email = (body.email or "").strip() or None

            if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
                raise HTTPException(
                    status_code=400,
                    detail=f"用户名长度需在 {USERNAME_MIN}-{USERNAME_MAX} 个字符之间",
                )
            if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
                raise HTTPException(
                    status_code=400,
                    detail=f"密码长度需在 {PASSWORD_MIN}-{PASSWORD_MAX} 个字符之间",
                )
            if email is not None and not EMAIL_PATTERN.match(email):
                raise HTTPException(status_code=400, detail="邮箱格式不正确")

            # 邮箱验证码校验：仅当启用邮箱服务且请求带邮箱时强制校验
            email_enabled = _as_bool(server.get("email", False), False)
            if email and email_enabled:
                code = (body.code or "").strip()
                if not code:
                    raise HTTPException(status_code=400, detail="请输入邮箱验证码")
                cache = self._require_cache()
                key = EMAIL_CODE_KEY.format(email=email.lower(), scene="register")
                try:
                    stored = await cache.get(key)
                except Exception as e:
                    self.logger.error(f"读取邮箱验证码失败: {e}")
                    raise HTTPException(status_code=503, detail="缓存不可用")
                if not stored or str(stored) != code:
                    raise HTTPException(status_code=400, detail="验证码错误或已过期")
                try:
                    await cache.delete(key)
                except Exception as e:
                    self.logger.warning(f"删除已使用的邮箱验证码失败: {e}")

            password_hash = Password.hash(password)
            result = await self._call_user(
                "register_user", username, password_hash, email=email
            )
            ok, message, _extra = _unpack_result(result)
            if not ok:
                # 用户名重复返回 409，其余注册失败返回 400
                if "存在" in message or "重复" in message:
                    raise HTTPException(status_code=409, detail=message or "用户名已存在")
                raise HTTPException(status_code=400, detail=message or "注册失败")

            self.logger.info(f"用户 {username} 注册成功")
            return {
                "success": True,
                "message": message or "注册成功",
                "data": {"username": username},
            }

        @self.router.post("/password", summary="修改密码")
        async def change_password(
            body: PasswordBody,
            request: Request,
            response: Response,
            user: dict = Depends(current_user),
        ):
            self._require_db()
            security = self._require_security()

            old_password = body.old_password or ""
            new_password = body.new_password or ""
            if not old_password or not new_password:
                raise HTTPException(status_code=400, detail="原密码和新密码不能为空")
            if not (PASSWORD_MIN <= len(new_password) <= PASSWORD_MAX):
                raise HTTPException(
                    status_code=400,
                    detail=f"新密码长度需在 {PASSWORD_MIN}-{PASSWORD_MAX} 个字符之间",
                )
            if old_password == new_password:
                raise HTTPException(status_code=400, detail="新密码不能与原密码相同")

            user_id = user.get("user_id")
            # get_user_by_id 可能不回传 password_hash，故再按用户名兜底取一次
            password_hash = None
            try:
                record = await self._call_user("get_user_by_id", user_id)
                if isinstance(record, dict):
                    password_hash = record.get("password_hash")
            except HTTPException:
                raise
            except Exception as e:
                self.logger.warning(f"读取用户信息失败: {e}")
            if not password_hash:
                record = await self._call_user("verify_user", user.get("username") or "")
                if isinstance(record, dict):
                    password_hash = record.get("password_hash")
            if not password_hash:
                raise HTTPException(status_code=404, detail="用户不存在或状态异常")

            if not Password.verify(old_password, password_hash):
                self.logger.warning(f"修改密码失败：原密码错误 user_id={user_id}")
                raise HTTPException(status_code=400, detail="原密码不正确")

            result = await self._call_user("update_password", user_id, Password.hash(new_password))
            ok, message, _extra = _unpack_result(result)
            if not ok:
                raise HTTPException(status_code=500, detail=message or "密码修改失败")

            # 安全考虑：销毁当前会话并要求重新登录。
            # （本项目没有“用户 -> 会话列表”索引，无法批量踢出其它设备的会话；
            #   其它设备上的会话会在登录有效期到期后自然失效。）
            try:
                await security.logout(request)
                security.clear_cookie(response)
            except Exception as e:
                self.logger.warning(f"修改密码后清理会话失败: {e}")

            self.logger.info(f"用户 user_id={user_id} 修改密码成功，当前会话已销毁")
            return {"success": True, "message": "密码修改成功，请重新登录"}

        @self.router.post("/email/code", summary="发送邮箱验证码")
        async def send_email_code(body: EmailCodeBody):
            server = self._server_config()
            if not _as_bool(server.get("email", False), False):
                raise HTTPException(status_code=503, detail="邮箱服务未启用")

            email = (body.email or "").strip()
            if not email or not EMAIL_PATTERN.match(email):
                raise HTTPException(status_code=400, detail="邮箱格式不正确")
            scene = (body.scene or "register").strip().lower()
            if scene not in ALLOWED_SCENES:
                raise HTTPException(status_code=400, detail="不支持的验证码场景")
            if getattr(self.value, "email", None) is None:
                raise HTTPException(status_code=503, detail="邮箱服务未启用")

            cache = self._require_cache()
            code_key = EMAIL_CODE_KEY.format(email=email.lower(), scene=scene)
            cooldown_key = EMAIL_COOLDOWN_KEY.format(email=email.lower(), scene=scene)

            # 防刷：同一邮箱 60 秒内不重复发送
            try:
                if await cache.get(cooldown_key):
                    raise HTTPException(status_code=429, detail="验证码发送过于频繁，请稍后再试")
            except HTTPException:
                raise
            except Exception as e:
                self.logger.warning(f"读取邮箱验证码冷却标记失败: {e}")

            code = "".join(secrets.choice("0123456789") for _ in range(6))
            try:
                stored = await cache.set(code_key, code, ex=EMAIL_CODE_TTL)
            except Exception as e:
                self.logger.error(f"保存邮箱验证码失败: {e}")
                stored = False
            if not stored:
                raise HTTPException(status_code=500, detail="验证码保存失败，请稍后重试")

            html = (
                "<div style=\"font-family:sans-serif;font-size:14px;color:#333\">"
                "<p>您好，本次操作的邮箱验证码为：</p>"
                f"<p style=\"font-size:24px;font-weight:bold;letter-spacing:4px\">{code}</p>"
                f"<p>验证码 {EMAIL_CODE_TTL // 60} 分钟内有效，请勿泄露给他人。</p>"
                "<p>如果这不是您本人的操作，请忽略本邮件。</p>"
                "<p>—— TWSC-NG</p></div>"
            )
            title = f"TWSC-NG 邮箱验证码（{scene}）"
            ok, detail = await self._send_email(email, html, title)
            if not ok:
                # 发送失败要清理验证码，避免留下“已发未达”的脏键
                try:
                    await cache.delete(code_key)
                except Exception as e:
                    self.logger.warning(f"发送失败后清理验证码失败: {e}")
                self.logger.error(f"邮箱验证码发送失败 email={email} scene={scene}: {detail}")
                raise HTTPException(status_code=500, detail=f"验证码发送失败：{detail}")

            try:
                await cache.set(cooldown_key, "1", ex=EMAIL_COOLDOWN_TTL)
            except Exception as e:
                self.logger.warning(f"写入邮箱验证码冷却标记失败: {e}")

            self.logger.info(f"邮箱验证码已发送 email={email} scene={scene}")
            return {"success": True, "message": "验证码已发送"}
