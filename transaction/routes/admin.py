# -*- coding:utf-8 -*-
"""后台认证路由：/admin/*

本项目不写后台界面，只提供 API：
- GET  /admin/ping                 后台认证探针（>= 管理员）
- GET  /admin/whoami               后台身份详情（>= 管理员）
- GET  /admin/users                用户列表（超级管理员）
- POST /admin/users                新增用户（超级管理员）
- POST /admin/users/{id}/active    启用/禁用（超级管理员）
- POST /admin/users/{id}/level     调整等级（超级管理员）

等级约定：-128 超级管理员 / -100 管理员 / 0 普通用户（数值越小权限越高）。
"""
import inspect
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import Password, LEVEL_ADMIN, LEVEL_SUPER, LEVEL_USER, require_level
from . import get_base_url

# 允许的等级取值（新增用户 / 调整等级都只接受这三种）
ALLOWED_LEVELS = (LEVEL_SUPER, LEVEL_ADMIN, LEVEL_USER)

USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN, PASSWORD_MAX = 6, 64


# --------------------------------------------------------------------------- #
# 请求模型（JSON）
# --------------------------------------------------------------------------- #
class UserCreateBody(BaseModel):
    username: str
    password: str
    level: int = LEVEL_USER
    belong: int = 0
    email: Optional[str] = None


class ActiveBody(BaseModel):
    active: bool


class LevelBody(BaseModel):
    level: int


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _level_of(record: Any) -> int:
    """读取用户记录的等级（兼容 level / role_level 两种字段名）"""
    if not isinstance(record, dict):
        return LEVEL_USER
    raw = record.get("level", record.get("role_level", LEVEL_USER))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return LEVEL_USER


def _same_id(left: Any, right: Any) -> bool:
    """宽松比较两个 user_id（数据库可能返回 int 或 str）"""
    if left is None or right is None:
        return False
    return str(left).strip() == str(right).strip()


def _filter_kwargs(method: Any, kwargs: dict) -> dict:
    """按被调方法签名过滤可选关键字参数（跨包契约未完全对齐时的兜底）"""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return kwargs
    params = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _unpack_result(result: Any) -> tuple:
    """统一数据库操作的返回形态：(bool, str) / (bool, str, extra) / bool / dict"""
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


def _public_user(record: Any) -> dict:
    """对外返回的用户信息：剔除 password_hash，并补齐 level 字段"""
    if not isinstance(record, dict):
        return {}
    data = {k: v for k, v in record.items() if k != "password_hash"}
    data["level"] = _level_of(record)
    return data


class Admin:
    """后台认证路由类（prefix = {base_url}/admin）"""

    def __init__(self, value):
        if value is None:
            raise ValueError("Admin 路由初始化失败：value 为空")
        self.value = value
        self.logger = getattr(value, "logger", None)
        if self.logger is None:
            raise ValueError("Admin 路由初始化失败：value.logger 未初始化")
        self.base_url = get_base_url(value)
        self.router = APIRouter(prefix=f"{self.base_url}/admin", tags=["后台认证"])
        self._route()

    # ------------------------------------------------------------------ #
    # value 依赖（鸭子类型）
    # ------------------------------------------------------------------ #
    @property
    def database(self):
        return getattr(self.value, "database", None)

    def _require_db(self):
        database = self.database
        if database is None or not getattr(database, "connected", False):
            raise HTTPException(status_code=503, detail="数据库不可用")
        return database

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

    # ------------------------------------------------------------------ #
    # 路由注册
    # ------------------------------------------------------------------ #
    def _route(self) -> None:
        @self.router.get("/ping", summary="后台认证探针")
        async def ping(user: dict = Depends(require_level(LEVEL_ADMIN))):
            return {
                "success": True,
                "message": "后台认证通过",
                "data": {
                    "level": user.get("level", LEVEL_USER),
                    "user_id": user.get("user_id"),
                    "username": user.get("username"),
                },
            }

        @self.router.get("/whoami", summary="后台身份详情")
        async def whoami(user: dict = Depends(require_level(LEVEL_ADMIN))):
            self._require_db()
            record = await self._call_user("get_user_by_id", user.get("user_id"))
            if not record:
                raise HTTPException(status_code=404, detail="用户不存在")
            return {
                "success": True,
                "message": "获取成功",
                "data": _public_user(record),
            }

        @self.router.get("/users", summary="用户列表")
        async def list_users(user: dict = Depends(require_level(LEVEL_SUPER))):
            self._require_db()
            records = await self._call_user("get_users")
            if records is None:
                records = []
            if not isinstance(records, (list, tuple)):
                self.logger.error("database.user.get_users 返回值不是列表（跨包契约未对齐）")
                raise HTTPException(status_code=500, detail="数据库用户模块接口不匹配，请检查契约")
            users = [_public_user(item) for item in records]
            return {
                "success": True,
                "message": "获取成功",
                "data": {"users": users, "total": len(users)},
            }

        @self.router.post("/users", summary="新增用户")
        async def create_user(body: UserCreateBody, user: dict = Depends(require_level(LEVEL_SUPER))):
            self._require_db()
            username = (body.username or "").strip()
            password = body.password or ""
            level = int(body.level)
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
            if level not in ALLOWED_LEVELS:
                raise HTTPException(status_code=400, detail="等级只允许 -128 / -100 / 0")

            existing = await self._call_user("get_user_by_username", username)
            if existing:
                raise HTTPException(status_code=409, detail="用户名已存在")

            result = await self._call_user(
                "add_user",
                username,
                Password.hash(password),
                level=level,
                belong=int(body.belong or 0),
                email=body.email,
            )
            ok, message, extra = _unpack_result(result)
            if not ok:
                if "存在" in message or "重复" in message:
                    raise HTTPException(status_code=409, detail=message or "用户名已存在")
                raise HTTPException(status_code=400, detail=message or "新增用户失败")

            # 取新用户 user_id：优先用返回值里带出来的，其次回查一次
            user_id = extra if isinstance(extra, int) else None
            if user_id is None:
                record = await self._call_user("get_user_by_username", username)
                if isinstance(record, dict):
                    user_id = record.get("user_id", record.get("id"))
            self.logger.info(f"管理员 {user.get('username')} 新增用户 {username}（level={level}）")
            return {
                "success": True,
                "message": message or "用户创建成功",
                "data": {"user_id": None if user_id is None else str(user_id), "username": username, "level": level},
            }

        @self.router.post("/users/{user_id}/active", summary="启用/禁用用户")
        async def set_active(
            user_id: str,
            body: ActiveBody,
            user: dict = Depends(require_level(LEVEL_SUPER)),
        ):
            self._require_db()
            record = await self._call_user("get_user_by_id", user_id)
            if not record:
                raise HTTPException(status_code=404, detail="用户不存在")
            if _same_id(user_id, user.get("user_id")) and not body.active:
                raise HTTPException(status_code=400, detail="不能禁用当前登录的账号")

            result = await self._call_user("update_active", user_id, bool(body.active))
            ok, message, _extra = _unpack_result(result)
            if not ok:
                raise HTTPException(status_code=400, detail=message or "操作失败")

            self.logger.info(
                f"管理员 {user.get('username')} 将用户 {user_id} 设为"
                f"{'启用' if body.active else '禁用'}"
            )
            return {
                "success": True,
                "message": "账号已启用" if body.active else "账号已禁用",
                "data": {"user_id": str(user_id), "active": bool(body.active)},
            }

        @self.router.post("/users/{user_id}/level", summary="调整用户等级")
        async def set_level(
            user_id: str,
            body: LevelBody,
            user: dict = Depends(require_level(LEVEL_SUPER)),
        ):
            self._require_db()
            level = int(body.level)
            if level not in ALLOWED_LEVELS:
                raise HTTPException(status_code=400, detail="等级只允许 -128 / -100 / 0")

            record = await self._call_user("get_user_by_id", user_id)
            if not record:
                raise HTTPException(status_code=404, detail="用户不存在")
            if _same_id(user_id, user.get("user_id")):
                raise HTTPException(status_code=400, detail="不能修改自己的等级")

            target_level = _level_of(record)
            if target_level == LEVEL_SUPER and level != LEVEL_SUPER:
                # 保留至少一个超级管理员：降级前统计现有超级管理员数量
                records = await self._call_user("get_users")
                if not isinstance(records, (list, tuple)):
                    self.logger.error("database.user.get_users 返回值不是列表（跨包契约未对齐）")
                    raise HTTPException(status_code=500, detail="数据库用户模块接口不匹配，请检查契约")
                super_users = [item for item in records if _level_of(item) == LEVEL_SUPER]
                if len(super_users) <= 1:
                    raise HTTPException(
                        status_code=400,
                        detail="系统必须保留至少一个超级管理员，不能降级最后一个超级管理员",
                    )

            result = await self._call_user("update_level", user_id, level)
            ok, message, _extra = _unpack_result(result)
            if not ok:
                raise HTTPException(status_code=400, detail=message or "操作失败")

            self.logger.info(
                f"管理员 {user.get('username')} 将用户 {user_id} 等级由 {target_level} 调整为 {level}"
            )
            return {
                "success": True,
                "message": "等级调整成功",
                "data": {"user_id": str(user_id), "level": level},
            }
