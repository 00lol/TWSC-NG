# -*- coding:utf-8 -*-
"""管理台 - 认证与账号插件（``console_auth``）

负责 ``API-接口清单.md`` 中认证与账号域的接口（清单序号 1-4、54-59、60-61）：

- 认证：``POST /admin/login``、``POST /admin/register``、``POST /admin/logout``、
  ``GET /admin/me``；
- 白名单：``GET/POST /admin/accounts/whitelist``、
  ``POST /admin/accounts/whitelist/{id}/remove``；
- 后台账号：``GET /admin/accounts``、``POST /admin/accounts/{id}/status``、
  ``POST /admin/accounts/{id}/role``（全部仅 ``super``）；
- 操作日志：``GET /admin/audit-logs``、``GET /admin/audit-logs/modules``。

实现约定见 ``docs/后端实现约定.md``：
- 令牌用 ``transaction.console.issue_admin_token``（12h Bearer）；
- 密码哈希/校验用 ``transaction.auth.Password``（PBKDF2-SHA256，库列 VARCHAR(255)）；
- 角色 ``super``/``op``/``developer``，``developer`` 只读；
- 登录失败锁定：同「账号+IP」15 分钟内 5 次失败 -> 403/2002，计数 key 前缀
  ``twsc:console:lock:``（缓存不可用时退化为进程内字典）；
- 写操作落 ``console_audit_logs``；本插件在 ``initialize`` 里注册
  ``value.hooks["console_audit"]``，供其它插件的 ``audit()`` 落库。
"""
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from databases.plugin import DatabasePlugin
from transaction.auth import Password
from transaction.console import (
    CODE_DUPLICATE,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    CODE_PARAM,
    CODE_SERVER,
    CODE_UNAUTH,
    ApiError,
    admin_guard,
    audit,
    client_ip,
    issue_admin_token,
    jsonable,
    mask_phone,
    now_dt,
    ok,
    paged,
    parse_date,
    parse_page,
    require_db,
    revoke_admin,
    set_admin_role,
    to_iso,
    unrevoke_admin,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
# 登录失败锁定（docs/后端实现约定.md §3）
LOCK_PREFIX = "twsc:console:lock:"
LOCK_WINDOW_SECONDS = 15 * 60      # 15 分钟窗口
LOCK_MAX_FAILURES = 5              # 窗口内最多失败 5 次

# 手机号：11 位、1 开头
PHONE_PATTERN = re.compile(r"^1\d{10}$")
# 密码强度：≥8 位且同时含字母与数字
PASSWORD_LETTER = re.compile(r"[A-Za-z]")
PASSWORD_DIGIT = re.compile(r"\d")

# 角色展示名（存储值 -> 中文标签）
ROLE_LABELS = {
    "super": "管理员",
    "op": "运营",
    "developer": "开发者",
}
# 接口入参角色 -> 存储值（``admin`` 映射为 ``super``）
INPUT_ROLE_TO_STORAGE = {
    "admin": "super",
    "super": "super",
    "op": "op",
    "developer": "developer",
}
# 写接口入参允许的角色（契约 §11.2 / §11.6）
INPUT_ROLES = ("admin", "developer")

# 三张自有表（字段与 docs/后端实现约定.md §6 一致）
TABLES = {
    "console_admins": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("username", "VARCHAR(64) NOT NULL UNIQUE"),
        ("password_hash", "VARCHAR(255) NOT NULL"),
        ("name", "VARCHAR(64)"),
        ("role", "VARCHAR(16) DEFAULT 'op'"),
        ("phone", "VARCHAR(20) UNIQUE"),
        ("status", "VARCHAR(16) DEFAULT 'active'"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("last_login", "DATETIME"),
        ("last_ip", "VARCHAR(45)"),
    ],
    "console_whitelist": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("phone", "VARCHAR(20) NOT NULL UNIQUE"),
        ("role", "VARCHAR(16) DEFAULT 'developer'"),
        ("created_by", "INT"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("removed_at", "DATETIME"),
    ],
    "console_audit_logs": [
        ("id", "BIGINT AUTO_INCREMENT PRIMARY KEY"),
        ("actor_id", "INT"),
        ("actor_name", "VARCHAR(64)"),
        ("module", "VARCHAR(64)"),
        ("action", "VARCHAR(64)"),
        ("target_type", "VARCHAR(32)"),
        ("target_id", "VARCHAR(64)"),
        ("detail", "JSON"),
        ("result", "VARCHAR(16) DEFAULT 'success'"),
        ("ip", "VARCHAR(45)"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def role_label(role: Any) -> Optional[str]:
    """角色中文标签；未知角色返回 None（不猜）"""
    text = str(role or "").strip().lower()
    return ROLE_LABELS.get(text)


def storage_role(role: Any) -> Optional[str]:
    """接口入参角色 -> 存储值（``admin`` -> ``super``）；不认识的返回 None"""
    text = str(role or "").strip().lower()
    return INPUT_ROLE_TO_STORAGE.get(text)


def _text(value: Any) -> str:
    """宽松取字符串（去空白）"""
    return str(value or "").strip()


def _as_bool(value: Any) -> Optional[bool]:
    """宽松解析布尔；无法判断返回 None（交给调用方报 1001）"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and int(value) in (0, 1):
        return bool(int(value))
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _pg_offset(page: int, page_size: int) -> int:
    """分页偏移量（page 从 1 开始）"""
    return max(0, (int(page) - 1) * int(page_size))


class ConsoleAuthPlugin(DatabasePlugin):
    """管理台 - 认证与账号（登录/注册/白名单/账号/操作日志）"""

    prefix = "/admin"  # 由 main.py 挂载时读取

    def __init__(self):
        super().__init__(
            name="console_auth",
            description="管理台 - 认证与账号（登录、注册、白名单、账号、操作日志）",
        )
        # 缓存不可用时的登录失败计数兜底：{key: (count, expire_at)}
        self._lock_memory: dict = {}

    # ── 生命周期 ────────────────────────────────────────────────

    async def initialize(self, value) -> bool:
        """建表 + 注册审计钩子（供其它插件的 audit() 落库）"""
        if not await super().initialize(value):
            return False

        await self.ensure_tables(TABLES)

        # 首次启动兜底：管理台一个账号都没有时自动创建超级管理员，
        # 避免「忘了开 SERVER_CONSOLE_SEED 就永远登录不进去」。
        await self._bootstrap_admin()

        # 审计钩子：transaction.console.audit() 通过 value.hooks 找落库实现
        hooks = getattr(value, "hooks", None)
        if hooks is None:
            hooks = {}
            try:
                setattr(value, "hooks", hooks)
            except Exception:
                hooks = None
        if isinstance(hooks, dict):
            hooks["console_audit"] = self.record_audit
            self.logger.info("[console_auth] 审计钩子 console_audit 已注册")
        else:
            self.logger.warning("[console_auth] value.hooks 不可用，审计日志不会落库")
        return True

    # ── 首次启动引导 ────────────────────────────────────────────

    async def _bootstrap_admin(self) -> None:
        """管理台没有账号时创建超级管理员

        账号/密码取 ``SERVER_CONSOLE_ADMIN_USER`` / ``SERVER_CONSOLE_ADMIN_PASSWORD``；
        未配置密码时：调试模式用演示密码 ``Admin@12345``，生产模式随机生成并写日志。
        """
        try:
            row = await self.fetch_one("SELECT COUNT(*) AS total FROM console_admins")
            if row and int(row.get("total") or 0) > 0:
                return
            server = {}
            try:
                server = dict((getattr(self.value, "config", None) or {}).get("server") or {})
            except Exception:
                server = {}
            username = _text(server.get("console_admin_user")) or "admin_super"
            password = str(server.get("console_admin_password") or "")
            debug = str(server.get("debug", False)).strip().lower() in ("1", "true", "yes", "on")
            if not password:
                password = "Admin@12345" if debug else secrets.token_hex(8)
                self.logger.warning(
                    f"[console_auth] 管理台无账号，已创建超级管理员 {username} / {password}"
                    f"（请登录后立即修改密码；演示数据请设 SERVER_CONSOLE_SEED=True）"
                )
            else:
                self.logger.info(f"[console_auth] 管理台无账号，已按配置创建超级管理员 {username}")
            await self.execute(
                "INSERT INTO console_admins (username, password_hash, name, role, status, created_at) "
                "VALUES (%s,%s,%s,'super','active',NOW())",
                (username, Password.hash(password), "超级管理员"),
            )
        except Exception as e:
            self.logger.error(f"[console_auth] 创建默认超级管理员失败（不影响服务启动）: {e}")

    # ── 审计钩子 ────────────────────────────────────────────────

    async def record_audit(
        self,
        *,
        admin: Optional[dict],
        module: str,
        action: str,
        target_type: Optional[str] = None,
        target_id: Any = None,
        detail: Optional[Any] = None,
        result: str = "success",
        ip: Optional[str] = None,
    ) -> None:
        """写一条操作日志（``transaction.console.audit`` 的落库钩子）

        任何异常都只记日志，绝不影响业务接口本身。
        """
        if self.database is None or not getattr(self.database, "connected", False):
            return

        actor = admin or {}
        try:
            raw_id = actor.get("id")
            actor_id = int(raw_id) if raw_id not in (None, "") else None
        except (TypeError, ValueError):
            actor_id = None
        actor_name = actor.get("name") or actor.get("username") or ""

        try:
            detail_text = json.dumps(
                jsonable(detail) if detail is not None else {},
                ensure_ascii=False,
                default=str,
            )
        except Exception:
            detail_text = "{}"

        try:
            await self.execute(
                "INSERT INTO console_audit_logs "
                "(actor_id, actor_name, module, action, target_type, target_id, "
                " detail, result, ip, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    actor_id,
                    str(actor_name)[:64],
                    str(module or "")[:64],
                    str(action or "")[:64],
                    (str(target_type)[:32] if target_type else None),
                    (str(target_id)[:64] if target_id is not None else None),
                    detail_text,
                    str(result or "success")[:16],
                    (str(ip)[:45] if ip else None),
                    now_dt(),
                ),
            )
        except Exception as e:
            try:
                self.logger.warning(f"[console_auth] 写操作日志失败（不影响业务）: {e}")
            except Exception:
                pass

    # ── 登录失败锁定 ────────────────────────────────────────────

    @staticmethod
    def _lock_key(account: str, ip: str) -> str:
        """锁定计数 key：同「账号+IP」"""
        return f"{LOCK_PREFIX}{account}:{ip or '未知'}"

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """宽松转 int"""
        try:
            if value is None or value == "":
                return default
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    async def _lock_failures(self, key: str) -> int:
        """读取当前失败次数（缓存优先，缓存不可用读进程内字典）"""
        cache = getattr(self.value, "cache", None)
        if cache is not None:
            try:
                raw = await cache.get(key)
            except Exception:
                raw = None
            if raw not in (None, ""):
                return self._safe_int(raw, 0)

        item = self._lock_memory.get(key)
        if not item:
            return 0
        count, expire_at = item
        if expire_at <= time.time():
            self._lock_memory.pop(key, None)
            return 0
        return int(count)

    async def _lock_hit(self, key: str) -> int:
        """记录一次登录失败，返回累计失败次数"""
        cache = getattr(self.value, "cache", None)
        if cache is not None:
            try:
                count = int(await cache.incr(key) or 0)
                if count == 1:
                    # 首次失败时设置 15 分钟窗口
                    await cache.expire(key, LOCK_WINDOW_SECONDS)
                if count > 0:
                    return count
            except Exception as e:
                try:
                    self.logger.warning(f"[console_auth] 缓存计数失败，改用内存计数: {e}")
                except Exception:
                    pass

        # 内存兜底
        now = time.time()
        count, expire_at = self._lock_memory.get(key, (0, 0.0))
        if expire_at <= now:
            count, expire_at = 0, now + LOCK_WINDOW_SECONDS
        count += 1
        self._lock_memory[key] = (count, expire_at)
        return count

    async def _lock_reset(self, key: str) -> None:
        """登录成功清零失败计数"""
        self._lock_memory.pop(key, None)
        cache = getattr(self.value, "cache", None)
        if cache is not None:
            try:
                await cache.delete(key)
            except Exception:
                pass

    # ── 请求体 ──────────────────────────────────────────────────

    @staticmethod
    async def _json_body(request: Request) -> dict:
        """读 JSON 请求体（只支持 JSON，空体返回 {}）"""
        try:
            raw = await request.body()
        except Exception:
            raise ApiError(CODE_PARAM, "请求体解析失败", 400)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(CODE_PARAM, "请求体必须是 JSON", 400)
        if not isinstance(data, dict):
            raise ApiError(CODE_PARAM, "请求体必须是 JSON 对象", 400)
        return data

    # ── 路由注册 ────────────────────────────────────────────────

    def register_routes(self, router: APIRouter) -> None:
        """由 main.py 挂载到 ``/admin`` 前缀"""
        self._route(router)

    def _route(self, router: APIRouter) -> None:  # noqa: C901 - 路由集中声明，可读性优先
        # ------------------------------------------------------------------ #
        # 1. POST /admin/login —— 用户名或手机号 + 密码
        # ------------------------------------------------------------------ #
        @router.post("/login")
        async def login(request: Request):
            require_db(self)
            body = await self._json_body(request)

            account = _text(body.get("username"))
            password = str(body.get("password") or "")
            if len(account) < 3 or len(account) > 40:
                raise ApiError(CODE_PARAM, "请输入用户名或手机号（3-40 字）", 400)
            if len(password) < 6 or len(password) > 64:
                raise ApiError(CODE_PARAM, "请输入密码（6-64 字）", 400)

            ip = await client_ip(request)
            lock_key = self._lock_key(account, ip)

            # 先判锁定：锁定期间即使密码正确也不放行
            if await self._lock_failures(lock_key) >= LOCK_MAX_FAILURES:
                raise ApiError(CODE_FORBIDDEN, "失败次数过多，账号已锁定 15 分钟", 403)

            # username 或 phone 精确匹配（用户名优先）
            row = await self.fetch_one(
                "SELECT id, username, password_hash, name, role, phone, status "
                "FROM console_admins WHERE username = %s OR phone = %s "
                "ORDER BY (username = %s) DESC, id ASC LIMIT 1",
                (account, account, account),
            )

            if row is None or not Password.verify(password, row.get("password_hash") or ""):
                failures = await self._lock_hit(lock_key)
                await audit(
                    request,
                    {"id": None, "name": account},
                    module="认证",
                    action="login_failed",
                    target_type="admin",
                    target_id=None,
                    detail={"account": account, "failures": failures},
                    result="failure",
                )
                if failures >= LOCK_MAX_FAILURES:
                    raise ApiError(
                        CODE_FORBIDDEN, "失败次数过多，账号已锁定 15 分钟", 403
                    )
                raise ApiError(CODE_UNAUTH, "账号或密码错误", 401)

            status = str(row.get("status") or "active").strip().lower()
            if status != "active":
                raise ApiError(CODE_FORBIDDEN, "账号已被停用", 403)

            # 登录成功：清零失败计数，记录登录时间/IP
            await self._lock_reset(lock_key)
            try:
                await self.execute(
                    "UPDATE console_admins SET last_login = %s, last_ip = %s WHERE id = %s",
                    (now_dt(), ip, int(row["id"])),
                )
            except Exception as e:
                # 登录时间写失败不影响登录本身
                self.logger.warning(f"[console_auth] 更新登录时间失败: {e}")

            admin = {
                "id": int(row["id"]),
                "username": row.get("username"),
                "name": row.get("name"),
                "role": str(row.get("role") or "op").strip().lower(),
            }
            token = issue_admin_token(self.value, admin)

            await audit(
                request,
                admin,
                module="认证",
                action="login",
                target_type="admin",
                target_id=admin["id"],
                detail={"ip": ip},
            )

            return ok(
                {
                    "token": token,
                    "admin": {
                        "id": admin["id"],
                        "username": admin["username"],
                        "name": admin["name"],
                        "role": admin["role"],
                        # 手机号统一脱敏（docs/后端实现约定.md §4）
                        "phone": mask_phone(row.get("phone")),
                    },
                }
            )

        # ------------------------------------------------------------------ #
        # 2. POST /admin/register —— 白名单手机号注册
        # ------------------------------------------------------------------ #
        @router.post("/register")
        async def register(request: Request):
            require_db(self)
            body = await self._json_body(request)

            phone = _text(body.get("phone"))
            password = str(body.get("password") or "")
            name = _text(body.get("name"))

            if not PHONE_PATTERN.match(phone):
                raise ApiError(CODE_PARAM, "手机号格式不正确", 400)
            if (
                len(password) < 8
                or not PASSWORD_LETTER.search(password)
                or not PASSWORD_DIGIT.search(password)
            ):
                raise ApiError(CODE_PARAM, "密码至少 8 位且需同时包含字母和数字", 400)
            if name and len(name) > 30:
                raise ApiError(CODE_PARAM, "姓名不能超过 30 个字", 400)
            name = name or phone  # 契约：默认手机号

            # 白名单：必须存在且未被移出
            whitelist = await self.fetch_one(
                "SELECT id, phone, role, removed_at FROM console_whitelist WHERE phone = %s",
                (phone,),
            )
            if whitelist is None or whitelist.get("removed_at") is not None:
                raise ApiError(
                    CODE_FORBIDDEN, "该手机号不在白名单内，请联系管理员登记", 403
                )

            # 已注册（phone 或 username 命中都算）
            exists = await self.fetch_one(
                "SELECT id FROM console_admins WHERE phone = %s OR username = %s LIMIT 1",
                (phone, phone),
            )
            if exists is not None:
                raise ApiError(CODE_DUPLICATE, "该手机号已注册，请直接登录", 409)

            stored_role = storage_role(whitelist.get("role")) or "developer"
            await self.execute(
                "INSERT INTO console_admins "
                "(username, password_hash, name, role, phone, status, created_at) "
                "VALUES (%s,%s,%s,%s,%s,'active',%s)",
                (phone, Password.hash(password), name, stored_role, phone, now_dt()),
            )
            created = await self.fetch_one(
                "SELECT id FROM console_admins WHERE username = %s LIMIT 1", (phone,)
            )
            if created is None:
                raise ApiError(CODE_SERVER, "注册失败，请稍后重试", 500)

            new_id = int(created["id"])
            await audit(
                request,
                {"id": new_id, "name": name, "role": stored_role},
                module="账号管理",
                action="register",
                target_type="admin",
                target_id=new_id,
                detail={"phone": mask_phone(phone), "role": stored_role},
            )

            # 契约：返回 {id, username, role}；role 为存储值（admin -> super）
            return ok({"id": new_id, "username": phone, "role": stored_role})

        # ------------------------------------------------------------------ #
        # 3. POST /admin/logout —— 无状态令牌
        # ------------------------------------------------------------------ #
        @router.post("/logout")
        async def logout(request: Request, admin: dict = Depends(admin_guard())):
            await audit(
                request,
                admin,
                module="认证",
                action="logout",
                target_type="admin",
                target_id=admin.get("id"),
            )
            return ok({"ok": True})

        # ------------------------------------------------------------------ #
        # 4. GET /admin/me —— 当前管理员（角色已含缓存覆盖值）
        # ------------------------------------------------------------------ #
        @router.get("/me")
        async def me(request: Request, admin: dict = Depends(admin_guard())):
            return ok(
                {
                    "id": admin.get("id"),
                    "username": admin.get("username"),
                    "name": admin.get("name"),
                    "role": admin.get("role"),
                }
            )

        # ------------------------------------------------------------------ #
        # 54. GET /admin/accounts/whitelist —— 白名单列表（仅 super）
        # ------------------------------------------------------------------ #
        @router.get("/accounts/whitelist")
        async def list_whitelist(
            request: Request,
            admin: dict = Depends(admin_guard(roles=("super",))),
        ):
            require_db(self)
            status = (_text(request.query_params.get("status")) or "all").lower()
            if status not in ("all", "waiting", "registered", "removed"):
                status = "all"

            sql = (
                "SELECT w.id, w.phone, w.role, w.created_by, w.created_at, w.removed_at, "
                "a.id AS admin_id, a.username, a.name, a.status AS admin_status "
                "FROM console_whitelist w "
                "LEFT JOIN console_admins a ON a.phone = w.phone"
            )
            if status == "removed":
                sql += " WHERE w.removed_at IS NOT NULL"
            elif status == "waiting":
                sql += " WHERE w.removed_at IS NULL AND a.id IS NULL"
            elif status == "registered":
                sql += " WHERE w.removed_at IS NULL AND a.id IS NOT NULL"
            sql += " ORDER BY w.id DESC"

            rows = await self.fetch_all(sql)
            items = []
            for row in rows:
                removed = row.get("removed_at") is not None
                admin_id = row.get("admin_id")
                if removed:
                    state = "removed"
                elif admin_id:
                    state = "registered"
                else:
                    state = "waiting"
                items.append(
                    {
                        "id": int(row["id"]),
                        "phone": mask_phone(row.get("phone")),
                        "role": row.get("role"),
                        "created_by": row.get("created_by"),
                        "created_at": to_iso(row.get("created_at")),
                        "removed_at": to_iso(row.get("removed_at")),
                        "status": state,
                        "role_label": role_label(row.get("role")),
                        "admin_id": int(admin_id) if admin_id else None,
                        "username": row.get("username"),
                        "name": row.get("name"),
                        "admin_status": row.get("admin_status"),
                    }
                )
            return ok({"list": items})

        # ------------------------------------------------------------------ #
        # 55. POST /admin/accounts/whitelist —— 登记白名单（仅 super）
        # ------------------------------------------------------------------ #
        @router.post("/accounts/whitelist")
        async def add_whitelist(
            request: Request,
            admin: dict = Depends(admin_guard(roles=("super",), write=True)),
        ):
            require_db(self)
            body = await self._json_body(request)

            phone = _text(body.get("phone"))
            role_input = _text(body.get("role")).lower()
            if not PHONE_PATTERN.match(phone):
                raise ApiError(CODE_PARAM, "手机号格式不正确", 400)
            if role_input not in INPUT_ROLES:
                raise ApiError(CODE_PARAM, "角色只能是 admin 或 developer", 400)
            stored_role = storage_role(role_input) or "developer"
            operator_id = admin.get("id")

            existing = await self.fetch_one(
                "SELECT id, removed_at FROM console_whitelist WHERE phone = %s", (phone,)
            )
            if existing is not None:
                whitelist_id = int(existing["id"])
                if existing.get("removed_at") is None:
                    raise ApiError(CODE_DUPLICATE, "该手机号已在白名单内", 409)
                # 曾被移出：重新登记（同一行复用，避免 UNIQUE 冲突）
                await self.execute(
                    "UPDATE console_whitelist SET role = %s, created_by = %s, "
                    "created_at = %s, removed_at = NULL WHERE id = %s",
                    (stored_role, operator_id, now_dt(), whitelist_id),
                )
                await audit(
                    request,
                    admin,
                    module="账号管理",
                    action="add_whitelist",
                    target_type="whitelist",
                    target_id=whitelist_id,
                    detail={
                        "phone": mask_phone(phone),
                        "role": stored_role,
                        "readded": True,
                    },
                )
                return ok({"id": whitelist_id, "readded": True})

            await self.execute(
                "INSERT INTO console_whitelist (phone, role, created_by, created_at, removed_at) "
                "VALUES (%s,%s,%s,%s,NULL)",
                (phone, stored_role, operator_id, now_dt()),
            )
            created = await self.fetch_one(
                "SELECT id FROM console_whitelist WHERE phone = %s LIMIT 1", (phone,)
            )
            if created is None:
                raise ApiError(CODE_SERVER, "登记白名单失败，请稍后重试", 500)
            whitelist_id = int(created["id"])
            await audit(
                request,
                admin,
                module="账号管理",
                action="add_whitelist",
                target_type="whitelist",
                target_id=whitelist_id,
                detail={"phone": mask_phone(phone), "role": stored_role},
            )
            return ok({"id": whitelist_id})

        # ------------------------------------------------------------------ #
        # 56. POST /admin/accounts/whitelist/{id}/remove —— 移出白名单（仅 super）
        # ------------------------------------------------------------------ #
        @router.post("/accounts/whitelist/{whitelist_id}/remove")
        async def remove_whitelist(
            request: Request,
            whitelist_id: int,
            admin: dict = Depends(admin_guard(roles=("super",), write=True)),
        ):
            require_db(self)
            row = await self.fetch_one(
                "SELECT id, phone, removed_at FROM console_whitelist WHERE id = %s",
                (whitelist_id,),
            )
            if row is None:
                raise ApiError(CODE_NOT_FOUND, "白名单记录不存在", 404)

            # 幂等：已移出也返回成功，只补写日志便于追溯
            already_removed = row.get("removed_at") is not None
            if not already_removed:
                await self.execute(
                    "UPDATE console_whitelist SET removed_at = %s "
                    "WHERE id = %s AND removed_at IS NULL",
                    (now_dt(), whitelist_id),
                )
            await audit(
                request,
                admin,
                module="账号管理",
                action="remove_whitelist",
                target_type="whitelist",
                target_id=whitelist_id,
                detail={
                    "phone": mask_phone(row.get("phone")),
                    "alreadyRemoved": already_removed,
                },
            )
            return ok({"id": whitelist_id, "removed": True})

        # ------------------------------------------------------------------ #
        # 57. GET /admin/accounts —— 后台账号列表（仅 super）
        # ------------------------------------------------------------------ #
        @router.get("/accounts")
        async def list_accounts(
            request: Request,
            admin: dict = Depends(admin_guard(roles=("super",))),
        ):
            require_db(self)
            rows = await self.fetch_all(
                "SELECT id, username, name, role, phone, status, created_at "
                "FROM console_admins ORDER BY id ASC"
            )
            items = [
                {
                    "id": int(row["id"]),
                    "username": row.get("username"),
                    "name": row.get("name"),
                    "role": row.get("role"),
                    "phone": mask_phone(row.get("phone")),
                    "status": row.get("status"),
                    "created_at": to_iso(row.get("created_at")),
                    "role_label": role_label(row.get("role")),
                }
                for row in rows
            ]
            return ok({"list": items})

        # ------------------------------------------------------------------ #
        # 58. POST /admin/accounts/{id}/status —— 停用/恢复（仅 super）
        # ------------------------------------------------------------------ #
        @router.post("/accounts/{account_id}/status")
        async def update_account_status(
            request: Request,
            account_id: int,
            admin: dict = Depends(admin_guard(roles=("super",), write=True)),
        ):
            require_db(self)
            body = await self._json_body(request)
            disabled = _as_bool(body.get("disabled"))
            if disabled is None:
                raise ApiError(CODE_PARAM, "参数 disabled 必须为布尔值", 400)

            if disabled and int(account_id) == self._safe_int(admin.get("id"), 0):
                raise ApiError(CODE_PARAM, "不能停用自己的账号", 400)

            row = await self.fetch_one(
                "SELECT id, username, name, status FROM console_admins WHERE id = %s",
                (account_id,),
            )
            if row is None:
                raise ApiError(CODE_NOT_FOUND, "账号不存在", 404)

            new_status = "disabled" if disabled else "active"
            await self.execute(
                "UPDATE console_admins SET status = %s WHERE id = %s",
                (new_status, account_id),
            )
            # 令牌立即失效 / 恢复（缓存标记，依赖里会读）
            if disabled:
                await revoke_admin(self.value, account_id)
            else:
                await unrevoke_admin(self.value, account_id)

            await audit(
                request,
                admin,
                module="账号管理",
                action="disable_account" if disabled else "enable_account",
                target_type="admin",
                target_id=account_id,
                detail={"username": row.get("username"), "status": new_status},
            )
            return ok({"id": account_id, "status": new_status})

        # ------------------------------------------------------------------ #
        # 59. POST /admin/accounts/{id}/role —— 调整角色（仅 super）
        # ------------------------------------------------------------------ #
        @router.post("/accounts/{account_id}/role")
        async def update_account_role(
            request: Request,
            account_id: int,
            admin: dict = Depends(admin_guard(roles=("super",), write=True)),
        ):
            require_db(self)
            body = await self._json_body(request)
            role_input = _text(body.get("role")).lower()
            if role_input not in INPUT_ROLES:
                raise ApiError(CODE_PARAM, "角色只能是 admin 或 developer", 400)
            stored_role = storage_role(role_input) or "developer"

            row = await self.fetch_one(
                "SELECT id, username, name, role FROM console_admins WHERE id = %s",
                (account_id,),
            )
            if row is None:
                raise ApiError(CODE_NOT_FOUND, "账号不存在", 404)

            await self.execute(
                "UPDATE console_admins SET role = %s WHERE id = %s",
                (stored_role, account_id),
            )
            # 改角色免重新登录即时生效（缓存标记）
            await set_admin_role(self.value, account_id, stored_role)

            await audit(
                request,
                admin,
                module="账号管理",
                action="update_account_role",
                target_type="admin",
                target_id=account_id,
                detail={
                    "username": row.get("username"),
                    "oldRole": row.get("role"),
                    "newRole": stored_role,
                },
            )
            return ok(
                {
                    "id": account_id,
                    "role": stored_role,
                    "role_label": role_label(stored_role),
                }
            )

        # ------------------------------------------------------------------ #
        # 60. GET /admin/audit-logs —— 操作日志（分页，pageSize 默认 50）
        # ------------------------------------------------------------------ #
        @router.get("/audit-logs")
        async def list_audit_logs(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            query = request.query_params
            page, page_size = parse_page(request, default=50, maximum=50)

            where: list = []
            params: list = []

            date_from = parse_date(query.get("from"))
            if date_from is not None:
                where.append("created_at >= %s")
                params.append(datetime(date_from.year, date_from.month, date_from.day))
            date_to = parse_date(query.get("to"))
            if date_to is not None:
                # to 含当日：小于次日 00:00
                next_day = date_to + timedelta(days=1)
                where.append("created_at < %s")
                params.append(datetime(next_day.year, next_day.month, next_day.day))

            actor = _text(query.get("actor"))
            if actor:
                if actor.isdigit():
                    # 契约：actor 支持姓名模糊或 id 精确；纯数字按 id 精确匹配
                    where.append("actor_id = %s")
                    params.append(int(actor))
                else:
                    where.append("actor_name LIKE %s")
                    params.append(f"%{actor}%")

            module = _text(query.get("module"))
            if module:
                where.append("module = %s")
                params.append(module)

            action = _text(query.get("action"))
            if action:
                where.append("`action` LIKE %s")
                params.append(f"%{action}%")

            clause = (" WHERE " + " AND ".join(where)) if where else ""

            total_row = await self.fetch_one(
                f"SELECT COUNT(*) AS c FROM console_audit_logs{clause}", tuple(params)
            )
            total = int((total_row or {}).get("c") or 0)

            rows = await self.fetch_all(
                "SELECT id, actor_id, actor_name, module, `action`, target_type, "
                "target_id, detail, result, created_at "
                f"FROM console_audit_logs{clause} ORDER BY id DESC LIMIT %s OFFSET %s",
                tuple(params + [page_size, _pg_offset(page, page_size)]),
            )
            items = [
                {
                    "id": int(row["id"]),
                    "actor_id": row.get("actor_id"),
                    "actor_name": row.get("actor_name"),
                    "module": row.get("module"),
                    "action": row.get("action"),
                    "target_type": row.get("target_type"),
                    "target_id": row.get("target_id"),
                    "detail": jsonable(row.get("detail")),
                    "result": row.get("result"),
                    "created_at": to_iso(row.get("created_at")),
                }
                for row in rows
            ]
            return ok(paged(items, total, page, page_size))

        # ------------------------------------------------------------------ #
        # 61. GET /admin/audit-logs/modules —— 模块字典（按数量倒序）
        # ------------------------------------------------------------------ #
        @router.get("/audit-logs/modules")
        async def list_audit_modules(
            request: Request, admin: dict = Depends(admin_guard())
        ):
            require_db(self)
            rows = await self.fetch_all(
                "SELECT module, COUNT(*) AS count FROM console_audit_logs "
                "WHERE module IS NOT NULL AND module <> '' "
                "GROUP BY module ORDER BY count DESC, module ASC"
            )
            return ok(
                {
                    "list": [
                        {
                            "module": row.get("module"),
                            "count": int(row.get("count") or 0),
                        }
                        for row in rows
                    ]
                }
            )


__all__ = ["ConsoleAuthPlugin", "TABLES", "role_label", "storage_role"]
