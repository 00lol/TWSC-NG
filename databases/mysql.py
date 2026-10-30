# -*- coding:utf-8 -*-
"""MySQL 驱动实现（asyncmy 异步连接池）

- 连接池在事件循环内创建（由 ``DatabaseManager.init()`` 在 lifespan 里调用）；
- 所有查询统一使用 ``DictCursor``，SQL 参数一律 ``%s`` 占位；
- 数据库不存在（MySQL 错误码 1049）时，先用不带 database 的临时连接自动建库，再重连建池。
"""
import re
from typing import Any, List, Optional, Tuple

import asyncmy
from asyncmy.cursors import DictCursor
from asyncmy.pool import Pool

from .base import Database

# 建库时数据库名合法性校验（要拼进 DDL，必须白名单）
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_$]+$")


class MySQL(Database):
    """MySQL 数据库（asyncmy 连接池 + DictCursor）"""

    engine = "mysql"

    def __init__(self, logger, config: dict):
        super().__init__(logger, config)
        # 连接池对象，连接前为 None
        self._pool: Optional[Pool] = None

    # ── 连接池 ──────────────────────────────────────────────────

    async def _create_pool(self) -> Pool:
        """创建 asyncmy 连接池（携带 database，可能因库不存在抛 1049）"""
        return await asyncmy.create_pool(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database or None,
            charset=self.charset or "utf8mb4",
            minsize=self.minsize,
            maxsize=self.maxsize,
            connect_timeout=self.connect_timeout,
            autocommit=True,
        )

    async def _create_database(self) -> bool:
        """数据库不存在时，用不带 database 的临时连接自动建库"""
        if not _DB_NAME_RE.match(self.database or ""):
            self.logger.error(f"数据库名 {self.database!r} 不合法，无法自动创建")
            return False

        conn = None
        try:
            conn = await asyncmy.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                charset=self.charset or "utf8mb4",
                connect_timeout=self.connect_timeout,
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.database}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            self.logger.info(f"数据库 {self.database} 不存在，已自动创建（utf8mb4_unicode_ci）")
            return True
        except Exception as e:
            self.logger.error(f"自动创建数据库 {self.database} 失败: {e}")
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                    await conn.ensure_closed()
                except Exception:
                    pass

    @staticmethod
    def _error_code(error: Exception) -> Optional[int]:
        """从异常里取 MySQL 错误码（如 1049 库不存在 / 1064 语法错误）"""
        args = getattr(error, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
        return None

    def _require_pool(self) -> Pool:
        """取连接池，未初始化则抛出明确的中文错误"""
        if self._pool is None:
            raise RuntimeError(
                f"MySQL 连接池未初始化（{self.host}:{self.port}/{self.database}），请先调用 connect()"
            )
        return self._pool

    async def connect(self) -> bool:
        """建立连接池；库不存在时自动建库后重试。失败只记日志、返回 False，不抛异常。"""
        if self._pool is not None:
            self.logger.debug("MySQL 连接池已存在，直接复用")
            return True

        try:
            self._pool = await self._create_pool()
        except Exception as e:
            error_code = self._error_code(e)
            if error_code == 1049:
                self.logger.warning(f"数据库 {self.database} 不存在，准备自动创建")
                if not await self._create_database():
                    self._connected = False
                    return False
                try:
                    self._pool = await self._create_pool()
                except Exception as retry_error:
                    self.logger.error(f"建库后重连 MySQL 失败: {retry_error}")
                    self._connected = False
                    return False
            else:
                self.logger.error(
                    f"MySQL 连接失败（{self.host}:{self.port} 用户 {self.user}）: {e}"
                )
                self._connected = False
                return False

        # 连接池建好后回显一次版本，确认真的可用
        ok, message = await self.ping()
        if not ok:
            self.logger.error(f"MySQL 连接池心跳检测失败: {message}")
            self._connected = False
            await self.close()
            return False

        self._connected = True
        self.logger.info(
            f"MySQL 连接池初始化成功（{self.host}:{self.port}/{self.database}，"
            f"版本 {message}，连接数 {self.minsize}~{self.maxsize}）"
        )
        return True

    async def close(self) -> None:
        """关闭连接池（pool.close 同步 + wait_closed 异步）"""
        pool = self._pool
        self._pool = None
        self._connected = False
        if pool is None:
            return
        try:
            pool.close()
        except Exception as e:
            self.logger.error(f"关闭 MySQL 连接池失败: {e}")
            return
        try:
            await pool.wait_closed()
            self.logger.info("MySQL 连接池已关闭")
        except Exception as e:
            # 进程退出兜底场景：连接池可能绑定在已结束的事件循环上，属可忽略情况
            self.logger.warning(
                f"等待 MySQL 连接池关闭时出现异常（进程退出场景可忽略）: {e}"
            )

    # ── 基础操作 ────────────────────────────────────────────────

    async def ping(self) -> Tuple[bool, str]:
        """心跳检测，返回 (是否可用, 版本号或错误信息)"""
        if self._pool is None:
            return False, "连接池未初始化"
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute("SELECT VERSION() AS version")
                    row = await cur.fetchone()
            version = (row or {}).get("version") or "未知版本"
            return True, str(version)
        except Exception as e:
            self.logger.error(f"MySQL 心跳检测失败: {e}")
            return False, str(e)

    async def execute(self, sql: str, params: Optional[Any] = None) -> int:
        """执行写操作 / DDL，返回影响行数"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(sql, params)
                return int(cur.rowcount)

    async def execute_many(self, sql: str, params_list: List[Any]) -> int:
        """批量执行写操作，返回影响行数"""
        if not params_list:
            return 0
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.executemany(sql, params_list)
                return int(cur.rowcount)

    async def fetch_one(
        self, sql: str, params: Optional[Any] = None
    ) -> Optional[dict]:
        """查询单行，返回 dict；无结果返回 None"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(sql, params)
                row = await cur.fetchone()
                return dict(row) if row else None

    async def fetch_all(self, sql: str, params: Optional[Any] = None) -> List[dict]:
        """查询多行，返回 dict 列表；无结果返回空列表"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
                return [dict(row) for row in rows]

    async def table_exists(self, table: str) -> bool:
        """判断表是否存在（查 information_schema，参数化）"""
        row = await self.fetch_one(
            "SELECT TABLE_NAME AS table_name FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s LIMIT 1",
            (self.database or None, table),
        )
        return row is not None

    async def table_columns(self, table: str) -> List[str]:
        """获取表字段名列表（按定义顺序）"""
        rows = await self.fetch_all(
            "SELECT COLUMN_NAME AS column_name FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (self.database or None, table),
        )
        return [row["column_name"] for row in rows]
