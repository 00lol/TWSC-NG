# -*- coding:utf-8 -*-
"""管理台 - 用户管理业务插件（console_users）

负责接口（见 ``API-接口清单.md`` 与 ``docs/后端实现约定.md``）：

- 8  ``GET  /admin/verify-queue``          认证待审队列（不分页，剔除 id_card_enc）
- 12 ``GET  /admin/users``                 用户列表（分页 + tab/role/status/verifyStatus/keyword + stats）
- 13 ``GET  /admin/users/:id``             用户档案（verifies / profile / demands / orders 摘要）
- 14 ``POST /admin/users/:id/audit``       实名 / 执照审核
- 15 ``POST /admin/users/:id/ban``         封禁 / 解封（飞手有进行中订单时必须 force=true）

表（字段与 docs §6 完全一致，只增不删不改）：
``platform_users`` / ``pilot_profiles`` / ``client_profiles`` / ``verify_records`` / ``demands``。

跨插件只读：``orders`` 表由 ``console_orders`` 插件创建；本插件只做「详情摘要」与
「封禁影响判断」，表不存在时按空处理（不报错）。

实现约定（与契约的补充说明）：
- 手机号一律 ``mask_phone``，``id_card_enc`` 任何接口都不回传；
- ``tab=pending`` / ``verifyStatus`` 均以 ``verify_records.status`` 判定（与 ``/admin/verify-queue`` 同源）；
- ``/admin/verify-queue`` 每条记录额外带 ``user_nickname`` / ``user_phone``（脱敏）/ ``user_role``；
- ``audit`` 返回在 ``{id, verifyType, status}`` 之外补充记录 id（``verifyId``），``id`` 为目标用户 id；
- ``intentTypeNames`` 由 ``intent_types`` 的编码映射为中文名（见 ``INTENT_TYPE_NAMES``，未知编码原样返回）。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from databases.plugin import DatabasePlugin
from transaction.console import (
    ApiError,
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_PARAM,
    admin_guard,
    audit,
    mask_phone,
    now_dt,
    ok,
    paged,
    parse_page,
    require_db,
    split_values,
    to_iso,
)

# --------------------------------------------------------------------------- #
# 表结构（权威定义见 docs/后端实现约定.md §6）
# --------------------------------------------------------------------------- #
TABLES = {
    "platform_users": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("nickname", "VARCHAR(64)"),
        ("phone", "VARCHAR(20) UNIQUE"),
        ("password_hash", "VARCHAR(255)"),
        ("role", "VARCHAR(16) DEFAULT 'client'"),
        ("real_verified", "TINYINT(1) DEFAULT 0"),
        ("real_name", "VARCHAR(64)"),
        ("status", "VARCHAR(16) DEFAULT 'active'"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
    ],
    "pilot_profiles": [
        ("user_id", "INT PRIMARY KEY"),
        ("license_no", "VARCHAR(64)"),
        ("license_status", "VARCHAR(16) DEFAULT 'pending'"),
        ("identity_status", "VARCHAR(16) DEFAULT 'pending'"),
        ("intent_types", "JSON"),
        ("avail_days", "INT DEFAULT 0"),
        ("skill_count", "INT DEFAULT 0"),
        ("rating", "DECIMAL(2,1) DEFAULT 5.0"),
        ("bio", "VARCHAR(255)"),
    ],
    "client_profiles": [
        ("user_id", "INT PRIMARY KEY"),
        ("company_name", "VARCHAR(128)"),
        ("kind", "VARCHAR(16) DEFAULT 'personal'"),
        ("identity_status", "VARCHAR(16) DEFAULT 'pending'"),
    ],
    "verify_records": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("user_id", "INT NOT NULL"),
        ("verify_type", "VARCHAR(16)"),
        ("status", "VARCHAR(16) DEFAULT 'pending'"),
        ("real_name", "VARCHAR(64)"),
        ("license_no", "VARCHAR(64)"),
        ("id_card_enc", "VARCHAR(255)"),
        ("reason", "VARCHAR(255)"),
        ("submitted_at", "DATETIME"),
        ("reviewed_at", "DATETIME"),
        ("reviewer_id", "INT"),
    ],
    "demands": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("user_id", "INT"),
        ("title", "VARCHAR(128)"),
        ("task_type", "VARCHAR(32)"),
        ("description", "TEXT"),
        ("budget", "INT DEFAULT 0"),
        ("status", "VARCHAR(16) DEFAULT 'recruiting'"),
        ("created_at", "DATETIME"),
    ],
}

# --------------------------------------------------------------------------- #
# 枚举与常量
# --------------------------------------------------------------------------- #
VERIFY_STATUSES = ("pending", "approved", "rejected")
USER_TABS = ("all", "pending", "pilot", "client", "banned")
USER_ROLES = ("pilot", "client")
USER_STATUSES = ("active", "banned")
VERIFY_TYPES = ("identity", "license")

#: 驳回原因最大长度（清单 §5.3）
REASON_MAX = 200
#: 进行中订单状态（清单 §5.4 / docs §7）
ACTIVE_ORDER_STATUSES = ("material_review", "in_progress", "delivered", "accepted", "disputed")
#: 认证队列不分页，后端限制条数
VERIFY_QUEUE_LIMIT = 100
#: 详情页 demands / orders 各取最近 N 条
DETAIL_LIMIT = 10

#: 意向类型编码 -> 展示名（未知编码原样返回，绝不丢数据）
INTENT_TYPE_NAMES = {
    "aerial": "航拍",
    "inspection": "巡检",
    "mapping": "测绘",
    "agriculture": "植保",
    "event": "活动跟拍",
    "training": "培训",
    "repair": "维修",
    "rent": "租赁",
    "rental": "租赁",
}


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _int(value: Any, default: int = 0) -> int:
    """宽松转 int（DB 的 SUM/COUNT 可能返回 Decimal）"""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _db_value(value: Any) -> Any:
    """DB 值 -> 可 JSON 序列化值（时间 / Decimal / bytes）"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (datetime, date)):
        return to_iso(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _json_row(row: Optional[dict], exclude: tuple = ()) -> dict:
    """DB 行 -> 可序列化 dict（可排除敏感字段）"""
    if not row:
        return {}
    return {key: _db_value(value) for key, value in row.items() if key not in exclude}


def _choice(raw: Any, allowed: tuple, label: str) -> Optional[str]:
    """校验枚举 query/body 参数：空值视为未传，非法值 -> 1001"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text not in allowed:
        raise ApiError(CODE_PARAM, f"参数不合法：{label} 仅支持 {' / '.join(allowed)}")
    return text


def _like(keyword: str) -> str:
    """构造 LIKE 参数（转义 % _ \\，避免用户输入被当成通配符）"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _reason_text(raw: Any, required: bool = False) -> Optional[str]:
    """校验 reason：必须是字符串、≤200 字；驳回时必填"""
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ApiError(CODE_PARAM, "参数不合法：reason 必须为字符串")
    text = raw.strip()
    if len(text) > REASON_MAX:
        raise ApiError(CODE_PARAM, f"参数不合法：reason 不能超过 {REASON_MAX} 个字符")
    if required and not text:
        raise ApiError(CODE_PARAM, "参数不合法：驳回时必须填写原因")
    return text or None


def _intent_names(value: Any) -> list:
    """intent_types（JSON 字符串 / 列表 / 字典）-> 中文展示名列表"""
    names = []
    for item in split_values(value):
        if isinstance(item, dict):
            code = item.get("type") or item.get("key") or item.get("value")
        else:
            code = item
        text = str(code or "").strip()
        if not text:
            continue
        names.append(INTENT_TYPE_NAMES.get(text, text))
    return names


async def _json_body(request: Request) -> dict:
    """读取 JSON 请求体（失败 -> 1001，不抛 FastAPI 校验错误）"""
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(CODE_PARAM, "参数不合法：请求体不是合法的 JSON")
    if not isinstance(payload, dict):
        raise ApiError(CODE_PARAM, "参数不合法：请求体必须是 JSON 对象")
    return payload


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #
class ConsoleUsersPlugin(DatabasePlugin):
    """管理台 - 用户管理（用户列表/档案/认证审核/封禁）"""

    prefix = "/admin"

    def __init__(self):
        super().__init__(name="console_users", description="管理台 - 用户管理（认证审核/封禁）")
        # orders 表由 console_orders 插件创建：只缓存「存在」结果，不存在时每次兜底重查
        self._orders_ready = False

    # ── 生命周期 ──────────────────────────────────────────────

    async def initialize(self, value) -> bool:
        if not await super().initialize(value):
            return False
        await self.ensure_tables(TABLES)
        return True

    # ── 基础工具 ──────────────────────────────────────────────

    async def _table_exists(self, table: str) -> bool:
        """判断表是否存在（information_schema，参数化）"""
        try:
            database = self.database
            if database is None:
                return False
            return bool(await database.table_exists(table))
        except Exception as e:
            if self.logger is not None:
                self.logger.warning(f"[console_users] 检查表 {table} 是否存在失败: {e}")
            return False

    async def _has_orders(self) -> bool:
        """orders 表是否可用（不存在时详情/封禁按空处理）"""
        if self._orders_ready:
            return True
        self._orders_ready = await self._table_exists("orders")
        return self._orders_ready

    async def _query_stats(self) -> dict:
        """用户分栏统计：一条聚合 SQL 取全量计数"""
        row = await self.fetch_one(
            "SELECT COUNT(*) AS c_all,"
            " SUM(CASE WHEN role = 'pilot' THEN 1 ELSE 0 END) AS c_pilot,"
            " SUM(CASE WHEN role = 'client' THEN 1 ELSE 0 END) AS c_client,"
            " SUM(CASE WHEN status = 'banned' THEN 1 ELSE 0 END) AS c_banned,"
            " (SELECT COUNT(DISTINCT user_id) FROM verify_records WHERE status = 'pending') AS c_pending"
            " FROM platform_users"
        ) or {}
        return {
            "all": _int(row.get("c_all")),
            "pending": _int(row.get("c_pending")),
            "pilot": _int(row.get("c_pilot")),
            "client": _int(row.get("c_client")),
            "banned": _int(row.get("c_banned")),
        }

    @staticmethod
    def _list_item(row: dict) -> dict:
        """列表行 -> AdminUserListResp.list 项（字段严格按清单 §5.1 示例）"""
        role = row.get("role")
        has_pilot_profile = row.get("pilot_profile_id") is not None
        if role == "pilot":
            identity_status = row.get("pilot_identity_status")
            license_status = row.get("license_status")
            intent_types = row.get("intent_types")
            avail_days = _int(row.get("avail_days")) if has_pilot_profile else None
            skill_count = _int(row.get("skill_count")) if has_pilot_profile else None
        elif role == "client":
            identity_status = row.get("client_identity_status")
            license_status = None
            intent_types = None
            avail_days = None
            skill_count = None
        else:
            identity_status = None
            license_status = None
            intent_types = None
            avail_days = None
            skill_count = None

        return {
            "id": _int(row.get("id")),
            "nickname": row.get("nickname"),
            "phone": mask_phone(row.get("phone")),
            "role": role,
            "real_verified": _int(row.get("real_verified")),
            "real_name": row.get("real_name"),
            "status": row.get("status"),
            "created_at": to_iso(row.get("created_at")),
            "identity_status": identity_status,
            "license_status": license_status,
            "demand_count": _int(row.get("demand_count")),
            "order_count": _int(row.get("order_count")),
            "intent_types": intent_types,
            "avail_days": avail_days,
            "skill_count": skill_count,
            "intentTypeNames": _intent_names(intent_types),
            "availabilitySet": bool(avail_days) if avail_days is not None else False,
            "availabilityDays": avail_days,
        }

    @staticmethod
    def _order_summary(row: dict, user_id: int) -> dict:
        """订单摘要（详情页最多 10 条）"""
        return {
            "id": _int(row.get("id")),
            "order_no": row.get("order_no"),
            "title": row.get("title"),
            "status": row.get("status"),
            "amount": _int(row.get("amount")),
            "city": row.get("city"),
            "created_at": to_iso(row.get("created_at")),
            "role": "pilot" if _int(row.get("pilot_id")) == _int(user_id) else "client",
        }

    # ── 业务查询：列表 ────────────────────────────────────────

    async def query_users(
        self,
        page: int = 1,
        page_size: int = 20,
        tab: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        verify_status: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> dict:
        """GET /admin/users：分页 + 筛选 + stats"""
        tab = _choice(tab, USER_TABS, "tab")
        role = _choice(role, USER_ROLES, "role")
        status = _choice(status, USER_STATUSES, "status")
        verify_status = _choice(verify_status, VERIFY_STATUSES, "verifyStatus")

        where = []
        params = []

        # 分栏
        if tab == "pending":
            where.append(
                "EXISTS (SELECT 1 FROM verify_records v WHERE v.user_id = u.id AND v.status = 'pending')"
            )
        elif tab in ("pilot", "client"):
            where.append("u.role = %s")
            params.append(tab)
        elif tab == "banned":
            where.append("u.status = %s")
            params.append("banned")

        # 精确筛选
        if role:
            where.append("u.role = %s")
            params.append(role)
        if status:
            where.append("u.status = %s")
            params.append(status)
        if verify_status:
            where.append(
                "EXISTS (SELECT 1 FROM verify_records v WHERE v.user_id = u.id AND v.status = %s)"
            )
            params.append(verify_status)

        # 关键词：昵称 / 手机号 / 真实姓名 模糊
        text = str(keyword or "").strip()
        if text:
            like = _like(text)
            where.append("(u.nickname LIKE %s OR u.phone LIKE %s OR u.real_name LIKE %s)")
            params.extend([like, like, like])

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        params = tuple(params)

        total_row = await self.fetch_one(
            f"SELECT COUNT(*) AS total FROM platform_users u{where_sql}", params
        )
        total = _int((total_row or {}).get("total"))

        # 订单数：orders 表不可用时按 0 处理（跨插件只读，不阻断用户列表）
        order_count_sql = (
            "(SELECT COUNT(*) FROM orders o WHERE o.client_id = u.id OR o.pilot_id = u.id)"
            if await self._has_orders()
            else "0"
        )

        offset = max(0, (int(page) - 1) * int(page_size))
        sql = (
            "SELECT u.id, u.nickname, u.phone, u.role, u.real_verified, u.real_name,"
            " u.status, u.created_at,"
            " p.user_id AS pilot_profile_id,"
            " p.identity_status AS pilot_identity_status,"
            " p.license_status AS license_status,"
            " p.intent_types AS intent_types,"
            " p.avail_days AS avail_days,"
            " p.skill_count AS skill_count,"
            " c.user_id AS client_profile_id,"
            " c.identity_status AS client_identity_status,"
            " (SELECT COUNT(*) FROM demands d WHERE d.user_id = u.id) AS demand_count,"
            f" {order_count_sql} AS order_count"
            " FROM platform_users u"
            " LEFT JOIN pilot_profiles p ON p.user_id = u.id"
            " LEFT JOIN client_profiles c ON c.user_id = u.id"
            f"{where_sql}"
            " ORDER BY u.id DESC"
            " LIMIT %s OFFSET %s"
        )
        rows = await self.fetch_all(sql, params + (int(page_size), offset))
        items = [self._list_item(row) for row in rows]

        result = paged(items, total, page, page_size)
        result["stats"] = await self._query_stats()
        return result

    # ── 业务查询：详情 ────────────────────────────────────────

    async def get_user_detail(self, user_id: int) -> dict:
        """GET /admin/users/:id：基本信息 + 认证记录 + 档案 + 需求/订单摘要"""
        user = await self.fetch_one(
            "SELECT id, nickname, phone, role, real_verified, real_name, status,"
            " created_at, updated_at FROM platform_users WHERE id = %s",
            (user_id,),
        )
        if not user:
            raise ApiError(CODE_NOT_FOUND, "用户不存在", 404)

        # 认证记录：显式列清单，绝不带 id_card_enc
        verifies = await self.fetch_all(
            "SELECT id, user_id, verify_type, status, real_name, license_no, reason,"
            " submitted_at, reviewed_at, reviewer_id"
            " FROM verify_records WHERE user_id = %s"
            " ORDER BY submitted_at DESC, id DESC",
            (user_id,),
        )

        # 档案：未选身份（或档案未建）为 null
        profile = None
        role = user.get("role")
        if role == "pilot":
            profile = await self.fetch_one(
                "SELECT * FROM pilot_profiles WHERE user_id = %s", (user_id,)
            )
        elif role == "client":
            profile = await self.fetch_one(
                "SELECT * FROM client_profiles WHERE user_id = %s", (user_id,)
            )

        demands = await self.fetch_all(
            "SELECT id, title, task_type, budget, status, created_at FROM demands"
            " WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
            (user_id, DETAIL_LIMIT),
        )

        orders = []
        if await self._has_orders():
            orders = await self.fetch_all(
                "SELECT id, order_no, title, status, amount, city, client_id, pilot_id, created_at"
                " FROM orders WHERE client_id = %s OR pilot_id = %s"
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (user_id, user_id, DETAIL_LIMIT),
            )

        return {
            "user": {
                "id": _int(user.get("id")),
                "nickname": user.get("nickname"),
                "phone": mask_phone(user.get("phone")),
                "role": role,
                "real_verified": _int(user.get("real_verified")),
                "real_name": user.get("real_name"),
                "status": user.get("status"),
                "created_at": to_iso(user.get("created_at")),
                "updated_at": to_iso(user.get("updated_at")),
            },
            "verifies": [_json_row(item) for item in verifies],
            "profile": _json_row(profile) if profile else None,
            "demands": [_json_row(item) for item in demands],
            "orders": [self._order_summary(item, user_id) for item in orders],
        }

    # ── 业务查询：认证待审队列 ────────────────────────────────

    async def query_verify_queue(self, status: Optional[str] = None) -> dict:
        """GET /admin/verify-queue：不分页，默认 pending"""
        status = _choice(status, VERIFY_STATUSES, "status") or "pending"
        rows = await self.fetch_all(
            "SELECT v.id, v.user_id, v.verify_type, v.status, v.real_name, v.license_no,"
            " v.reason, v.submitted_at, v.reviewed_at, v.reviewer_id,"
            " u.nickname AS user_nickname, u.phone AS user_phone, u.role AS user_role"
            " FROM verify_records v LEFT JOIN platform_users u ON u.id = v.user_id"
            " WHERE v.status = %s"
            " ORDER BY v.submitted_at DESC, v.id DESC LIMIT %s",
            (status, VERIFY_QUEUE_LIMIT),
        )

        items = []
        for row in rows:
            item = _json_row(row, exclude=("user_phone",))
            item["user_nickname"] = row.get("user_nickname")
            item["user_phone"] = mask_phone(row.get("user_phone"))
            items.append(item)
        return {"list": items}

    # ── 业务写操作：认证审核 ──────────────────────────────────

    async def audit_user(
        self,
        user_id: int,
        verify_type: Any,
        approved: Any,
        reason: Any = None,
        reviewer_id: Any = None,
    ) -> dict:
        """POST /admin/users/:id/audit：审核 identity / license

        - 无待审记录：404（有非 pending 同类型记录则 409「已审核」）
        - 通过：更新 platform_users.real_verified（实名）/ pilot_profiles 对应状态（执照）
        """
        verify_type = _choice(verify_type, VERIFY_TYPES, "verifyType")
        if verify_type is None:
            raise ApiError(CODE_PARAM, "参数不合法：verifyType 仅支持 identity / license")
        if not isinstance(approved, bool):
            raise ApiError(CODE_PARAM, "参数不合法：approved 必须为布尔值")
        reason_text = _reason_text(reason, required=not approved)

        user = await self.fetch_one(
            "SELECT id, role FROM platform_users WHERE id = %s", (user_id,)
        )
        if not user:
            raise ApiError(CODE_NOT_FOUND, "用户不存在", 404)

        record = await self.fetch_one(
            "SELECT id, real_name, license_no FROM verify_records"
            " WHERE user_id = %s AND verify_type = %s AND status = 'pending'"
            " ORDER BY submitted_at DESC, id DESC LIMIT 1",
            (user_id, verify_type),
        )
        if not record:
            exists = await self.fetch_one(
                "SELECT id FROM verify_records WHERE user_id = %s AND verify_type = %s"
                " ORDER BY id DESC LIMIT 1",
                (user_id, verify_type),
            )
            if exists:
                raise ApiError(CODE_CONFLICT, "该记录已审核，不能重复处理", 409)
            raise ApiError(CODE_NOT_FOUND, "该用户没有待审核的认证记录", 404)

        status = "approved" if approved else "rejected"
        await self.execute(
            "UPDATE verify_records SET status = %s, reason = %s, reviewed_at = %s,"
            " reviewer_id = %s WHERE id = %s",
            (status, reason_text, now_dt(), reviewer_id, _int(record.get("id"))),
        )
        await self._apply_verify_result(user, verify_type, status, record)

        return {
            "id": _int(user_id),
            "verifyId": _int(record.get("id")),
            "verifyType": verify_type,
            "status": status,
        }

    async def _apply_verify_result(
        self,
        user: dict,
        verify_type: str,
        status: str,
        record: dict,
    ) -> None:
        """审核结果回写用户 / 档案状态（通过时才点亮 real_verified）"""
        user_id = _int(user.get("id"))
        if verify_type == "identity":
            real_verified = 1 if status == "approved" else 0
            real_name = str((record or {}).get("real_name") or "").strip()[:64] or None
            await self.execute(
                "UPDATE platform_users SET real_verified = %s,"
                " real_name = COALESCE(NULLIF(%s, ''), real_name) WHERE id = %s",
                (real_verified, real_name, user_id),
            )
            # 飞手 / 客户两种档案都尝试更新（实际只会命中一种）
            await self.execute(
                "UPDATE pilot_profiles SET identity_status = %s WHERE user_id = %s",
                (status, user_id),
            )
            await self.execute(
                "UPDATE client_profiles SET identity_status = %s WHERE user_id = %s",
                (status, user_id),
            )
        elif verify_type == "license":
            license_no = str((record or {}).get("license_no") or "").strip()[:64] or None
            await self.execute(
                "UPDATE pilot_profiles SET license_status = %s,"
                " license_no = COALESCE(NULLIF(%s, ''), license_no) WHERE user_id = %s",
                (status, license_no, user_id),
            )

    # ── 业务写操作：封禁 / 解封 ───────────────────────────────

    async def ban_user(
        self,
        user_id: int,
        banned: Any,
        reason: Any = None,
        force: Any = False,
    ) -> dict:
        """POST /admin/users/:id/ban：封禁 / 解封

        封禁飞手且存在进行中订单时，必须 ``force=true``，否则 409 + affectedOrders。
        """
        if not isinstance(banned, bool):
            raise ApiError(CODE_PARAM, "参数不合法：banned 必须为布尔值")
        if force is None:
            force = False
        if not isinstance(force, bool):
            raise ApiError(CODE_PARAM, "参数不合法：force 必须为布尔值")
        _reason_text(reason)  # reason 仅校验长度/类型（封禁原因当前只进审计日志）

        user = await self.fetch_one(
            "SELECT id, role, status FROM platform_users WHERE id = %s", (user_id,)
        )
        if not user:
            raise ApiError(CODE_NOT_FOUND, "用户不存在", 404)

        previous_status = str(user.get("status") or "")
        # 只在「首次封禁」时检查进行中订单（重复封禁保持幂等）
        if banned and previous_status != "banned":
            affected = await self._active_orders_of_pilot(user_id)
            if affected and not force:
                raise ApiError(
                    CODE_CONFLICT,
                    f"该用户有 {len(affected)} 个进行中订单，确认影响后请带 force=true 再次提交",
                    409,
                    data={"affectedOrders": affected},
                )

        new_status = "banned" if banned else "active"
        await self.execute(
            "UPDATE platform_users SET status = %s WHERE id = %s", (new_status, user_id)
        )
        return {"id": _int(user_id), "status": new_status}

    async def _active_orders_of_pilot(self, user_id: int) -> list:
        """飞手进行中订单（orders 表不存在时按空处理）"""
        if not await self._has_orders():
            return []
        placeholders = ", ".join(["%s"] * len(ACTIVE_ORDER_STATUSES))
        try:
            rows = await self.fetch_all(
                "SELECT order_no, title, status FROM orders"
                f" WHERE pilot_id = %s AND status IN ({placeholders})"
                " ORDER BY created_at DESC, id DESC",
                (user_id, *ACTIVE_ORDER_STATUSES),
            )
        except Exception as e:
            if self.logger is not None:
                self.logger.warning(f"[console_users] 查询进行中订单失败（按无订单处理）: {e}")
            return []
        return [
            {
                "order_no": row.get("order_no"),
                "title": row.get("title"),
                "status": row.get("status"),
            }
            for row in rows
        ]

    # ── 路由注册 ──────────────────────────────────────────────

    def register_routes(self, router: APIRouter) -> None:
        self._route(router)

    def _route(self, router: APIRouter) -> None:
        # ── 8. 认证待审队列（不分页，默认 pending）────────────
        @router.get("/verify-queue")
        async def verify_queue(
            request: Request,
            admin: dict = Depends(admin_guard()),
        ):
            require_db(self)
            data = await self.query_verify_queue(request.query_params.get("status"))
            return ok(data)

        # ── 12. 用户列表（分页 + 筛选 + stats）───────────────
        @router.get("/users")
        async def list_users(
            request: Request,
            admin: dict = Depends(admin_guard()),
        ):
            require_db(self)
            page, page_size = parse_page(request)
            query = request.query_params
            data = await self.query_users(
                page=page,
                page_size=page_size,
                tab=query.get("tab"),
                role=query.get("role"),
                status=query.get("status"),
                verify_status=query.get("verifyStatus"),
                keyword=query.get("keyword"),
            )
            return ok(data)

        # ── 13. 用户档案 ─────────────────────────────────────
        @router.get("/users/{user_id}")
        async def get_user(
            user_id: int,
            admin: dict = Depends(admin_guard()),
        ):
            require_db(self)
            data = await self.get_user_detail(user_id)
            return ok(data)

        # ── 14. 实名 / 执照审核 ──────────────────────────────
        @router.post("/users/{user_id}/audit")
        async def audit_user_route(
            user_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            payload = await _json_body(request)
            detail = {
                "verifyType": payload.get("verifyType"),
                "approved": payload.get("approved"),
                "reason": payload.get("reason"),
            }
            try:
                result = await self.audit_user(
                    user_id=user_id,
                    verify_type=payload.get("verifyType"),
                    approved=payload.get("approved"),
                    reason=payload.get("reason"),
                    reviewer_id=admin.get("id"),
                )
            except ApiError as exc:
                # 失败也要记审计（谁在什么时候尝试审了哪条、为何失败）
                await audit(
                    request,
                    admin,
                    module="用户管理",
                    action="audit_user",
                    target_type="user",
                    target_id=user_id,
                    detail={**detail, "error": exc.message},
                    result="failed",
                )
                raise
            await audit(
                request,
                admin,
                module="用户管理",
                action="audit_user",
                target_type="user",
                target_id=user_id,
                detail={**detail, "status": result.get("status")},
                result="success",
            )
            return ok(result)

        # ── 15. 封禁 / 解封 ──────────────────────────────────
        @router.post("/users/{user_id}/ban")
        async def ban_user_route(
            user_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            payload = await _json_body(request)
            detail = {
                "banned": payload.get("banned"),
                "reason": payload.get("reason"),
                "force": payload.get("force"),
            }
            try:
                result = await self.ban_user(
                    user_id=user_id,
                    banned=payload.get("banned"),
                    reason=payload.get("reason"),
                    force=payload.get("force", False),
                )
            except ApiError as exc:
                await audit(
                    request,
                    admin,
                    module="用户管理",
                    action="ban_user",
                    target_type="user",
                    target_id=user_id,
                    detail={**detail, "error": exc.message},
                    result="failed",
                )
                raise
            await audit(
                request,
                admin,
                module="用户管理",
                action="ban_user",
                target_type="user",
                target_id=user_id,
                detail={**detail, "status": result.get("status")},
                result="success",
            )
            return ok(result)
