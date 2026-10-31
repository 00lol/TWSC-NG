# -*- coding:utf-8 -*-
"""管理台（/admin/*）接口框架

前端契约见 docs/API-接口清单.md（61 条接口），本模块提供所有业务插件共用的：

- 统一响应包 ``{code,message,data,traceId}`` 与错误码；
- ``ApiError``：业务错误抛这个，由全局异常处理器转成外壳；
- 分页：``parse_page`` / ``paged``；
- 时间、金额、手机号脱敏、CSV 导出；
- Bearer 令牌：``issue_admin_token`` / ``console_admin`` / ``admin_guard``；
- 停用与改角色的即时生效（缓存标记）；审计日志钩子 ``audit``。

约定：管理台接口全部返回本模块的外壳；``/auth/*`` 等旧接口不受影响
（中间件按路径前缀区分）。
"""
import csv
import io
import json
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone, time as dtime
from typing import Any, Callable, Iterable, Optional, Sequence

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .auth import CookieSigner, get_client_ip

# --------------------------------------------------------------------------- #
# 错误码（与前端契约一致）
# --------------------------------------------------------------------------- #
CODE_OK = 0
CODE_PARAM = 1001          # 参数不合法 -> HTTP 400
CODE_UNAUTH = 2001         # 用户未登录 / 登录过期 -> HTTP 401
CODE_FORBIDDEN = 2002      # 无权限 -> HTTP 403
CODE_ADMIN_UNAUTH = 2003   # 管理员登录过期 / 账号不可用 -> HTTP 401
CODE_NOT_FOUND = 3001      # 资源不存在 -> HTTP 404
CODE_CONFLICT = 3002       # 状态冲突 -> HTTP 409
CODE_DUPLICATE = 3003      # 重复提交 / 已存在 -> HTTP 409
CODE_NOT_ALLOWED = 3004    # 准入未通过（C 端预留）
CODE_SERVER = 5000         # 服务异常 -> HTTP 500

# 令牌与缓存
TOKEN_SALT = "twsc.console"
TOKEN_HOURS_DEFAULT = 12
REVOKE_KEY = "twsc:console:revoked:{admin_id}"
ROLE_KEY = "twsc:console:role:{admin_id}"
LOCK_KEY = "twsc:console:lock:{account}:{ip}"

# 时区：接口统一 +08:00
TZ = timezone(timedelta(hours=8))

_trace_id: ContextVar[str] = ContextVar("twsc_console_trace_id", default="")


# --------------------------------------------------------------------------- #
# traceId 与响应包
# --------------------------------------------------------------------------- #
def set_trace_id(value: str) -> None:
    """由中间件在每个请求开始时写入"""
    _trace_id.set(str(value or ""))


def trace_id() -> str:
    """当前请求的 traceId（中间件写入，未写入时为空串）"""
    return _trace_id.get() or ""


def envelope(data: Any = None, message: str = "ok", code: int = CODE_OK) -> dict:
    """统一响应包"""
    return {"code": int(code), "message": str(message), "data": data, "traceId": trace_id()}


class ApiError(Exception):
    """业务错误：由全局异常处理器转成管理台外壳"""

    def __init__(
        self,
        code: int = CODE_PARAM,
        message: str = "参数不合法",
        http_status: int = 400,
        data: Any = None,
    ):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.http_status = int(http_status)
        self.data = data


def ok(data: Any = None, message: str = "ok") -> JSONResponse:
    """成功响应"""
    return JSONResponse(status_code=200, content=envelope(data, message, CODE_OK))


def fail(
    code: int,
    message: str,
    http_status: int = 400,
    data: Any = None,
) -> JSONResponse:
    """失败响应（优先用 raise ApiError）"""
    return JSONResponse(
        status_code=int(http_status),
        content=envelope(data, message, code),
    )


def error_response(exc: ApiError) -> JSONResponse:
    """把 ApiError 转成响应"""
    return fail(exc.code, exc.message, exc.http_status, exc.data)


def install_handlers(app) -> None:
    """注册 ApiError 全局处理器（main.py 组装阶段调用）"""

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return error_response(exc)


# --------------------------------------------------------------------------- #
# 分页
# --------------------------------------------------------------------------- #
def to_int(value: Any, default: int = 0) -> int:
    """宽松转 int"""
    try:
        if value is None or value == "":
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_page(request: Request, default: int = 20, maximum: int = 50) -> tuple:
    """解析 page / pageSize（默认 20，上限 50），返回 (page, page_size)"""
    query = request.query_params
    page = max(1, to_int(query.get("page"), 1))
    page_size = to_int(query.get("pageSize"), default)
    if page_size <= 0:
        page_size = default
    page_size = min(page_size, maximum)
    return page, page_size


def paged(items: Iterable, total: int, page: int, page_size: int) -> dict:
    """分页外壳"""
    total = max(0, int(total or 0))
    return {
        "list": list(items or []),
        "total": total,
        "page": int(page),
        "pageSize": int(page_size),
        "hasMore": int(page) * int(page_size) < total,
    }


# --------------------------------------------------------------------------- #
# 时间 / 金额 / 脱敏
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    """当前时间 ISO8601（+08:00，毫秒）"""
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def now_dt() -> datetime:
    """当前时间（naive，写库用）"""
    return datetime.now(TZ).replace(tzinfo=None)


def to_iso(value: Any) -> Optional[str]:
    """DB 时间 -> ISO8601 字符串；空值返回 None"""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            dt = value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
            return dt.isoformat(timespec="milliseconds")
        if isinstance(value, date):
            return datetime.combine(value, dtime.min, tzinfo=TZ).isoformat(timespec="milliseconds")
        text = str(value).strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text)
        dt = parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
        return dt.isoformat(timespec="milliseconds")
    except Exception:
        return str(value)


def to_date(value: Any) -> Optional[str]:
    """DB 时间 -> YYYY-MM-DD"""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")
        if isinstance(value, date):
            return value.strftime("%Y-%m-%d")
        text = str(value).strip()
        return datetime.fromisoformat(text).strftime("%Y-%m-%d") if text else None
    except Exception:
        return str(value)[:10] if value else None


def today_str() -> str:
    """今天（+08:00）YYYY-MM-DD"""
    return datetime.now(TZ).strftime("%Y-%m-%d")


def parse_date(value: Any) -> Optional[date]:
    """解析 YYYY-MM-DD / ISO 字符串"""
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date()
    except Exception:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def mask_phone(phone: Any) -> Optional[str]:
    """手机号脱敏：138****8000；空值返回 None"""
    text = str(phone or "").strip()
    if not text:
        return None
    if len(text) <= 3:
        return "*" * len(text)
    if len(text) < 7:
        return text[0] + "****"
    return f"{text[:3]}****{text[-4:]}"


def yuan(amount: Any) -> Optional[float]:
    """分 -> 元（仅调试/展示用，接口一律返回分）"""
    if amount is None:
        return None
    try:
        return round(int(amount) / 100, 2)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# CSV 导出
# --------------------------------------------------------------------------- #
def csv_response(filename: str, header: Sequence, rows: Iterable[Sequence]) -> Response:
    """CSV 响应：UTF-8 BOM + \\r\\n + attachment 头"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(header))
    for row in rows or []:
        writer.writerow(list(row))
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# 令牌（Bearer）
# --------------------------------------------------------------------------- #
def _value_of(request: Request) -> Any:
    """取全局 value（web.py 在创建应用时挂在 app.state.value）"""
    return getattr(request.app.state, "value", None)


def _server_config(value: Any) -> dict:
    config = getattr(value, "config", None)
    return dict((config or {}).get("server") or {})


def token_hours(value: Any) -> int:
    """令牌有效期（小时）"""
    hours = to_int(_server_config(value).get("console_token_hours"), TOKEN_HOURS_DEFAULT)
    return hours if hours > 0 else TOKEN_HOURS_DEFAULT


def signer_of(value: Any) -> CookieSigner:
    """管理台令牌签名器（与登录饼干共用 server.secret，独立 salt）"""
    secret = str(_server_config(value).get("secret") or "").strip() or "twsc-console-secret"
    return CookieSigner(secret, salt=TOKEN_SALT)


def issue_admin_token(value: Any, admin: dict) -> str:
    """登录成功时签发 Bearer 令牌（12h）"""
    payload = {
        "id": int(admin["id"]),
        "username": admin.get("username"),
        "name": admin.get("name"),
        "role": admin.get("role") or "op",
    }
    return signer_of(value).sign(payload)


def bearer_token(request: Request) -> Optional[str]:
    """从 Authorization: Bearer <token> 取令牌"""
    raw = request.headers.get("Authorization") or ""
    scheme, _, token = raw.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    token = token.strip()
    return token or None


async def console_admin(request: Request) -> dict:
    """依赖：校验 Bearer 令牌，返回当前管理员 {id, username, name, role}"""
    token = bearer_token(request)
    if not token:
        raise ApiError(CODE_ADMIN_UNAUTH, "管理员登录已过期，请重新登录", 401)
    value = _value_of(request)
    if value is None:
        raise ApiError(CODE_SERVER, "服务异常：应用未初始化", 500)

    payload = signer_of(value).verify(token, max_age=token_hours(value) * 3600)
    if not payload or "id" not in payload:
        raise ApiError(CODE_ADMIN_UNAUTH, "管理员登录已过期，请重新登录", 401)

    try:
        admin_id = int(payload["id"])
    except (TypeError, ValueError):
        raise ApiError(CODE_ADMIN_UNAUTH, "管理员登录已过期，请重新登录", 401)

    # 停用 / 改角色即时生效：缓存标记（缓存不可用时退化为令牌内信息）
    cache = getattr(value, "cache", None)
    if cache is not None:
        try:
            revoked = await cache.get(REVOKE_KEY.format(admin_id=admin_id))
            if revoked:
                raise ApiError(CODE_ADMIN_UNAUTH, "账号已被停用，请联系管理员", 401)
            override = await cache.get(ROLE_KEY.format(admin_id=admin_id))
            if override:
                payload["role"] = override
        except ApiError:
            raise
        except Exception:
            pass

    return {
        "id": admin_id,
        "username": payload.get("username"),
        "name": payload.get("name"),
        "role": payload.get("role") or "op",
    }


def admin_guard(roles: Optional[Sequence[str]] = None, write: bool = False) -> Callable:
    """依赖工厂

    Args:
        roles: 允许的角色；None 表示 super/op/developer 都可读
        write: True 表示写接口，developer 一律 403
    """
    allowed = tuple(roles) if roles else None

    async def _dependency(request: Request) -> dict:
        admin = await console_admin(request)
        if allowed and admin.get("role") not in allowed:
            raise ApiError(CODE_FORBIDDEN, "无权限", 403)
        if write and admin.get("role") == "developer":
            raise ApiError(CODE_FORBIDDEN, "无权限：开发者账号为只读", 403)
        return admin

    return _dependency


# --------------------------------------------------------------------------- #
# 停用 / 改角色的即时生效（缓存标记，TTL 与令牌一致）
# --------------------------------------------------------------------------- #
async def _cache_set(value: Any, key: str, payload: str) -> None:
    cache = getattr(value, "cache", None)
    if cache is None:
        return
    try:
        await cache.set(key, payload, ex=token_hours(value) * 3600)
    except Exception:
        pass


async def _cache_delete(value: Any, key: str) -> None:
    cache = getattr(value, "cache", None)
    if cache is None:
        return
    try:
        await cache.delete(key)
    except Exception:
        pass


async def revoke_admin(value: Any, admin_id: int) -> None:
    """停用账号：令牌立即失效"""
    await _cache_set(value, REVOKE_KEY.format(admin_id=int(admin_id)), "1")


async def unrevoke_admin(value: Any, admin_id: int) -> None:
    """恢复账号"""
    await _cache_delete(value, REVOKE_KEY.format(admin_id=int(admin_id)))


async def set_admin_role(value: Any, admin_id: int, role: str) -> None:
    """改角色：不用重新登录即可生效"""
    await _cache_set(value, ROLE_KEY.format(admin_id=int(admin_id)), str(role))


async def clear_admin_role(value: Any, admin_id: int) -> None:
    """清除角色覆盖（删除账号等场景）"""
    await _cache_delete(value, ROLE_KEY.format(admin_id=int(admin_id)))


# --------------------------------------------------------------------------- #
# 其他工具
# --------------------------------------------------------------------------- #
def require_db(plugin) -> None:
    """数据库不可用 -> 503/5000（服务可降级，但业务接口要明确报错）"""
    database = getattr(plugin, "database", None)
    if database is None or not getattr(database, "connected", False):
        raise ApiError(CODE_SERVER, "服务异常：数据库不可用", 503)


async def client_ip(request: Request) -> str:
    """客户端 IP（受信代理时才信任 X-Forwarded-For）"""
    try:
        return await get_client_ip(request)
    except Exception:
        try:
            return request.client.host if request.client else "未知"
        except Exception:
            return "未知"


async def audit(
    request: Request,
    admin: dict,
    module: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Any = None,
    detail: Optional[Any] = None,
    result: str = "success",
) -> None:
    """写操作日志（由 console_auth 插件提供落库钩子，未安装时只记 debug 日志）"""
    value = _value_of(request)
    hooks = getattr(value, "hooks", None) or {}
    hook = hooks.get("console_audit")
    if not callable(hook):
        logger = getattr(value, "logger", None)
        if logger is not None:
            try:
                logger.debug(f"[审计未安装] {module}/{action} admin={admin.get('id')} target={target_id}")
            except Exception:
                pass
        return
    try:
        await hook(
            admin=admin,
            module=module,
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            detail=detail or {},
            result=result,
            ip=await client_ip(request),
        )
    except Exception as e:
        logger = getattr(value, "logger", None)
        if logger is not None:
            try:
                logger.warning(f"写审计日志失败（不影响业务）: {e}")
            except Exception:
                pass


def jsonable(value: Any) -> Any:
    """把 DB 行里的 JSON 字符串/日期转成可序列化结构"""
    if value is None:
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return to_iso(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                return json.loads(text)
            except Exception:
                return value
    return value


def split_values(value: Any) -> list:
    """JSON 文本/列表统一成 list"""
    parsed = jsonable(value)
    if isinstance(parsed, list):
        return parsed
    if parsed in (None, ""):
        return []
    return [parsed]


__all__ = [
    "CODE_OK", "CODE_PARAM", "CODE_UNAUTH", "CODE_FORBIDDEN", "CODE_ADMIN_UNAUTH",
    "CODE_NOT_FOUND", "CODE_CONFLICT", "CODE_DUPLICATE", "CODE_NOT_ALLOWED", "CODE_SERVER",
    "ApiError", "ok", "fail", "envelope", "error_response", "install_handlers",
    "set_trace_id", "trace_id", "parse_page", "paged", "to_int",
    "now_iso", "now_dt", "to_iso", "to_date", "today_str", "parse_date",
    "mask_phone", "yuan", "csv_response",
    "issue_admin_token", "bearer_token", "console_admin", "admin_guard",
    "revoke_admin", "unrevoke_admin", "set_admin_role", "clear_admin_role",
    "require_db", "client_ip", "audit", "jsonable", "split_values",
]
