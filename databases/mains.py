# -*- coding:utf-8 -*-
"""数据库管理器

职责：读 ``value.config["db"]`` -> 按 engine 注册表选驱动 -> 连接 -> 建表/补字段
      -> 扫描 ``plugin/database_business`` 加载插件 -> 写入 ``value.plugin.database_business``。

``__init__`` 只做组装（不连网），真正的连接放在 ``async init()``，由 web 的 lifespan
在 uvicorn 事件循环里调用（asyncmy 连接池必须绑定该循环）。
任何失败都只记错误日志并返回 False，绝不让整个服务启不来。
"""
import logging
import os
import types
from typing import Any, Dict, List, Optional

from .mysql import MySQL
from .plugin import DatabasePlugin, PluginManager, get_plugin_manager
from .user import User

# 引擎注册表：新增引擎（MariaDB 等）时在这里登记即可
ENGINES: Dict[str, type] = {
    "mysql": MySQL,
}

# 业务插件目录：<项目根>/plugin/database_business
PLUGIN_DIR: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugin",
    "database_business",
)


class DatabaseManager:
    """数据库统一入口（value.database）"""

    def __init__(self, value):
        self.value = value
        self.logger = getattr(value, "logger", None) or logging.getLogger("database")

        # 读配置：只取 db 段，缺失时给安全默认值
        config: Dict[str, Any] = {}
        try:
            raw_config = getattr(value, "config", None)
            if isinstance(raw_config, dict):
                config = dict(raw_config.get("db") or {})
        except Exception as e:
            self.logger.warning(f"读取数据库配置失败，使用默认值: {e}")
        self.config = config

        # 选引擎
        engine = str(config.get("engine") or "mysql").strip().lower()
        driver_class = ENGINES.get(engine)
        if driver_class is None:
            self.logger.warning(f"未支持的数据库引擎 {engine!r}，回退到 mysql")
            engine = "mysql"
            driver_class = ENGINES[engine]
        self.engine = engine

        # 组装驱动 / 用户表操作 / 插件体系（全部不连网）
        self.driver = driver_class(self.logger, config)
        self.user = User(self.driver, self.logger)
        self.plugins: Dict[str, DatabasePlugin] = {}
        self.plugin_manager: PluginManager = get_plugin_manager(
            self.logger, "database_business"
        )
        self._initialized = False

    # ── 生命周期 ────────────────────────────────────────────────

    async def init(self) -> bool:
        """连接 -> 建表 -> 加载插件；失败不抛异常，返回 False"""
        # 1. 连接（驱动内部已 catch，失败返回 False）
        try:
            connected = await self.driver.connect()
        except Exception as e:  # 双保险，驱动理论上不会抛
            self.logger.error(f"数据库连接异常: {e}")
            connected = False

        if not connected:
            self.logger.error(
                "数据库初始化失败：服务将以无数据库模式继续运行（依赖数据库的接口返回 503）"
            )
            self._initialized = False
            return False

        # 2. 建表 / 补字段
        try:
            await self.user.ensure_tables()
        except Exception as e:
            self.logger.error(f"数据库建表 / 补字段失败: {e}")

        # 3. 加载业务插件并初始化
        try:
            self.plugin_manager.load_plugins_from_dir(PLUGIN_DIR)
            await self.plugin_manager.initialize_all(self.value)
            self.plugins = dict(self.plugin_manager.plugins)
        except Exception as e:
            self.logger.error(f"加载数据库业务插件失败: {e}")

        # 4. 回写 value.plugin.database_business
        self._attach_plugins_to_value()

        self._initialized = True
        self.logger.info(
            f"数据库初始化完成（引擎 {self.engine}，插件 {len(self.plugins)} 个）"
        )
        return True

    def _attach_plugins_to_value(self) -> None:
        """把插件字典挂到 value.plugin.database_business（value.plugin 缺失时自动补）"""
        try:
            holder = getattr(self.value, "plugin", None)
            if holder is None:
                holder = types.SimpleNamespace()
                setattr(self.value, "plugin", holder)
            setattr(holder, "database_business", self.plugins)
            self.logger.debug(
                f"已将 {len(self.plugins)} 个插件挂载到 value.plugin.database_business"
            )
        except Exception as e:
            self.logger.error(f"挂载插件到 value.plugin.database_business 失败: {e}")

    async def close(self) -> None:
        """关闭插件与数据库连接"""
        try:
            self.plugin_manager.shutdown_all()
            self.plugins = {}
        except Exception as e:
            self.logger.error(f"关闭数据库插件失败: {e}")

        try:
            await self.driver.close()
        except Exception as e:
            self.logger.error(f"关闭数据库连接失败: {e}")
        self._initialized = False

    def close_pool(self) -> None:
        """进程退出兜底：同步关闭连接池"""
        try:
            self.driver.close_pool()
        except Exception as e:
            self.logger.error(f"同步关闭数据库连接池失败: {e}")

    # ── 便捷转发 ────────────────────────────────────────────────

    async def ping(self):
        """心跳检测，返回 (是否可用, 版本号或错误信息)"""
        return await self.driver.ping()

    async def execute(self, sql: str, params: Optional[Any] = None) -> int:
        """执行写操作 / DDL，返回影响行数"""
        return await self.driver.execute(sql, params)

    async def execute_many(self, sql: str, params_list: List[Any]) -> int:
        """批量执行写操作，返回影响行数"""
        return await self.driver.execute_many(sql, params_list)

    async def fetch_one(self, sql: str, params: Optional[Any] = None) -> Optional[dict]:
        """查询单行"""
        return await self.driver.fetch_one(sql, params)

    async def fetch_all(self, sql: str, params: Optional[Any] = None) -> List[dict]:
        """查询多行"""
        return await self.driver.fetch_all(sql, params)

    async def table_exists(self, table: str) -> bool:
        """判断表是否存在"""
        return await self.driver.table_exists(table)

    async def table_columns(self, table: str) -> List[str]:
        """获取表字段名列表"""
        return await self.driver.table_columns(table)

    async def ensure_tables(self, table_defs: dict) -> None:
        """建表 / 补字段（只增不删不改）"""
        await self.driver.ensure_tables(table_defs)

    # ── 状态 ────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """当前是否已连接"""
        return bool(getattr(self.driver, "connected", False))

    @property
    def initialized(self) -> bool:
        """是否已完成初始化流程"""
        return bool(self._initialized)
