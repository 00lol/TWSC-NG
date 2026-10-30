# -*- coding:utf-8 -*-
"""默认用户表 users 与用户操作（异步版，对标 TWSC 的 User 类）

所有方法：
- 只做参数组装 + 调用 database 的封装方法，不自己拿游标；
- 内部异常统一 catch 并记中文日志，返回安全默认值（False / None / [] / 0），不让上层崩。
"""
import logging
from typing import Dict, List, Optional, Tuple

# users 表结构定义（只增不删不改，ensure_tables 据此建表 / 补字段）
USERS_TABLE: Dict[str, List[Tuple[str, str]]] = {
    "users": [
        ("user_id", "INT AUTO_INCREMENT PRIMARY KEY"),
        (
            "username",
            "VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL UNIQUE",
        ),
        ("password_hash", "VARCHAR(255) NOT NULL"),
        ("nickname", "VARCHAR(100)"),
        # 唯一索引允许 NULL（MySQL 唯一索引允许多个 NULL）
        ("email", "VARCHAR(255) UNIQUE"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
        ("last_login", "DATETIME"),
        ("is_active", "TINYINT(1) DEFAULT 1"),
        ("role_level", "TINYINT DEFAULT 0"),
        ("group_", "VARCHAR(300)"),
        ("ip_address", "VARCHAR(45)"),
        ("projects", "JSON"),
        ("belong", "INT DEFAULT 0"),
    ],
}


class User:
    """默认用户表操作"""

    # 权限等级：越负越高
    LEVEL_SUPER = -128  # 超级管理员
    LEVEL_ADMIN = -100  # 管理员
    LEVEL_USER = 0  # 普通用户

    #: 对外查询时隐藏的敏感字段
    HIDDEN_FIELDS = ("password_hash",)

    def __init__(self, database, logger):
        # 只组装：database 为 databases.base.Database 子类实例（如 MySQL）
        self.database = database
        self.logger = logger or logging.getLogger("database.user")

    # ── 建表 ────────────────────────────────────────────────────

    async def ensure_tables(self) -> None:
        """确保 users 表存在，并补齐缺失字段"""
        if self.database is None:
            self.logger.warning("数据库不可用，跳过 users 表初始化")
            return
        try:
            await self.database.ensure_tables(USERS_TABLE)
            self.logger.info("users 表结构检查完成")
        except Exception as e:
            self.logger.error(f"初始化 users 表失败: {e}")

    # ── 内部工具 ────────────────────────────────────────────────

    @staticmethod
    def _public(row: Optional[dict]) -> Optional[dict]:
        """去掉敏感字段（password_hash）后的用户字典"""
        if not row:
            return None
        return {k: v for k, v in dict(row).items() if k not in User.HIDDEN_FIELDS}

    async def _username_exists(self, username: str) -> bool:
        """用户名是否已存在（精确匹配）"""
        return await self.get_user_by_username(username) is not None

    async def _email_exists(self, email: Optional[str]) -> bool:
        """邮箱是否已存在（NULL 不算）"""
        if not email:
            return False
        return await self.get_user_by_email(email) is not None

    # ── 查询 ────────────────────────────────────────────────────

    async def verify_user(self, username: str) -> Optional[dict]:
        """按用户名精确查询（登录用，返回含 password_hash 的完整行）

        精确匹配写法 ``username = CAST(%s AS BINARY)``：
        - 语义等价于旧实现的 ``BINARY username = %s``（大小写敏感、中文精确匹配）；
        - MySQL 8.0 已废弃 ``BINARY expr`` 语法并会告警，官方建议改用 CAST。
        """
        try:
            return await self.database.fetch_one(
                "SELECT * FROM users WHERE username = CAST(%s AS BINARY)", (username,)
            )
        except Exception as e:
            self.logger.error(f"验证用户 {username} 失败: {e}")
            return None

    async def get_user_by_id(self, user_id: int) -> Optional[dict]:
        """按用户 ID 查询（不含 password_hash）"""
        try:
            row = await self.database.fetch_one(
                "SELECT * FROM users WHERE user_id = %s", (user_id,)
            )
            return self._public(row)
        except Exception as e:
            self.logger.error(f"按 ID 查询用户 {user_id} 失败: {e}")
            return None

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        """按用户名精确查询（不含 password_hash）"""
        try:
            row = await self.database.fetch_one(
                "SELECT * FROM users WHERE username = CAST(%s AS BINARY)", (username,)
            )
            return self._public(row)
        except Exception as e:
            self.logger.error(f"按用户名查询用户 {username} 失败: {e}")
            return None

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        """按邮箱查询（不含 password_hash）"""
        if not email:
            return None
        try:
            row = await self.database.fetch_one(
                "SELECT * FROM users WHERE email = %s LIMIT 1", (email,)
            )
            return self._public(row)
        except Exception as e:
            self.logger.error(f"按邮箱查询用户 {email} 失败: {e}")
            return None

    async def get_users(self, belong: Optional[int] = None) -> List[dict]:
        """获取用户列表（不含 password_hash）

        Args:
            belong: 归属过滤；None 表示全部用户
        """
        sql = (
            "SELECT user_id, username, nickname, email, created_at, updated_at, "
            "last_login, is_active, role_level, group_, ip_address, belong "
            "FROM users"
        )
        try:
            if belong is None:
                rows = await self.database.fetch_all(f"{sql} ORDER BY user_id")
            else:
                rows = await self.database.fetch_all(
                    f"{sql} WHERE belong = %s ORDER BY user_id", (belong,)
                )
            return [row for row in rows]
        except Exception as e:
            self.logger.error(f"获取用户列表失败: {e}")
            return []

    async def count(self) -> int:
        """用户总数"""
        try:
            row = await self.database.fetch_one("SELECT COUNT(*) AS total FROM users")
            return int((row or {}).get("total") or 0)
        except Exception as e:
            self.logger.error(f"统计用户数量失败: {e}")
            return 0

    async def transfer(self) -> Optional[dict]:
        """是否存在超级管理员，返回该用户（不含 password_hash）或 None"""
        try:
            row = await self.database.fetch_one(
                "SELECT * FROM users WHERE role_level = %s LIMIT 1",
                (self.LEVEL_SUPER,),
            )
            return self._public(row)
        except Exception as e:
            self.logger.error(f"检查超级管理员失败: {e}")
            return None

    # ── 新增 ────────────────────────────────────────────────────

    async def register_user(
        self,
        username: str,
        password_hash: str,
        email: Optional[str] = None,
        nickname: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """前台注册（等级固定为普通用户，归属固定 0）

        Returns:
            (是否成功, 提示信息)
        """
        return await self._create_user(
            username=username,
            password_hash=password_hash,
            level=self.LEVEL_USER,
            belong=0,
            email=email,
            nickname=nickname,
            success_message="注册成功",
        )

    async def add_user(
        self,
        username: str,
        password_hash: str,
        level: int = 0,
        belong: int = 0,
        email: Optional[str] = None,
        nickname: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """后台新增用户（可指定等级 / 归属）

        Returns:
            (是否成功, 提示信息)；新增成功后 user_id 可用 get_user_by_username 回查
        """
        return await self._create_user(
            username=username,
            password_hash=password_hash,
            level=level,
            belong=belong,
            email=email,
            nickname=nickname,
            success_message="添加用户成功",
        )

    async def _create_user(
        self,
        username: str,
        password_hash: str,
        level: int,
        belong: int,
        email: Optional[str],
        nickname: Optional[str],
        success_message: str,
    ) -> Tuple[bool, str]:
        """新增用户的公共实现"""
        try:
            if not username or not password_hash:
                return False, "用户名和密码不能为空"
            if await self._username_exists(username):
                return False, "用户名已存在"
            if await self._email_exists(email):
                return False, "邮箱已存在"

            await self.database.execute(
                "INSERT INTO users "
                "(username, password_hash, nickname, email, role_level, belong) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (username, password_hash, nickname, email, int(level), int(belong)),
            )
            # execute 返回 rowcount，真实主键再查一次（只用于日志，便于排查）
            row = await self.database.fetch_one(
                "SELECT user_id FROM users WHERE username = CAST(%s AS BINARY)", (username,)
            )
            new_id = int((row or {}).get("user_id") or 0)
            self.logger.info(f"新增用户成功: {username}（user_id={new_id}，等级 {level}）")
            return True, success_message
        except Exception as e:
            self.logger.error(f"新增用户 {username} 失败: {e}")
            return False, "添加用户失败（用户名或邮箱可能已存在）"

    # ── 更新 ────────────────────────────────────────────────────

    async def update_login(self, user_id: int, ip: Optional[str] = None) -> bool:
        """更新最后登录时间与登录 IP"""
        try:
            affected = await self.database.execute(
                "UPDATE users SET last_login = NOW(), ip_address = %s WHERE user_id = %s",
                (ip, user_id),
            )
            return affected > 0
        except Exception as e:
            self.logger.error(f"更新用户 {user_id} 登录信息失败: {e}")
            return False

    async def update_password(self, user_id: int, new_password_hash: str) -> bool:
        """更新密码哈希"""
        try:
            affected = await self.database.execute(
                "UPDATE users SET password_hash = %s WHERE user_id = %s",
                (new_password_hash, user_id),
            )
            return affected > 0
        except Exception as e:
            self.logger.error(f"更新用户 {user_id} 密码失败: {e}")
            return False

    async def update_level(self, user_id: int, level: int) -> bool:
        """更新权限等级（越负越高）"""
        try:
            affected = await self.database.execute(
                "UPDATE users SET role_level = %s WHERE user_id = %s",
                (int(level), user_id),
            )
            return affected > 0
        except Exception as e:
            self.logger.error(f"更新用户 {user_id} 等级失败: {e}")
            return False

    async def update_active(self, user_id: int, active: bool = True) -> bool:
        """启用 / 禁用用户"""
        try:
            affected = await self.database.execute(
                "UPDATE users SET is_active = %s WHERE user_id = %s",
                (1 if active else 0, user_id),
            )
            return affected > 0
        except Exception as e:
            self.logger.error(f"更新用户 {user_id} 启用状态失败: {e}")
            return False

    # ── 删除 ────────────────────────────────────────────────────

    async def delete_user(self, user_id: int) -> bool:
        """删除用户"""
        try:
            affected = await self.database.execute(
                "DELETE FROM users WHERE user_id = %s", (user_id,)
            )
            return affected > 0
        except Exception as e:
            self.logger.error(f"删除用户 {user_id} 失败: {e}")
            return False
