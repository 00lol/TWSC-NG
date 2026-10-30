# -*- coding:utf-8 -*-
"""缓存统一入口（value.cache）

Redis 优先，Redis 不可用时自动降级进程内 MemoryCache；init() 永不抛异常，
保证缓存故障不会让整个服务起不来（README「五、8」）。业务代码只与本类交互，
无需关心底层是 Redis 还是内存。
"""
from typing import Any, Optional

from .redis import RedisClient, RedisError
from .memory import MemoryCache
from .session import SessionStore


class Cache:
    """统一缓存服务：对外接口在 Redis 与内存模式下完全一致"""

    def __init__(self, logger, config: dict):
        # 只做对象组装与配置解析，连网放在 async init()
        self.logger = logger
        self.config = config if isinstance(config, dict) else {}

        # 基础配置（键名见 README「三、配置」redis 段）
        self.enabled = self._to_bool(self.config.get("enabled", True), True)
        self.host = str(self.config.get("host") or "127.0.0.1")
        self.port = self._to_int(self.config.get("port"), 6379)
        self.password = str(self.config.get("password") or "")
        self.db = self._to_int(self.config.get("db"), 0)
        self.maxsize = max(1, self._to_int(self.config.get("maxsize"), 5))
        self.timeout = max(0.1, self._to_float(self.config.get("timeout"), 5.0))
        # 降级后内存缓存的键上限（与 Redis 连接数无关，单独给默认值）
        self.memory_maxsize = 10000

        # 运行期状态
        self.client = None           # RedisClient | MemoryCache | None
        self.mode = "none"           # "redis" | "memory" | "none"
        self.session = None          # SessionStore | None

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_bool(value, default: bool = False) -> bool:
        """兼容 True/False、1/0、"True"/"true"/"on" 等写法"""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on", "y", "t"):
            return True
        if text in ("0", "false", "no", "off", "n", "f", ""):
            return False
        return default

    def _log(self, level: str, message: str):
        """日志兜底：logger 允许为空，且日志异常不能影响业务"""
        logger = self.logger
        if logger is None:
            return
        func = getattr(logger, level, None)
        if func is None:
            return
        try:
            func(message)
        except Exception:
            pass

    def main(self):
        """返回自身，符合项目 main() 约定"""
        return self

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def init(self) -> bool:
        """初始化缓存：Redis 优先，失败降级内存；任何情况都返回 True 且不抛异常"""
        # 允许重复调用（配置热重载）：先关旧客户端
        try:
            await self.close()
        except Exception as e:
            self._log("debug", f"[缓存] 关闭旧客户端时出现异常（已忽略）: {e}")

        if not self.enabled:
            await self._degrade("配置未启用 Redis（enabled=False）")
            return True

        client = None
        try:
            client = RedisClient(
                self.logger,
                host=self.host,
                port=self.port,
                password=self.password,
                db=self.db,
                maxsize=self.maxsize,
                timeout=self.timeout,
            )
            if not await client.connect():
                raise RedisError(f"Redis 连接失败（{self.host}:{self.port}）")
            alive, message = await client.ping()
            if not alive:
                raise RedisError(f"Redis PING 失败: {message}")

            self.client = client
            self.mode = "redis"
            self.session = SessionStore(client)
            self._log("info", f"[缓存] Redis 模式就绪 {self.host}:{self.port} 数据库={self.db}")
            return True
        except Exception as e:
            # 连接/握手失败：关掉半成品客户端，降级内存
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            self.client = None
            self.mode = "none"
            self.session = None
            await self._degrade(f"Redis 不可用，降级内存缓存：{e}")
            return True

    async def _degrade(self, reason: str):
        """降级到进程内内存缓存"""
        try:
            memory = MemoryCache(self.logger, maxsize=self.memory_maxsize)
            await memory.connect()
            self.client = memory
            self.mode = "memory"
            self.session = SessionStore(memory)
            self._log("warning", f"[缓存] {reason}，已降级为进程内内存缓存")
        except Exception as e:
            # 兜底：内存缓存也失败时保持 none，仍然不能让服务起不来
            self.client = None
            self.mode = "none"
            self.session = None
            self._log("error", f"[缓存] 内存缓存初始化失败: {e}")

    async def close(self):
        """关闭底层客户端（重复调用安全）"""
        client = self.client
        self.client = None
        self.session = None
        self.mode = "none"
        if client is None:
            return
        try:
            await client.close()
            self._log("debug", "[缓存] 已关闭")
        except Exception as e:
            self._log("error", f"[缓存] 关闭失败: {e}")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    async def ping(self) -> tuple:
        """心跳检测，返回 (是否成功, 说明)"""
        client = self.client
        if client is None:
            return False, "缓存未初始化"
        try:
            return await client.ping()
        except Exception as e:
            return False, f"缓存异常: {e}"

    # ------------------------------------------------------------------
    # 命令转发
    # ------------------------------------------------------------------
    async def _forward(self, method: str, fallback: Any, *args, **kwargs) -> Any:
        """把命令转发给底层 client；未初始化或执行异常时返回兜底值，绝不抛出"""
        client = self.client
        if client is None:
            self._log("warning", f"[缓存] 客户端未初始化，忽略 {method} 请求")
            return fallback
        try:
            return await getattr(client, method)(*args, **kwargs)
        except Exception as e:
            self._log("error", f"[缓存] 执行 {method} 失败: {e}")
            return fallback

    async def get(self, key: str) -> Optional[str]:
        return await self._forward("get", None, key)

    async def set(self, key: str, value, ex: Optional[int] = None) -> bool:
        return await self._forward("set", False, key, value, ex=ex)

    async def delete(self, *keys) -> int:
        return await self._forward("delete", 0, *keys)

    async def exists(self, key: str) -> int:
        return await self._forward("exists", 0, key)

    async def expire(self, key: str, seconds: int) -> bool:
        return await self._forward("expire", False, key, seconds)

    async def ttl(self, key: str) -> int:
        return await self._forward("ttl", -2, key)

    async def incr(self, key: str) -> int:
        return await self._forward("incr", 0, key)

    async def get_json(self, key: str) -> Optional[dict]:
        return await self._forward("get_json", None, key)

    async def set_json(self, key: str, value, ex: Optional[int] = None) -> bool:
        return await self._forward("set_json", False, key, value, ex=ex)
