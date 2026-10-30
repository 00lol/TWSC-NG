# -*- coding:utf-8 -*-
"""进程内缓存（Redis 不可用时的降级实现）

接口与 cache.redis.RedisClient 保持一致（同样是 async 方法），但数据只存在
当前进程的字典里。单进程部署足够用；多进程/多实例部署时会话不共享，仅作保底。

- 每个键保存 (值, 过期时间戳)，读取时惰性判断过期
- 写入时按批次清理过期键，超过 maxsize 时淘汰最旧的键，避免内存无限增长
"""
import json
import math
import time
from typing import Optional

from .redis import RedisError


class MemoryCache:
    """进程内缓存（与 RedisClient 同套异步接口）"""

    def __init__(self, logger, maxsize=10000):
        # 只组装，不做 I/O
        self.logger = logger
        self.maxsize = max(1, int(maxsize))
        self.mode = "memory"
        self._data = {}              # key -> (值, 过期时间戳 / None)
        self._running = False
        self._ops = 0                # 写操作计数，用于周期性清理过期键

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        """内存缓存只要 connect() 过就算可用"""
        return self._running

    def _log(self, level: str, message: str):
        """日志兜底：logger 允许为空"""
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

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        """启用内存缓存（不会失败）"""
        self._running = True
        self._log("info", f"[内存缓存] 已启用，键上限 {self.maxsize}")
        return True

    async def close(self):
        """清空数据并关闭"""
        self._data.clear()
        self._running = False
        self._log("debug", "[内存缓存] 已清空并关闭")

    async def ping(self) -> tuple:
        """心跳检测，内存实现永远可用"""
        return True, "PONG(内存缓存)"

    # ------------------------------------------------------------------
    # 内部维护
    # ------------------------------------------------------------------
    def _now(self) -> float:
        """单调时钟，避免系统时间被调整导致过期判断异常"""
        return time.monotonic()

    def _get_entry(self, key):
        """读取一条未过期的记录，顺带惰性删除已过期的键"""
        item = self._data.get(key)
        if item is None:
            return None
        expire_at = item[1]
        if expire_at is not None and expire_at <= self._now():
            self._data.pop(key, None)
            return None
        return item

    def _purge_all(self):
        """清理全部过期键"""
        if not self._data:
            return
        now = self._now()
        expired = [key for key, item in self._data.items()
                   if item[1] is not None and item[1] <= now]
        for key in expired:
            self._data.pop(key, None)

    def _evict(self):
        """上限淘汰：先清过期，仍然超限则按写入顺序淘汰最旧的键"""
        self._purge_all()
        while len(self._data) > self.maxsize:
            oldest = next(iter(self._data), None)
            if oldest is None:
                break
            self._data.pop(oldest, None)
            self._log("debug", f"[内存缓存] 超出上限，淘汰最旧键: {oldest}")

    def _maintain(self):
        """写操作后的维护：每 256 次全量清一次过期键，超限则淘汰"""
        self._ops += 1
        if len(self._data) > self.maxsize:
            self._evict()
        elif self._ops % 256 == 0:
            self._purge_all()

    @staticmethod
    def _to_text(value) -> str:
        """统一按字符串保存（与 Redis 的 GET 返回字符串对齐）"""
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # 字符串命令
    # ------------------------------------------------------------------
    async def get(self, key: str) -> Optional[str]:
        item = self._get_entry(key)
        return item[0] if item is not None else None

    async def set(self, key: str, value, ex: Optional[int] = None) -> bool:
        expire_at = None
        if ex is not None:
            seconds = self._to_int(ex, 0)
            if seconds > 0:
                expire_at = self._now() + seconds
        self._data[key] = (self._to_text(value), expire_at)
        self._maintain()
        return True

    async def delete(self, *keys) -> int:
        if not keys:
            return 0
        deleted = 0
        for key in keys:
            if self._data.pop(key, None) is not None:
                deleted += 1
        return deleted

    async def exists(self, key: str) -> int:
        return 1 if self._get_entry(key) is not None else 0

    async def expire(self, key: str, seconds: int) -> bool:
        item = self._get_entry(key)
        if item is None:
            return False
        seconds = self._to_int(seconds, -1)
        if seconds < 0:
            self._log("debug", f"[内存缓存] EXPIRE {key} 的秒数非法: {seconds}")
            return False
        if seconds == 0:
            # 与 Redis 一致：EXPIRE 0 表示立即删除
            self._data.pop(key, None)
            return True
        self._data[key] = (item[0], self._now() + seconds)
        return True

    async def ttl(self, key: str) -> int:
        item = self._get_entry(key)
        if item is None:
            return -2
        expire_at = item[1]
        if expire_at is None:
            return -1
        return max(0, math.ceil(expire_at - self._now()))

    async def incr(self, key: str) -> int:
        item = self._get_entry(key)
        if item is None:
            value = 1
            expire_at = None
        else:
            try:
                value = int(str(item[0]).strip()) + 1
            except (TypeError, ValueError):
                raise RedisError(f"内存缓存 INCR 失败：键 {key} 的值不是整数")
            expire_at = item[1]
        self._data[key] = (str(value), expire_at)
        self._maintain()
        return value

    # ------------------------------------------------------------------
    # JSON 命令
    # ------------------------------------------------------------------
    async def get_json(self, key: str) -> Optional[dict]:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as e:
            self._log("warning", f"[内存缓存] 键 {key} 的 JSON 解析失败: {e}")
            return None
        if not isinstance(value, dict):
            self._log("warning", f"[内存缓存] 键 {key} 的 JSON 不是对象，已忽略")
            return None
        return value

    async def set_json(self, key: str, value, ex: Optional[int] = None) -> bool:
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            self._log("error", f"[内存缓存] 键 {key} 的 JSON 序列化失败: {e}")
            return False
        return await self.set(key, payload, ex=ex)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        """当前键数量（含尚未清理的过期键，仅用于观测）"""
        return len(self._data)

    def clear(self):
        """同步清空（进程退出/测试用）"""
        self._data.clear()
