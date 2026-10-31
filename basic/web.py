#-*- coding: utf-8 -*-
"""Web 应用：FastAPI 组装、lifespan、CORS、静态资源与模板、启动

职责边界：
- 本文件只负责"应用外壳"：创建 FastAPI、注册中间件、CORS、静态/模板、启动 uvicorn；
- 启动/关闭时的业务初始化（数据库、缓存、认证、管理员引导）由 main.py 通过
  value.startup / value.shutdown 注入，在 lifespan 里执行；
- 路由挂载由 transaction/routing.py 负责，main.py 在启动前调用。
"""
import asyncio
import atexit
import mimetypes
import os
import signal
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from socketio import AsyncServer
from socketio.asgi import ASGIApp


class FastAPIWebApp:
    def __init__(self, value):
        self.value = value
        self.app = None
        self.shutdown_event = threading.Event()
        self._closed = False
        # 项目根目录（本文件位于 basic/ 下）
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.template_folder = os.path.join(self.root, "WebResources", "templates")
        self.static_folder = os.path.join(self.root, "WebResources", "static")
        self.server = (value.config or {}).get("server", {}) or {}

        # 初始化 FastAPI 应用
        self.app = FastAPI(
            title="TWSC-NG",
            description="TWSC-NG 后端服务（FastAPI）",
            version="0.0.1",
            # 作者
            contact={
                "author": "SkyMaoZi",
                "copyright": "©Copyright 2026 SkyMaoZi. All rights reserved.",
                "email": "dqj13115716883@163.com",
            },
            lifespan=self._lifespan,
        )
        # 立刻回写全局 value，中间件/路由都从 value.app 取应用实例
        self.value.app = self.app

        # 请求中间件（访问日志、会话装载、CSRF、异常兜底）先注册
        self._setup_middleware()

        # 再挂 CORS，保证中间件返回的错误响应也带跨域头
        self.setting()

        # 挂载静态文件目录（骨架阶段可能没有 WebResources，存在才挂）
        if os.path.isdir(self.static_folder):
            self.app.mount("/static", StaticFiles(directory=self.static_folder), name="static")
            self.value.logger.debug(f"静态路径:{self.static_folder}")
        else:
            self.value.logger.debug(f"静态目录不存在，跳过挂载:{self.static_folder}")

        # 初始化模板引擎（当前阶段不写后台界面，存在才启用）
        if os.path.isdir(self.template_folder):
            self.value.templates = Jinja2Templates(directory=self.template_folder)
            self.value.logger.debug(f"模板路径:{self.template_folder}")

        # 初始化 SocketIO（异步版本）—— CORS 与 HTTP 保持一致
        origins = self.server.get("cors") or ["*"]
        self.socketio = AsyncServer(
            async_mode="asgi",
            cors_allowed_origins="*" if "*" in origins else origins,
        )
        self.sio_app = ASGIApp(self.socketio, self.app)
        self.value.sio_app = self.sio_app
        self.value.socketio = self.socketio  # 保存到 value 对象中供其他模块使用

        self._register_shutdown_hooks()

    # 应用生命周期：执行 main.py 注入的启动/关闭钩子
    @asynccontextmanager
    async def _lifespan(self, app):
        logger = self.value.logger
        try:
            if callable(self.value.startup):
                await self.value.startup()
        except Exception as e:
            logger.error(f"服务启动钩子执行失败: {e}")
        try:
            yield
        finally:
            try:
                if callable(self.value.shutdown):
                    await self.value.shutdown()
            except Exception as e:
                logger.error(f"服务关闭钩子执行失败: {e}")

    # 请求中间件
    def _setup_middleware(self):
        try:
            from transaction.middleware import Middleware
            Middleware(self.value).main()
            self.value.logger.debug("请求中间件已注册")
        except Exception as e:
            # 中间件注册失败不阻断启动，但认证/CSRF 会失效，必须显式报出来
            self.value.logger.error(f"请求中间件注册失败: {e}")

    def _default_cors_origins(self):
        """未配置跨域来源时的默认值：本机同源"""
        server_cfg = (self.value.config or {}).get("server", {}) or {}
        host = server_cfg.get("ip", "127.0.0.1")
        port = server_cfg.get("port", 8000)
        scheme = "https" if server_cfg.get("ssl", False) else "http"

        origins = {
            f"{scheme}://127.0.0.1:{port}",
            f"{scheme}://localhost:{port}",
        }
        if host not in ("0.0.0.0", "127.0.0.1", "localhost"):
            origins.add(f"{scheme}://{host}:{port}")
        return sorted(origins)

    def _register_shutdown_hooks(self):
        """注册关闭钩子（uvicorn 正常退出时会走 lifespan，这里是兜底）"""

        def signal_handler(signum, frame):
            self.value.logger.info(f"WebApp接收到关闭信号 {signum}")
            self._graceful_shutdown()

        # 只在主线程注册信号，避免多线程/子进程环境报错
        if threading.current_thread() is threading.main_thread():
            for sig_name in ("SIGINT", "SIGTERM"):
                sig = getattr(signal, sig_name, None)
                if sig is None:
                    continue
                try:
                    signal.signal(sig, signal_handler)
                except (ValueError, OSError) as e:
                    self.value.logger.debug(f"注册信号 {sig_name} 失败: {e}")
        atexit.register(self._graceful_shutdown)

    def _graceful_shutdown(self):
        """优雅关闭：尽量释放数据库连接池与缓存连接"""
        if self._closed:
            return
        self._closed = True
        self.shutdown_event.set()
        logger = self.value.logger
        logger.info("开始优雅关闭服务器...")
        # 数据库
        database = getattr(self.value, "database", None)
        if database is not None and hasattr(database, "close_pool"):
            try:
                database.close_pool()
                logger.info("数据库连接池已关闭")
            except Exception as e:
                logger.error(f"关闭数据库连接池失败: {e}")
        # 缓存
        cache = getattr(self.value, "cache", None)
        if cache is not None and hasattr(cache, "close"):
            try:
                result = cache.close()
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop is not None and loop.is_running():
                        loop.create_task(result)
                    else:
                        asyncio.run(result)
                logger.info("缓存连接已关闭")
            except Exception as e:
                logger.error(f"关闭缓存连接失败: {e}")
        # 运行时长
        if getattr(self.value, "start_time", None):
            run_time = time.time() - self.value.start_time
            logger.info(f"服务器总运行时间: {run_time:.2f} 秒")
        logger.info("服务器优雅关闭完成")

    # 配置服务器
    def setting(self):
        # 修复 JavaScript MIME 类型识别
        mimetypes.add_type("application/javascript", ".js")

        # CORS 配置 —— 不允许 "*" 同时携带 credentials（规范禁止的组合）
        allow_origins = self.server.get("cors") or self._default_cors_origins()
        if allow_origins and "*" not in allow_origins:
            # 显式配置了具体来源时允许凭据（饼干登录需要）
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=allow_origins,
                allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                allow_headers=["*"],
            )
        else:
            # 默认严格策略：不允许跨源带凭据
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"] if not allow_origins else allow_origins,
                allow_credentials=False,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type"],
            )

    # 初始化服务器
    def Init(self):
        # 返回到第三方应用
        return self.app
        # return self.sio_app # 需要 WebSocket 时换成这一行

    # 启动服务器
    def run(self, times=0, server_type="uvicorn"):
        logger = self.value.logger
        # 延迟开启
        for i in list(range(times + 1))[::-1]:
            logger.info(f"服务器将在{i}秒后开启")
            time.sleep(1)

        if server_type != "uvicorn":
            logger.error("未知的服务器类型，FastAPI 推荐使用 uvicorn")
            raise SystemExit(1)
        logger.info("使用Uvicorn服务器启动")

        server_cfg = self.server or {}
        uvicorn_config = {
            "host": server_cfg.get("ip", "127.0.0.1"),
            "port": int(server_cfg.get("port", 8000)),
            "log_level": "debug" if server_cfg.get("debug", False) else "info",
            "access_log": bool(server_cfg.get("debug", False)),
        }

        # 检查是否启用 SSL
        if server_cfg.get("ssl", False):
            ssl_cert = server_cfg.get("ssl_cert", "./key/cert.pem")
            ssl_key = server_cfg.get("ssl_key", "./key/key.pem")
            if os.path.exists(ssl_cert) and os.path.exists(ssl_key):
                uvicorn_config["ssl_certfile"] = ssl_cert
                uvicorn_config["ssl_keyfile"] = ssl_key
                logger.info("SSL证书已配置，启用HTTPS")
            else:
                logger.warning(f"SSL证书文件不存在: {ssl_cert} 或 {ssl_key}，将以HTTP模式启动")
        else:
            logger.info("未配置SSL证书，将以HTTP模式启动")

        if server_cfg.get("debug", False):
            # 热重载要求 uvicorn 拿到导入字符串（会新开子进程重新执行 main.py 初始化）
            logger.info("调试模式：启用热重载（uvicorn 导入字符串 main:app）")
            uvicorn.run(
                "main:app",
                reload=True,
                reload_dirs=[self.root],
                **uvicorn_config,
            )
        else:
            # 生产模式：直接使用应用实例，避免重复初始化
            uvicorn.run(self.app, **uvicorn_config)
