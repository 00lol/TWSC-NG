# -*- coding:utf-8 -*-
"""数据库业务插件目录

运行时由 ``databases.mains.DatabaseManager`` 扫描本目录并加载插件：
- 每个 ``.py`` 文件（``__init__.py`` 除外）都会被导入，其中继承 ``DatabasePlugin``
  的类会被实例化，按 ``plugin.name`` 注册；
- 含 ``__init__.py`` 的子包同样会被扫描；
- 加载后的插件可从 ``value.plugin.database_business[插件名]`` 取用。

``example.py`` 是可拷贝即用的模板。
"""

__all__ = []
