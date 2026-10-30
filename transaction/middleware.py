# -*- coding:utf-8 -*-
"""请求事务层 —— HTTP 中间件与全局异常兜底

职责（按请求顺序）：
1. 生成 request_id / 开始时间，挂到 request.state；
2. 取真实客户端 IP（仅可信代理才信任 X-Forwarded-For）；
3. 会话装载：request.state.user = await value.security.resolve(request)；
4. CSRF 双提交校验（不安全方法，Cookie 与 X-CSRF-Token 请求头比对）；
5. 调用下游路由；
6. 安全响应头 + 访问日志；
7. 异常兜底：HTTPException / 参数校验 / 未捕获异常统一返回 JSON。

统一响应体：
- 成功：{"success": True,  "message": str, "data": any}
- 失败：{"success": False, "message": str, "code": int}

注意：``@app.middleware("http")`` 必须在应用启动前注册，main.py 会在 uvicorn 启动前调用 main()。
"""
import hmac
import time
import uuid
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import Security, get_client_ip, parse_trusted_proxies, set_trusted_proxies

# 不参与 CSRF 校验的路径（登录/注册本身无会话，健康检查与文档不改变状态）
CSRF_EXEMPT_PATHS = {
    "/auth/login",
    "/auth/register",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
}

# 需要 CSRF 校验的请求方法
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class Middleware:
    """请求事务中间件

    使用方式（main.py）::

        transaction.Middleware(Value).main()
    """

    def __init__(self, value):
        self.value = value
        self.logger = getattr(value, "logger", None)
        if self.logger is None:
            raise ValueError("Middleware 初始化失败：value.logger 未初始化")

        server = self._server_config()
        self.debug = str(server.get("debug", False)).strip().lower() in {"1", "true", "yes", "on"}
        self.ssl = str(server.get("ssl", False)).strip().lower() in {"1", "true", "yes", "on"}
        base_url = server.get("base_url") or server.get("BaseUrl") or ""
        self.base_url = str(base_url).strip().rstrip("/")
        if self.base_url and not self.base_url.startswith("/"):
            self.base_url = "/" + self.base_url

        # 可信代理（get_client_ip 使用）
        self.trusted_proxies = parse_trusted_proxies(getattr(value, "config", None))
        set_trusted_proxies(self.trusted_proxies)

    # ------------------------------------------------------------------ #
    # 配置与路径工具
    # ------------------------------------------------------------------ #
    def _server_config(self) -> dict:
        config = getattr(self.value, "config", None)
        if isinstance(config, dict):
            server = config.get("server")
            if isinstance(server, dict):
                return server
            legacy = config.get("Sever")  # 兼容旧 TWSC 配置结构
            if isinstance(legacy, dict):
                return legacy
        return {}

    def _normalize_path(self, path: str) -> str:
        """去掉 base_url 前缀，得到用于白名单匹配的规范化路径"""
        path = path or "/"
        if self.base_url and path.startswith(self.base_url):
            path = path[len(self.base_url):] or "/"
        return path or "/"

    @staticmethod
    def _csrf_exempt(path: str) -> bool:
        normalized = (path or "/").rstrip("/") or "/"
        if normalized in CSRF_EXEMPT_PATHS:
            return True
        # 文档相关路由允许带子路径（/docs/oauth2-redirect 等）
        return (
            normalized.startswith("/docs")
            or normalized.startswith("/redoc")
            or normalized.startswith("/openapi.json")
        )

    # ------------------------------------------------------------------ #
    # 响应构造
    # ------------------------------------------------------------------ #
    @staticmethod
    def _message(detail: Any) -> str:
        """把 HTTPException.detail（可能是 dict/list）转成可展示的中文消息"""
        if isinstance(detail, str):
            return detail
        if detail is None:
            return "请求失败"
        try:
            import json

            return json.dumps(detail, ensure_ascii=False)
        except Exception:
            return str(detail)

    def _error_response(
        self,
        status_code: int,
        message: Any,
        headers: Optional[dict] = None,
    ) -> JSONResponse:
        """统一失败响应体"""
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = 500
        return JSONResponse(
            status_code=code,
            content={"success": False, "message": self._message(message), "code": code},
            headers=headers,
        )

    def _finish(self, response, request: Request, start: float, request_id: str, ip: str):
        """统一的响应收尾：安全头 + 访问日志"""
        try:
            headers = response.headers
            headers.setdefault("X-Content-Type-Options", "nosniff")
            headers.setdefault("X-Frame-Options", "DENY")
            headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            headers.setdefault("X-Request-ID", request_id)
            if self.ssl:
                headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

            path = self._normalize_path(request.url.path)
            if path.startswith("/static"):
                headers.setdefault("Cache-Control", "public, max-age=2")
            elif path == "/health" or path.startswith("/auth") or path.startswith("/admin"):
                headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                headers.setdefault("Pragma", "no-cache")
            else:
                headers.setdefault("Cache-Control", "no-store")
        except Exception as e:
            try:
                self.logger.warning(f"[{request_id}] 写入安全响应头失败: {e}")
            except Exception:
                pass

        try:
            cost_ms = (time.perf_counter() - start) * 1000
            user = getattr(getattr(request, "state", None), "user", None)
            user_text = f"{user.get('username')}({user.get('user_id')})" if user else "-"
            self.logger.debug(
                f"[{request_id}] {request.method} {request.url.path} -> "
                f"{getattr(response, 'status_code', 0)} {cost_ms:.1f}ms ip={ip} user={user_text}"
            )
        except Exception:
            pass
        return response

    # ------------------------------------------------------------------ #
    # 主处理逻辑
    # ------------------------------------------------------------------ #
    async def _dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        request_id = uuid.uuid4().hex[:16]
        state = request.state
        state.request_id = request_id
        state.start_time = start
        state.user = None

        # 真实 IP
        ip = "未知"
        try:
            ip = await get_client_ip(request)
        except Exception as e:
            try:
                self.logger.warning(f"[{request_id}] 解析客户端 IP 失败: {e}")
            except Exception:
                pass
        state.client_ip = ip

        # 会话装载（security 未就绪 / 异常时按未登录处理）
        security = getattr(self.value, "security", None)
        if security is not None:
            try:
                state.user = await security.resolve(request)
            except Exception as e:
                try:
                    self.logger.warning(f"[{request_id}] 会话装载失败，按未登录处理: {e}")
                except Exception:
                    pass
                state.user = None
        else:
            try:
                self.logger.warning(f"[{request_id}] value.security 未就绪，按未登录处理")
            except Exception:
                pass

        # CSRF 双提交校验（白名单放行；没有 CSRF Cookie 时放行给路由自己判 401）
        if request.method.upper() in UNSAFE_METHODS and not self._csrf_exempt(
            self._normalize_path(request.url.path)
        ):
            try:
                cookie_token = request.cookies.get(Security.CSRF_COOKIE_NAME)
                header_token = request.headers.get(Security.CSRF_HEADER)
            except Exception:
                cookie_token, header_token = None, None
            if cookie_token:
                matched = False
                try:
                    matched = bool(header_token) and hmac.compare_digest(
                        str(cookie_token).encode("utf-8"), str(header_token).encode("utf-8")
                    )
                except Exception:
                    matched = False
                if not matched:
                    try:
                        self.logger.warning(
                            f"[{request_id}] CSRF 校验失败：{request.method} {request.url.path} ip={ip}"
                        )
                    except Exception:
                        pass
                    response = self._error_response(403, "CSRF 校验失败，请刷新页面后重试")
                    return self._finish(response, request, start, request_id, ip)

        # 调用下游路由（异常兜底）
        try:
            response = await call_next(request)
        except HTTPException as exc:
            response = self._error_response(
                getattr(exc, "status_code", 500), getattr(exc, "detail", "请求失败"),
                getattr(exc, "headers", None),
            )
        except StarletteHTTPException as exc:
            response = self._error_response(
                getattr(exc, "status_code", 500), getattr(exc, "detail", "请求失败"),
                getattr(exc, "headers", None),
            )
        except RequestValidationError as exc:
            try:
                self.logger.debug(f"[{request_id}] 请求参数校验失败: {exc.errors()}")
            except Exception:
                pass
            response = self._error_response(422, "请求参数校验失败")
        except Exception as exc:
            try:
                self.logger.exception(f"[{request_id}] 未捕获异常: {exc}")
            except Exception:
                pass
            response = self._error_response(500, "服务器内部错误")

        return self._finish(response, request, start, request_id, ip)

    # ------------------------------------------------------------------ #
    # 全局异常处理器（路由内抛出的异常由 FastAPI 的 ExceptionMiddleware 交给它们）
    # ------------------------------------------------------------------ #
    async def _handle_http_exception(self, request: Request, exc: StarletteHTTPException):
        request_id = getattr(getattr(request, "state", None), "request_id", "-")
        try:
            self.logger.debug(
                f"[{request_id}] HTTP 异常 {getattr(exc, 'status_code', 500)}: "
                f"{getattr(exc, 'detail', '')}"
            )
        except Exception:
            pass
        return self._error_response(
            getattr(exc, "status_code", 500),
            getattr(exc, "detail", "请求失败"),
            getattr(exc, "headers", None),
        )

    async def _handle_validation_exception(self, request: Request, exc: RequestValidationError):
        request_id = getattr(getattr(request, "state", None), "request_id", "-")
        try:
            self.logger.debug(f"[{request_id}] 请求参数校验失败: {exc.errors()}")
        except Exception:
            pass
        return self._error_response(422, "请求参数校验失败")

    # ------------------------------------------------------------------ #
    # 注册入口
    # ------------------------------------------------------------------ #
    def main(self) -> None:
        """把中间件与异常处理器注册到 value.app（必须在应用启动前调用）"""
        app = getattr(self.value, "app", None)
        if app is None:
            self.logger.error("value.app 未就绪，中间件注册失败")
            return
        if not hasattr(app, "middleware"):
            self.logger.error("value.app 不是 FastAPI 应用实例，中间件注册失败")
            return

        # 应用级可信代理（get_client_ip 会优先读它）
        try:
            app.state.trusted_proxies = self.trusted_proxies
            app.state.base_url = self.base_url
        except Exception as e:
            self.logger.warning(f"写入 app.state 失败: {e}")

        try:
            @app.middleware("http")
            async def _dispatch(request: Request, call_next):
                return await self._dispatch(request, call_next)

            self.logger.debug("HTTP 中间件注册成功（请求上下文/会话/CSRF/安全头/访问日志/异常兜底）")
        except Exception as e:
            self.logger.error(f"HTTP 中间件注册失败: {e}")

        # 统一异常响应：HTTPException（含 FastAPI 子类与 Starlette 基础类）与参数校验
        try:
            app.add_exception_handler(StarletteHTTPException, self._handle_http_exception)
            app.add_exception_handler(HTTPException, self._handle_http_exception)
            self.logger.debug("HTTPException 异常处理器注册成功")
        except Exception as e:
            self.logger.warning(f"HTTPException 异常处理器注册失败: {e}")

        try:
            app.add_exception_handler(RequestValidationError, self._handle_validation_exception)
            self.logger.debug("RequestValidationError 异常处理器注册成功")
        except Exception as e:
            self.logger.warning(f"RequestValidationError 异常处理器注册失败: {e}")
