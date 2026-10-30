# -*- coding:utf-8 -*-
"""缓存包：Redis 优先、内存降级

对外只暴露统一入口 Cache（value.cache）与各实现类，业务代码不需要
直接构造 RedisClient / MemoryCache / SessionStore。
"""
from .redis import RedisClient, RedisError
from .memory import MemoryCache
from .session import SessionStore
from .mains import Cache

__all__ = ["Cache", "RedisClient", "RedisError", "MemoryCache", "SessionStore"]
