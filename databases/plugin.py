# -*- coding:utf-8 -*-
"""插件基类与插件管理器（与 TWSC 同构，异步化）

- ``Plugin``：通用插件基类（同步接口）；
- ``DatabasePlugin``：数据库业务插件基类，``initialize`` 为 async，自动拿到
  ``self.logger`` / ``self.database`` / ``self.user``，并提供数据库快捷方法与建表入口；
- ``PluginManager``：按目录扫描 .py 与包目录，importlib 动态导入并实例化插件。
"""
import asyncio
import importlib
import inspect
import logging
import os
import sys
from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter
except Exception:  # pragma: no cover - FastAPI 未安装时不影响插件体系导入
    APIRouter = None  # type: ignore


class Plugin:
    """插件基类，所有插件都应继承此类"""

    def __init__(self, name: str, description: str, plugin_type: str = "generic"):
        self.name = name
        self.description = description
        self.plugin_type = plugin_type
        self.active = False
        self.value = None

    def initialize(self, value: Any) -> bool:
        """初始化（同步基类，子类可改为 async）"""
        self.value = value
        self.active = True
        return True

    def shutdown(self) -> bool:
        """关闭插件"""
        self.active = False
        self.value = None
        return True

    def get_info(self) -> Dict[str, Any]:
        """插件信息（供接口展示）"""
        return {
            "name": self.name,
            "description": self.description,
            "plugin_type": self.plugin_type,
            "active": self.active,
        }


class DatabasePlugin(Plugin):
    """数据库业务插件基类 —— 子类只写业务方法，自动获得数据库连接

    可选类属性 ``prefix``：路由挂载前缀。缺省时 main.py 挂到
    ``{base_url}/plugin/<插件名>``；管理台业务插件统一写 ``prefix = "/admin"``。
    """

    plugin_type = "database_business"
    prefix = None

    def __init__(self, name: str, description: str):
        super().__init__(name, description, plugin_type=self.plugin_type)
        self.logger = None
        self.database = None
        self.user = None

    async def initialize(self, value) -> bool:
        """异步初始化：注入 logger / database / user"""
        super().initialize(value)  # 同步基类：记录 value 与 active 状态
        self.logger = getattr(value, "logger", None) or logging.getLogger(self.name)
        self.database = getattr(value, "database", None)
        self.user = getattr(self.database, "user", None) if self.database is not None else None

        if self.database is None:
            self.logger.warning(
                f"[{self.name}] value.database 不可用，插件运行在无数据库模式"
            )
        else:
            engine = getattr(self.database, "engine", "unknown")
            self.logger.info(
                f"[{self.name}] 数据库已注入（引擎: {engine}），插件初始化完成"
            )
        return True

    async def execute(self, sql: str, params: Optional[Any] = None) -> int:
        """执行写操作 / DDL，返回影响行数"""
        if self.database is None:
            self.logger.error(f"[{self.name}] 数据库不可用，无法执行 SQL: {sql}")
            return 0
        try:
            return await self.database.execute(sql, params)
        except Exception as e:
            self.logger.error(f"[{self.name}] SQL 执行失败: {e}\nSQL: {sql}")
            raise

    async def fetch_one(self, sql: str, params: Optional[Any] = None) -> Optional[dict]:
        """查询单行"""
        if self.database is None:
            self.logger.error(f"[{self.name}] 数据库不可用，无法查询: {sql}")
            return None
        try:
            return await self.database.fetch_one(sql, params)
        except Exception as e:
            self.logger.error(f"[{self.name}] SQL 查询单行失败: {e}\nSQL: {sql}")
            raise

    async def fetch_all(self, sql: str, params: Optional[Any] = None) -> List[dict]:
        """查询多行"""
        if self.database is None:
            self.logger.error(f"[{self.name}] 数据库不可用，无法查询: {sql}")
            return []
        try:
            return await self.database.fetch_all(sql, params)
        except Exception as e:
            self.logger.error(f"[{self.name}] SQL 查询多行失败: {e}\nSQL: {sql}")
            raise

    async def ensure_tables(self, table_defs: dict) -> None:
        """确保插件所需的表 / 字段存在（只增不删不改）"""
        if self.database is None:
            self.logger.warning(f"[{self.name}] 数据库不可用，跳过建表: {list((table_defs or {}).keys())}")
            return
        await self.database.ensure_tables(table_defs)

    def register_routes(self, router: "APIRouter") -> None:
        """可选：注册 HTTP 路由（子类覆盖）"""
        pass


class PluginManager:
    """按目录扫描 Python 插件的通用管理器"""

    def __init__(self, logger, plugin_base_class=Plugin):
        self.plugins: Dict[str, Plugin] = {}
        self.plugin_dirs: List[str] = []
        self.logger = logger or logging.getLogger("plugin")
        self.plugin_base_class = plugin_base_class

    # ── 目录 / 注册 ─────────────────────────────────────────────

    def register_plugin_dir(self, dir_path: str) -> bool:
        """登记插件目录（不加载）"""
        if not dir_path or not os.path.isdir(dir_path):
            self.logger.warning(f"插件目录不存在，无法登记: {dir_path}")
            return False
        if dir_path not in self.plugin_dirs:
            self.plugin_dirs.append(dir_path)
        return True

    def register_plugin(self, plugin: Plugin) -> bool:
        """注册插件实例（同名已存在则拒绝）"""
        if plugin is None or not getattr(plugin, "name", None):
            return False
        if plugin.name in self.plugins:
            self.logger.warning(f"插件 {plugin.name} 已存在，跳过重复注册")
            return False
        self.plugins[plugin.name] = plugin
        return True

    def unregister_plugin(self, plugin_name: str) -> bool:
        """注销插件"""
        if plugin_name not in self.plugins:
            return False
        plugin = self.plugins[plugin_name]
        try:
            plugin.shutdown()
        except Exception as e:
            self.logger.error(f"关闭插件 {plugin_name} 失败: {e}")
        del self.plugins[plugin_name]
        self.logger.info(f"插件 {plugin_name} 已注销")
        return True

    def get_plugin(self, plugin_name: str):
        """按名字取插件"""
        return self.plugins.get(plugin_name)

    def get_all_plugins(self) -> Dict[str, Dict[str, Any]]:
        """全部插件的信息字典"""
        return {name: plugin.get_info() for name, plugin in self.plugins.items()}

    # ── 加载 ────────────────────────────────────────────────────

    @staticmethod
    def _module_belongs_to_dir(module, dir_path: str) -> bool:
        """判断 sys.modules 里同名模块是否来自本插件目录"""
        module_file = getattr(module, "__file__", None) or ""
        if not module_file:
            return False
        module_file = os.path.abspath(module_file)
        return module_file.startswith(os.path.abspath(dir_path) + os.sep)

    def load_plugins_from_dir(self, dir_path: str) -> List[str]:
        """从目录加载插件（支持 .py 文件与含 __init__.py 的子包）

        Returns:
            成功加载的插件名列表
        """
        loaded_plugins: List[str] = []
        if not dir_path or not os.path.isdir(dir_path):
            self.logger.warning(f"插件目录不存在，跳过加载: {dir_path}")
            return loaded_plugins

        self.register_plugin_dir(dir_path)
        if dir_path not in sys.path:
            sys.path.append(dir_path)

        base_classes = {Plugin, self.plugin_base_class}
        try:
            entries = sorted(os.listdir(dir_path))
        except Exception as e:
            self.logger.error(f"读取插件目录 {dir_path} 失败: {e}")
            return loaded_plugins

        for filename in entries:
            full_path = os.path.join(dir_path, filename)
            module_name = None
            if filename.endswith(".py"):
                if filename == "__init__.py":
                    continue
                module_name = filename[:-3]
            elif os.path.isdir(full_path) and os.path.isfile(
                os.path.join(full_path, "__init__.py")
            ):
                module_name = filename

            if not module_name or module_name.startswith("."):
                continue

            # 同名模块若来自别的目录，先清掉，避免 import 到旧模块
            cached = sys.modules.get(module_name)
            if cached is not None and not self._module_belongs_to_dir(cached, dir_path):
                sys.modules.pop(module_name, None)

            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                self.logger.error(f"导入插件模块 {module_name} 失败: {e}")
                continue

            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if not isinstance(attr, type):
                    continue
                if attr in base_classes:
                    continue
                if not issubclass(attr, self.plugin_base_class):
                    continue
                # 只实例化本模块自己定义的类，避免把 import 进来的类重复注册
                if getattr(attr, "__module__", None) != module.__name__:
                    continue
                try:
                    instance = attr()
                except Exception as e:
                    self.logger.error(f"实例化插件类 {attr_name} 失败: {e}")
                    continue
                if self.register_plugin(instance):
                    loaded_plugins.append(instance.name)
                    self.logger.info(
                        f"成功加载插件: {instance.name}（{module_name}.{attr_name}）"
                    )

        self.logger.info(
            f"插件目录 {dir_path} 加载完成，共加载 {len(loaded_plugins)} 个插件"
        )
        return loaded_plugins

    # ── 生命周期 ────────────────────────────────────────────────

    async def initialize_all(self, value) -> List[str]:
        """逐个初始化插件（兼容同步 / 异步 initialize）

        Returns:
            初始化成功的插件名列表
        """
        initialized: List[str] = []
        for name, plugin in list(self.plugins.items()):
            try:
                result = plugin.initialize(value)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as e:
                self.logger.error(f"插件 {name} 初始化异常: {e}")
                continue
            if result:
                initialized.append(name)
                self.logger.info(f"插件 {name} 初始化成功")
            else:
                self.logger.warning(f"插件 {name} 初始化失败（initialize 返回 False）")

        self.logger.info(
            f"插件初始化完成：成功 {len(initialized)}/{len(self.plugins)}"
        )
        return initialized

    def shutdown_all(self) -> None:
        """关闭并清空全部插件"""
        for name, plugin in list(self.plugins.items()):
            try:
                result = plugin.shutdown()
                if inspect.isawaitable(result):
                    # 同步方法里不能 await：调度到当前事件循环执行
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop is not None:
                        loop.create_task(result)
                    else:
                        self.logger.debug(
                            f"插件 {name} 的 shutdown 为异步实现，当前无事件循环，跳过等待"
                        )
            except Exception as e:
                self.logger.error(f"关闭插件 {name} 失败: {e}")
        self.plugins.clear()
        self.logger.info("全部插件已关闭")


def get_plugin_manager(logger=None, plugin_type="database_business") -> PluginManager:
    """插件管理器工厂

    Args:
        logger: 日志对象，缺省用标准库 logger
        plugin_type: "database_business" 使用 DatabasePlugin 作为基类，其他用通用 Plugin
    """
    if logger is None:
        logger = logging.getLogger("plugin")
    if plugin_type == "database_business":
        return PluginManager(logger, DatabasePlugin)
    return PluginManager(logger, Plugin)


__all__ = [
    "Plugin",
    "DatabasePlugin",
    "PluginManager",
    "get_plugin_manager",
]
