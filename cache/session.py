# -*- coding:utf-8 -*-
"""服务端会话存储（登录饼干背后的 session）

只依赖缓存对象的 get / set / delete 三个异步方法（expire 可选），因此
cache.redis.RedisClient、cache.memory.MemoryCache、cache.mains.Cache 都能直接传入。
会话内容以 JSON 字符串保存，sid 由 secrets.token_urlsafe 随机生成。
"""
import json
import re
import secrets
from typing import Optional

# 只接受 token_urlsafe 生成的 URL 安全字符，防止外部传入异常 sid 拼出奇怪键名
_SID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class SessionStore:
    """会话存储：create / load / refresh / destroy"""

    def __init__(self, cache, prefix="twsc:session:", ttl=86400):
        # 只组装，不做 I/O
        self.cache = cache
        self.prefix = str(prefix or "")
        self.ttl = self._to_int(ttl, 86400)
        if self.ttl <= 0:
            self.ttl = 86400
        self.logger = getattr(cache, "logger", None)

    def main(self):
        """返回自身，符合项目 main() 约定"""
        return self

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _log(self, level: str, message: str):
        """日志兜底：cache 对象不一定带 logger"""
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

    def _key(self, sid) -> Optional[str]:
        """拼接缓存键；sid 非法时返回 None"""
        if not isinstance(sid, str) or not _SID_PATTERN.match(sid):
            return None
        return f"{self.prefix}{sid}"

    def _seconds(self, ttl) -> int:
        """计算本次会话有效期"""
        seconds = self._to_int(ttl, self.ttl) if ttl is not None else self.ttl
        if seconds <= 0:
            seconds = self.ttl
        return seconds

    # ------------------------------------------------------------------
    # 会话操作
    # ------------------------------------------------------------------
    async def create(self, data: dict, ttl: Optional[int] = None) -> str:
        """创建会话，返回随机 sid；写入失败返回空字符串"""
        sid = secrets.token_urlsafe(32)
        key = f"{self.prefix}{sid}"
        seconds = self._seconds(ttl)
        try:
            payload = json.dumps(data if isinstance(data, dict) else {}, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            self._log("error", f"[会话] 数据序列化失败: {e}")
            return ""
        try:
            ok = await self.cache.set(key, payload, ex=seconds)
        except Exception as e:
            self._log("error", f"[会话] 创建失败: {e}")
            return ""
        if not ok:
            self._log("error", "[会话] 创建失败：缓存写入未成功")
            return ""
        self._log("debug", f"[会话] 创建 {sid[:8]}... 有效期 {seconds}s")
        return sid

    async def load(self, sid: str) -> Optional[dict]:
        """读取会话；sid 不存在/已过期/格式非法返回 None"""
        key = self._key(sid)
        if key is None:
            return None
        try:
            raw = await self.cache.get(key)
        except Exception as e:
            self._log("error", f"[会话] 读取失败: {e}")
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as e:
            self._log("warning", f"[会话] 数据已损坏，忽略: {e}")
            return None
        if not isinstance(data, dict):
            self._log("warning", "[会话] 数据不是字典，忽略")
            return None
        return data

    async def refresh(self, sid: str, ttl: Optional[int] = None) -> bool:
        """续期会话；sid 不存在返回 False"""
        key = self._key(sid)
        if key is None:
            return False
        seconds = self._seconds(ttl)
        # 优先用 EXPIRE 原地续期，避免重复读写
        expire = getattr(self.cache, "expire", None)
        if expire is not None:
            try:
                return bool(await expire(key, seconds))
            except Exception as e:
                self._log("error", f"[会话] 续期失败: {e}")
                return False
        # 缓存对象没有 expire：读出来重新写入以续期
        data = await self.load(sid)
        if data is None:
            return False
        try:
            payload = json.dumps(data, ensure_ascii=False)
            return bool(await self.cache.set(key, payload, ex=seconds))
        except Exception as e:
            self._log("error", f"[会话] 续期失败: {e}")
            return False

    async def destroy(self, sid: str) -> bool:
        """销毁会话；sid 不存在返回 False"""
        key = self._key(sid)
        if key is None:
            return False
        try:
            deleted = await self.cache.delete(key)
        except Exception as e:
            self._log("error", f"[会话] 销毁失败: {e}")
            return False
        return bool(deleted)
