# -*- coding:utf-8 -*-
"""数据库抽象基类

设计约定：
- ``__init__`` 只保存配置（host/port/user/password/database/charset/maxsize/minsize），不做任何 IO；
- 真正的连接、建表等 IO 全部放在 ``async`` 方法里（连接池绑定事件循环）；
- 子类（如 ``mysql.MySQL``）只实现"原语"（connect/close/execute/fetch...），
  "建表 / 补字段"这类与引擎无关的通用逻辑放在基类复用，只增不删不改。
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

# 表名 / 字段名合法性校验：只允许字母、数字、下划线，且不能以数字开头
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Database:
    """数据库抽象基类（子类按引擎覆盖抽象方法）"""

    #: 引擎标识，子类覆盖（例如 mysql）
    engine: str = "base"

    def __init__(self, logger, config: dict):
        # 日志允许调用方不传，退化为标准库 logger，避免 None 打爆日志
        self.logger = logger or logging.getLogger(f"database.{self.engine}")
        # 只保存配置，不做 IO
        self.config: Dict[str, Any] = dict(config or {})
        self.host: str = str(self.config.get("host") or "127.0.0.1")
        self.port: int = self._as_int(self.config.get("port"), 3306)
        self.user: str = str(self.config.get("user") or "root")
        self.password: str = str(self.config.get("password") or "")
        self.database: str = str(self.config.get("database") or "")
        self.charset: str = str(self.config.get("charset") or "utf8mb4")
        self.maxsize: int = self._as_int(self.config.get("maxsize"), 10)
        self.minsize: int = self._as_int(self.config.get("minsize"), 2)
        # 连接超时（秒），非契约字段，允许配置覆盖
        self.connect_timeout: int = self._as_int(self.config.get("connect_timeout"), 10)
        # 连接状态由子类维护
        self._connected: bool = False

    # ── 内部小工具 ──────────────────────────────────────────────

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        """把配置里的字符串端口 / 连接数安全转成 int"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _check_identifier(self, name: Any, kind: str = "标识符") -> str:
        """校验表名 / 字段名，防止 DDL 拼接被注入"""
        text = str(name or "")
        if not _IDENTIFIER_RE.match(text):
            raise ValueError(f"非法{kind}: {name!r}（只允许字母、数字、下划线）")
        return text

    # ── 生命周期 ────────────────────────────────────────────────

    async def connect(self) -> bool:
        """建立连接（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 connect()")

    async def close(self) -> None:
        """关闭连接 / 连接池（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 close()")

    def close_pool(self) -> None:
        """关闭连接池的同步兜底（进程退出时用）

        - 当前没有运行中的事件循环：直接 ``asyncio.run`` 关闭（带超时，避免退出卡死）；
        - 有运行中的事件循环：不允许阻塞循环，改为调度后台任务并记日志。
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is None:
            try:
                asyncio.run(self._close_guarded())
                self.logger.debug("数据库连接池已通过同步兜底关闭")
            except Exception as e:
                self.logger.error(f"同步关闭数据库连接池失败: {e}")
            return

        try:
            running.create_task(self.close())
            self.logger.debug("检测到运行中的事件循环，数据库连接池已提交后台任务关闭")
        except Exception as e:
            self.logger.error(f"调度数据库连接池关闭任务失败: {e}")

    async def _close_guarded(self, timeout: float = 5.0) -> None:
        """带超时的关闭（连接池可能绑定在已结束的事件循环上，避免无限等待）"""
        try:
            await asyncio.wait_for(self.close(), timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.warning(f"关闭数据库连接池超时（{timeout}s），已跳过等待")

    # ── 基础操作（抽象）──────────────────────────────────────────

    async def ping(self) -> Tuple[bool, str]:
        """心跳检测，返回 (是否可用, 版本号或错误信息)（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 ping()")

    async def execute(self, sql: str, params: Optional[Any] = None) -> int:
        """执行写操作 / DDL，返回影响行数（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 execute()")

    async def execute_many(self, sql: str, params_list: List[Any]) -> int:
        """批量执行写操作，返回影响行数（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 execute_many()")

    async def fetch_one(self, sql: str, params: Optional[Any] = None) -> Optional[dict]:
        """查询单行（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 fetch_one()")

    async def fetch_all(self, sql: str, params: Optional[Any] = None) -> List[dict]:
        """查询多行（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 fetch_all()")

    async def table_exists(self, table: str) -> bool:
        """判断表是否存在（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 table_exists()")

    async def table_columns(self, table: str) -> List[str]:
        """获取表字段名列表（子类实现）"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 table_columns()")

    # ── 通用逻辑（与引擎无关，基于上面原语实现）──────────────────

    async def ensure_tables(self, table_defs: dict) -> None:
        """确保表存在，并补齐缺失字段（只增不删不改）

        Args:
            table_defs: 形如 ``{"users": [("user_id", "INT AUTO_INCREMENT PRIMARY KEY"), ...]}``

        行为：
        - 表不存在：``CREATE TABLE ... ENGINE=InnoDB DEFAULT CHARSET=utf8mb4``
        - 表已存在：``DESCRIBE`` 现有字段，缺哪个就 ``ALTER TABLE ... ADD COLUMN`` 补哪个；
          已有字段的类型 / 默认值 / 索引一律不动。
        - 单张表失败只记错误日志并继续后续表，不打断服务启动。
        """
        if not table_defs:
            self.logger.debug("ensure_tables 未提供表定义，跳过")
            return

        for table_name, columns in table_defs.items():
            try:
                table = self._check_identifier(table_name, "表名")
                if not columns:
                    self.logger.warning(f"表 {table} 未提供字段定义，跳过")
                    continue

                if await self.table_exists(table):
                    existing = await self.table_columns(table)
                    self.logger.debug(f"表 {table} 已存在，字段数 {len(existing)}，开始检查缺失字段")
                    for column_name, column_def in columns:
                        col = self._check_identifier(column_name, "字段名")
                        if col in existing:
                            continue
                        await self.execute(
                            f"ALTER TABLE `{table}` ADD COLUMN `{col}` {column_def}"
                        )
                        self.logger.info(f"表 {table} 补充缺失字段 {col} 成功")
                    continue

                # 表不存在：整表创建
                column_sql = ", ".join(
                    f"`{self._check_identifier(col, '字段名')}` {definition}"
                    for col, definition in columns
                )
                await self.execute(
                    f"CREATE TABLE `{table}` ({column_sql}) "
                    f"ENGINE=InnoDB DEFAULT CHARSET={self.charset}"
                )
                self.logger.info(f"创建表 {table} 成功（共 {len(columns)} 个字段）")
            except Exception as e:
                self.logger.error(f"确保表 {table_name} 存在时失败: {e}")

    # ── 状态 ────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """当前是否已连接"""
        return bool(self._connected)
