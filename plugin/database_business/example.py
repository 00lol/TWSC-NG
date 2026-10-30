# -*- coding:utf-8 -*-
"""数据库业务插件 —— 示例模板（可拷贝修改）

继承 ``DatabasePlugin`` 后自动获得：
  - ``self.logger``   日志对象（来自 value.logger）
  - ``self.database`` 数据库管理器（value.database，DatabaseManager）
  - ``self.user``     默认用户表操作（value.database.user，User）
  - ``await self.execute / fetch_one / fetch_all ...`` 数据库快捷方法
  - ``await self.ensure_tables({...})`` 建表 / 补字段

## 基本用法（复制本文件后改名 class 与 name）

  1. 插件会被自动加载，按 name 注册：

     plugin = value.plugin.database_business["example_business"]

  2. 调业务方法：

     rows = await plugin.get_active_users()
     user = await plugin.get_user_by_id(1)

## 注意事项

  - ``initialize`` 里只做"拿到连接 + 建自己需要的表"，不要写重业务逻辑；
  - 模板默认**不建表**，需要建表时把下方注释打开并改成自己的表结构；
  - 插件内所有 IO 都必须是 async（本项目全异步）。
"""

from databases.plugin import DatabasePlugin


class ExampleBusinessPlugin(DatabasePlugin):
    """示例数据库业务插件（模板，可整块复制修改）"""

    def __init__(self):
        super().__init__(
            # 后续代码中通过 name 访问插件：
            # value.plugin.database_business["example_business"]
            name="example_business",
            description="示例数据库业务插件（模板）",
        )

    # ── 初始化：只注入连接，建表代码默认注释 ──────────────────────

    async def initialize(self, value) -> bool:
        if not await super().initialize(value):
            return False

        # 需要本插件自己的表时，打开下面的注释并改成自己的结构：
        # await self.ensure_tables({
        #     "exam_scores": [
        #         ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        #         ("user_id", "INT NOT NULL"),
        #         ("exam_name", "VARCHAR(100)"),
        #         ("score", "DECIMAL(5,2)"),
        #         ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        #     ],
        # })
        return True

    # ── 业务方法（示例，默认注释）────────────────────────────────

    # async def get_active_users(self):
    #     """获取所有活跃用户"""
    #     return await self.fetch_all(
    #         "SELECT user_id, username, created_at, role_level FROM users "
    #         "WHERE is_active = 1"
    #     )
    #
    # async def get_user_by_id(self, user_id: int):
    #     """根据 ID 查询用户"""
    #     return await self.fetch_one(
    #         "SELECT * FROM users WHERE user_id = %s", (user_id,)
    #     )
    #
    # async def update_last_login(self, user_id: int, ip: str):
    #     """更新最后登录时间和 IP"""
    #     return await self.execute(
    #         "UPDATE users SET last_login = NOW(), ip_address = %s WHERE user_id = %s",
    #         (ip, user_id),
    #     )

    # ── 可选：注册 FastAPI 路由 ──────────────────────────────────
    # 如果这个插件需要暴露 HTTP 接口，覆盖 register_routes（由路由层挂到 /plugin/<name>）：
    #
    # def register_routes(self, router):
    #     @router.get("/api/users/list")
    #     async def list_users():
    #         return await self.get_active_users()
