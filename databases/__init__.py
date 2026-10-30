# -*- coding:utf-8 -*-
"""数据库包（插件形态，MySQL / asyncmy）

对外统一导出，使用方只 import 本包即可：

    from databases import Database, MySQL, User, DatabaseManager
    from databases import Plugin, DatabasePlugin, PluginManager, get_plugin_manager
"""
from .base import Database
from .mysql import MySQL
from .user import User
from .plugin import Plugin, DatabasePlugin, PluginManager, get_plugin_manager
from .mains import DatabaseManager

__all__ = [
    "Database",
    "MySQL",
    "User",
    "DatabaseManager",
    "Plugin",
    "DatabasePlugin",
    "PluginManager",
    "get_plugin_manager",
]
