# -*- coding:utf-8 -*-
"""自研异步 Redis 客户端（RESP2 协议，无第三方依赖）

TWSC-NG 只需要缓存/会话用到的一小部分命令，因此不引入 redis 库，
直接用 asyncio.open_connection 实现 RESP2：

    +简单字符串   -错误   :整数   $批量字符串   *数组

连接池由 asyncio.Queue 保管空闲连接，最多 maxsize 条；连接被 Redis 关闭
或出现网络异常时丢弃该连接并自动重连重试一次；所有网络等待都用
asyncio.wait_for 包住，避免 Redis 不可用时整个服务长时间挂起。
"""
import asyncio
import json
from typing import Any, Optional


class RedisError(Exception):
    """Redis 客户端异常：命令错误、连接失败、协议错误"""


class _ConnectionError(RedisError):
    """内部使用：连接级异常（网络中断 / 超时 / 被服务端关闭），需要丢弃连接并重试"""


class _Connection:
    """单条 Redis 连接（reader / writer 对）"""

    __slots__ = ("reader", "writer")

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def close(self):
        """关闭连接，关闭过程中的异常一律忽略"""
        try:
            self.writer.close()
        except Exception:
            pass
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


def _encode_command(args) -> bytes:
    """把命令编码为 RESP2 数组：*N\\r\\n$长度\\r\\n参数\\r\\n..."""
    if not args:
        raise RedisError("Redis 命令不能为空")
    chunks = [b"*%d\r\n" % len(args)]
    for arg in args:
        if isinstance(arg, bytes):
            data = arg
        elif isinstance(arg, bool):
            data = b"1" if arg else b"0"
        elif isinstance(arg, str):
            data = arg.encode("utf-8")
        elif isinstance(arg, (int, float)):
            data = str(arg).encode("utf-8")
        else:
            data = str(arg).encode("utf-8")
        chunks.append(b"$%d\r\n" % len(data))
        chunks.append(data)
        chunks.append(b"\r\n")
    return b"".join(chunks)


def _to_int(value, default: int = 0) -> int:
    """安全的 int 转换（配置/参数可能来自字符串）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    """安全的 float 转换"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class RedisClient:
    """异步 Redis 客户端（RESP2，自研）

    用法：
        client = RedisClient(logger, host="127.0.0.1", port=6379, password="", db=0)
        if await client.connect():
            await client.set("name", "twsc", ex=60)
            value = await client.get("name")
        await client.close()
    """

    def __init__(self, logger, host="127.0.0.1", port=6379, password="", db=0,
                 maxsize=5, timeout=5):
        # 只做参数组装与校验，真正连网放在 async connect()
        self.logger = logger
        self.host = str(host or "127.0.0.1")
        self.port = _to_int(port, 6379)
        self.password = str(password or "")
        self.db = _to_int(db, 0)
        self.maxsize = max(1, _to_int(maxsize, 5))
        self.timeout = max(0.1, _to_float(timeout, 5.0))
        self.last_error = ""

        # 连接池相关（在事件循环里由 connect() 真正创建）
        self._pool: Optional[asyncio.Queue] = None
        self._lock: Optional[asyncio.Lock] = None
        self._created = 0            # 已创建且存活的连接数（空闲 + 使用中）
        self._conns = set()          # 全部存活连接，close() 时兜底关闭
        self._connected = False

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        """当前是否处于已连接（连接池可用）状态"""
        return bool(self._connected and self._pool is not None)

    def _log(self, level: str, message: str):
        """日志兜底：logger 允许为空，避免日志本身把业务打断"""
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
    # 连接管理
    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        """创建连接池并完成 AUTH / SELECT / PING 握手，失败返回 False"""
        # 允许重复调用（配置热重载）：先关掉旧连接
        await self.close()
        self.last_error = ""
        self._pool = asyncio.Queue(maxsize=self.maxsize)
        self._lock = asyncio.Lock()
        self._created = 0
        self._conns = set()
        try:
            conn = await self._create_connection()
            self._conns.add(conn)
            self._created += 1
            self._pool.put_nowait(conn)
            self._connected = True
            self._log("info", f"[Redis] 连接成功 {self.host}:{self.port} 数据库={self.db} 最大连接数={self.maxsize}")
            return True
        except Exception as e:
            self.last_error = str(e)
            self._connected = False
            self._log("error", f"[Redis] 连接失败 {self.host}:{self.port} | {e}")
            await self._close_all()
            self._pool = None
            self._lock = None
            return False

    async def close(self):
        """关闭所有连接并清空连接池（可重复调用）"""
        self._connected = False
        self._pool = None
        self._lock = None
        await self._close_all()

    async def _close_all(self):
        """关闭并清空全部存活连接"""
        conns = list(self._conns)
        self._conns.clear()
        self._created = 0
        for conn in conns:
            await conn.close()
        if conns:
            self._log("debug", f"[Redis] 已关闭 {len(conns)} 条连接")

    async def _create_connection(self) -> _Connection:
        """建立一条 TCP 连接并完成握手"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            raise _ConnectionError(f"连接 {self.host}:{self.port} 超时({self.timeout}s)")
        except OSError as e:
            raise _ConnectionError(f"连接 {self.host}:{self.port} 失败: {e}")

        conn = _Connection(reader, writer)
        try:
            # 有密码先认证
            if self.password:
                await self._command(conn, "AUTH", self.password)
            # 选择数据库（0 为默认库，不必发送）
            if self.db:
                await self._command(conn, "SELECT", self.db)
            # 心跳握手，确认对方真的是可用的 Redis
            reply = await self._command(conn, "PING")
            if str(reply).upper() != "PONG":
                raise _ConnectionError(f"PING 握手异常: {reply!r}")
        except Exception:
            await conn.close()
            raise
        return conn

    # ------------------------------------------------------------------
    # 连接池
    # ------------------------------------------------------------------
    async def _acquire(self) -> _Connection:
        """取一条连接：优先空闲连接，不足上限则新建，满了就排队等待"""
        if self._pool is None:
            raise _ConnectionError("Redis 连接池未初始化，请先调用 connect()")
        try:
            return self._pool.get_nowait()
        except asyncio.QueueEmpty:
            pass

        if self._created < self.maxsize:
            lock = self._lock
            if lock is None:
                raise _ConnectionError("Redis 连接池已关闭")
            async with lock:
                if self._created < self.maxsize:
                    conn = await self._create_connection()
                    self._conns.add(conn)
                    self._created += 1
                    return conn

        try:
            return await asyncio.wait_for(self._pool.get(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise _ConnectionError(f"等待空闲连接超时({self.timeout}s)")

    async def _release(self, conn: _Connection):
        """归还连接（连接池已关闭时直接丢弃）"""
        pool = self._pool
        if pool is None:
            await self._discard(conn)
            return
        try:
            pool.put_nowait(conn)
        except asyncio.QueueFull:
            # 理论上不会发生：空闲连接数不会超过上限
            await self._discard(conn)

    async def _discard(self, conn: _Connection):
        """丢弃一条不可信连接"""
        self._conns.discard(conn)
        self._created = max(0, self._created - 1)
        await conn.close()

    # ------------------------------------------------------------------
    # 协议读写
    # ------------------------------------------------------------------
    async def _send_and_recv(self, conn: _Connection, payload: bytes) -> Any:
        """发送已编码命令并读取一条回复"""
        try:
            conn.writer.write(payload)
            await asyncio.wait_for(conn.writer.drain(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise _ConnectionError("发送命令超时")
        except (ConnectionError, OSError) as e:
            raise _ConnectionError(f"发送命令失败: {e}")
        return await self._read_reply(conn.reader)

    async def _command(self, conn: _Connection, *args) -> Any:
        """在指定连接上执行一条命令"""
        return await self._send_and_recv(conn, _encode_command(args))

    async def _read_reply(self, reader) -> Any:
        """按 RESP2 解析一条回复

        返回：
            +简单字符串 -> str
            :整数       -> int
            $批量字符串 -> str / None（空值）
            *数组       -> list / None（空数组）
            -错误       -> 抛 RedisError
        """
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise _ConnectionError("读取回复超时")
        except asyncio.IncompleteReadError:
            raise _ConnectionError("连接已被 Redis 关闭")
        except (ConnectionError, OSError) as e:
            raise _ConnectionError(f"读取回复失败: {e}")

        if not line.endswith(b"\r\n") or len(line) < 3:
            raise _ConnectionError(f"协议错误：回复行异常 {line!r}")
        line = line[:-2]
        prefix = line[:1]
        body = line[1:]

        if prefix == b"+":
            return body.decode("utf-8", "replace")
        if prefix == b"-":
            # 服务端错误回复：连接仍然可用，交给调用方处理
            raise RedisError(body.decode("utf-8", "replace"))
        if prefix == b":":
            try:
                return int(body)
            except ValueError:
                raise _ConnectionError(f"协议错误：整数回复异常 {line!r}")
        if prefix == b"$":
            try:
                length = int(body)
            except ValueError:
                raise _ConnectionError(f"协议错误：批量字符串长度异常 {line!r}")
            if length < 0:
                return None
            try:
                data = await asyncio.wait_for(
                    reader.readexactly(length + 2), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                raise _ConnectionError("读取批量字符串超时")
            except asyncio.IncompleteReadError:
                raise _ConnectionError("连接已被 Redis 关闭")
            except (ConnectionError, OSError) as e:
                raise _ConnectionError(f"读取批量字符串失败: {e}")
            return data[:length].decode("utf-8", "replace")
        if prefix == b"*":
            try:
                length = int(body)
            except ValueError:
                raise _ConnectionError(f"协议错误：数组长度异常 {line!r}")
            if length < 0:
                return None
            return [await self._read_reply(reader) for _ in range(length)]

        raise _ConnectionError(f"未知的 RESP 类型 {prefix!r}")

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------
    async def execute(self, *args) -> Any:
        """执行原始命令并返回解析后的结果（错误回复抛 RedisError）

        连接级异常（被服务端关闭 / 网络中断 / 超时）会自动丢弃连接并重试一次。
        """
        if not args:
            raise RedisError("Redis 命令不能为空")
        # 先编码：参数非法时不必占用连接
        payload = _encode_command(args)

        last_error = "连接不可用"
        for attempt in range(2):
            conn = None
            try:
                conn = await self._acquire()
                reply = await self._send_and_recv(conn, payload)
            except _ConnectionError as e:
                last_error = str(e)
                if conn is not None:
                    await self._discard(conn)
                self._connected = False
                self.last_error = last_error
                if attempt == 0:
                    self._log("debug", f"[Redis] 连接异常，丢弃后自动重连重试: {e}")
                continue
            except RedisError:
                # 服务端错误回复：连接完好，归还后原样抛给调用方
                if conn is not None:
                    await self._release(conn)
                raise
            else:
                await self._release(conn)
                self._connected = True
                self.last_error = ""
                return reply

        raise RedisError(f"Redis 命令执行失败（已重试）: {last_error}")

    async def ping(self) -> tuple:
        """心跳检测，返回 (是否成功, 说明)"""
        try:
            reply = await self.execute("PING")
        except Exception as e:
            self._connected = False
            self.last_error = str(e)
            self._log("debug", f"[Redis] PING 失败: {e}")
            return False, str(e)
        self._connected = True
        return True, str(reply)

    # ------------------------------------------------------------------
    # 常用命令封装（字符串）
    # ------------------------------------------------------------------
    async def get(self, key: str) -> Optional[str]:
        """GET：键不存在返回 None"""
        reply = await self.execute("GET", key)
        return reply if isinstance(reply, str) else None

    async def set(self, key: str, value, ex: Optional[int] = None) -> bool:
        """SET [EX 秒]：成功返回 True"""
        args = ["SET", key, value]
        if ex is not None:
            seconds = _to_int(ex, 0)
            if seconds > 0:
                args.extend(["EX", seconds])
            else:
                self._log("debug", f"[Redis] SET {key} 的 ex={ex!r} 非法，按永久键处理")
        reply = await self.execute(*args)
        return reply == "OK"

    async def delete(self, *keys) -> int:
        """DEL：返回实际删除的键数量"""
        if not keys:
            return 0
        reply = await self.execute("DEL", *keys)
        return _to_int(reply, 0)

    async def exists(self, key: str) -> int:
        """EXISTS：存在返回 1，否则 0"""
        reply = await self.execute("EXISTS", key)
        return _to_int(reply, 0)

    async def expire(self, key: str, seconds: int) -> bool:
        """EXPIRE：设置成功返回 True"""
        seconds = _to_int(seconds, -1)
        if seconds < 0:
            self._log("debug", f"[Redis] EXPIRE {key} 的秒数非法: {seconds}")
            return False
        reply = await self.execute("EXPIRE", key, seconds)
        return _to_int(reply, 0) == 1

    async def ttl(self, key: str) -> int:
        """TTL：-2 键不存在，-1 无过期时间，其余为剩余秒数"""
        reply = await self.execute("TTL", key)
        return _to_int(reply, -2)

    async def incr(self, key: str) -> int:
        """INCR：返回自增后的整数（值不是整数时抛 RedisError）"""
        reply = await self.execute("INCR", key)
        return _to_int(reply, 0)

    # ------------------------------------------------------------------
    # 常用命令封装（JSON）
    # ------------------------------------------------------------------
    async def get_json(self, key: str) -> Optional[dict]:
        """读取并反序列化 JSON 对象，键不存在/格式不对返回 None"""
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as e:
            self._log("warning", f"[Redis] 键 {key} 的 JSON 解析失败: {e}")
            return None
        if not isinstance(value, dict):
            self._log("warning", f"[Redis] 键 {key} 的 JSON 不是对象，已忽略")
            return None
        return value

    async def set_json(self, key: str, value, ex: Optional[int] = None) -> bool:
        """序列化为 JSON 字符串后写入"""
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            self._log("error", f"[Redis] 键 {key} 的 JSON 序列化失败: {e}")
            return False
        return await self.set(key, payload, ex=ex)
