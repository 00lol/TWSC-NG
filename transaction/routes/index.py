# -*- coding:utf-8 -*-
"""引导路由：服务信息与健康检查（无认证）"""
from typing import Any

from fastapi import APIRouter

from . import get_base_url


class Index:
    """GET /      服务信息
       GET /health 数据库 / 缓存状态
    """

    def __init__(self, value):
        if value is None:
            raise ValueError("Index 路由初始化失败：value 为空")
        self.value = value
        self.logger = getattr(value, "logger", None)
        if self.logger is None:
            raise ValueError("Index 路由初始化失败：value.logger 未初始化")
        self.base_url = get_base_url(value)
        self.router = APIRouter(prefix=self.base_url, tags=["引导路由"])
        self._route()

    # ------------------------------------------------------------------ #
    # 配置 / 依赖探测
    # ------------------------------------------------------------------ #
    def _server_config(self) -> dict:
        config = getattr(self.value, "config", None)
        if isinstance(config, dict):
            server = config.get("server")
            if isinstance(server, dict):
                return server
            legacy = config.get("Sever")
            if isinstance(legacy, dict):
                return legacy
        return {}

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

    async def _check_database(self) -> dict:
        """数据库状态：disabled（未启用）/ down（未连接或探测失败）/ ok"""
        database = getattr(self.value, "database", None)
        if database is None:
            return {"status": "disabled", "detail": "数据库未启用"}
        if not getattr(database, "connected", False):
            return {"status": "down", "detail": "数据库未连接"}
        try:
            result = await database.ping()
        except Exception as e:
            return {"status": "down", "detail": f"数据库探测异常: {e}"}
        if isinstance(result, tuple) and len(result) >= 2:
            ok, detail = bool(result[0]), str(result[1])
        elif isinstance(result, bool):
            ok, detail = result, ("数据库连接正常" if result else "数据库连接异常")
        else:
            ok, detail = True, str(result or "数据库连接正常")
        return {"status": "ok" if ok else "down", "detail": detail}

    async def _check_cache(self) -> dict:
        """缓存状态：disabled（未启用）/ down（探测失败）/ ok"""
        cache = getattr(self.value, "cache", None)
        if cache is None:
            return {"status": "disabled", "detail": "缓存未启用"}
        try:
            result = await cache.ping()
        except Exception as e:
            return {
                "status": "down",
                "detail": f"缓存探测异常: {e}",
                "mode": str(getattr(cache, "mode", "") or "unknown"),
            }
        if isinstance(result, tuple) and len(result) >= 2:
            ok, detail = bool(result[0]), str(result[1])
        elif isinstance(result, bool):
            ok, detail = result, ("缓存连接正常" if result else "缓存连接异常")
        else:
            ok, detail = True, str(result or "缓存连接正常")
        return {
            "status": "ok" if ok else "down",
            "detail": detail,
            "mode": str(getattr(cache, "mode", "") or "unknown"),
        }

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def _route(self) -> None:
        @self.router.get("/", summary="服务信息")
        async def index():
            server = self._server_config()
            return {
                "success": True,
                "message": "TWSC-NG 服务运行中",
                "data": {
                    "name": "TWSC-NG",
                    "version": "0.0.1",
                    "debug": self._as_bool(server.get("debug", False), False),
                },
            }

        @self.router.get("/health", summary="健康检查")
        async def health():
            """健康检查永远返回 HTTP 200，依赖不可用时在 detail 中说明"""
            database = await self._check_database()
            cache = await self._check_cache()
            cache_mode = cache.get("mode") or str(getattr(getattr(self.value, "cache", None), "mode", "") or "none")
            healthy = database.get("status") in {"ok", "disabled"} and cache.get("status") in {"ok", "disabled"}
            return {
                "success": True,
                "message": "服务运行正常" if healthy else "服务运行中，部分依赖不可用",
                "data": {
                    "database": database,
                    "cache": cache,
                    "mode": str(cache_mode),
                },
            }
