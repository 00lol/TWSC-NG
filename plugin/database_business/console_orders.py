# -*- coding:utf-8 -*-
"""管理台 - 订单管理插件（API 清单 §6，接口 16-24）

负责接口：
    16 GET  /admin/orders                               订单列表（分页 + status/group/keyword + counts）
    17 GET  /admin/orders/{order_id}                    订单详情整包
    18 POST /admin/orders/{order_id}/approve-material   通过材料审核
    19 POST /admin/orders/{order_id}/cancel             取消待付款订单
    20 POST /admin/orders/{order_id}/force-cancel       强制取消
    21 POST /admin/orders/{order_id}/force-accept       强制验收
    22 POST /admin/orders/{order_id}/refund             退款（默认全额，可指定金额）
    23 POST /admin/orders/{order_id}/settle             手动结算
    24 POST /admin/orders/{order_id}/dispute-ruling     争议裁定（判给一方 / 按比例分账）

状态机（docs/后端实现约定.md §7）：
    pending_pay → material_review → in_progress → delivered → accepted → settled
    异常分支：cancelled / refunding / refunded / disputed

本插件只建以下 8 张表（只增不删不改，字段与 docs §6 一致）：
    orders / order_timeline / order_reports / order_payments /
    order_deliverables / order_deliverable_files / conversations / messages

跨插件只读：
    platform_users / client_profiles / pilot_profiles 由 console_users 插件创建，
    本插件读取时先探测表是否存在；表还没建好也照常返回（对应字段给 None/默认值），
    不影响订单接口本身。订单号生成规则：OD{YYYYMMDD}{3 位序号}。

实现约定：
    - 金额一律整数分；时间出参统一 to_iso()（+08:00）；
    - 所有写操作：校验状态（不一致 409/3002）→ 条件 UPDATE（防并发）→ 追加 order_timeline
      → await audit(...) → 返回；写接口统一 admin_guard(write=True)；
    - 列表 counts 必须补齐 10 个状态键 + all（无记录补 0）；
    - 框架未提供显式事务，退款/结算按「先写流水、再改状态」顺序执行（与旧 Node 版一致）。
"""
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request

from databases.plugin import DatabasePlugin
from transaction.console import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_PARAM,
    ApiError,
    admin_guard,
    audit,
    now_dt,
    ok,
    paged,
    parse_page,
    require_db,
    to_iso,
)

# --------------------------------------------------------------------------- #
# 常量：订单状态、展示文案、状态机约束
# --------------------------------------------------------------------------- #
#: 订单状态 → 中文文案（前端 counts 与 statusLabel 共用，顺序即前端分栏顺序）
STATUS_LABELS: Dict[str, str] = {
    "pending_pay": "待付款",
    "material_review": "材料审核",
    "in_progress": "履约中",
    "delivered": "已交付",
    "accepted": "待结算",
    "settled": "已结算",
    "cancelled": "已取消",
    "refunding": "退款中",
    "refunded": "已退款",
    "disputed": "争议中",
}
ORDER_STATUSES = tuple(STATUS_LABELS.keys())
#: 终态：不可再强制取消
TERMINAL_STATUSES = ("settled", "cancelled", "refunded")
#: 可退款状态（清单 §6.7；待付款不在此列，直接取消即可）
REFUNDABLE_STATUSES = ("material_review", "in_progress", "delivered", "accepted", "disputed")
#: UPDATE orders 时允许通过 extra 写入的字段白名单（防 SQL 拼接注入）
UPDATABLE_ORDER_FIELDS = frozenset(
    {
        "status",
        "paid_at",
        "delivered_at",
        "accepted_at",
        "settled_at",
        "cancelled_at",
        "refunded_at",
        "commission_amount",
        "settle_amount",
    }
)

#: 履约流程（详情 flow[] 按状态实时计算，不单独建表）
FLOW_STEPS = (
    ("下单支付", "pending_pay", "订单创建并完成支付"),
    ("材料审核", "material_review", "飞手提交飞行报备，等待平台审核"),
    ("履约中", "in_progress", "飞手执行航拍任务"),
    ("已交付", "delivered", "飞手提交交付物，等待雇主观飞验收"),
    ("待结算", "accepted", "验收通过，等待平台结算"),
    ("已结算", "settled", "平台完成结算"),
)
#: 异常状态补充节点：状态 → (标题, 描述)
ABNORMAL_STEPS = {
    "cancelled": ("已取消", "订单已取消"),
    "refunding": ("退款中", "退款处理中"),
    "refunded": ("已退款", "订单已退款"),
    "disputed": ("争议中", "订单进入争议处理"),
}

# --------------------------------------------------------------------------- #
# 表结构（权威定义见 docs/后端实现约定.md §6，只增不删不改）
# --------------------------------------------------------------------------- #
TABLES: Dict[str, List[tuple]] = {
    "orders": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("order_no", "VARCHAR(32) NOT NULL UNIQUE"),
        ("title", "VARCHAR(128)"),
        ("status", "VARCHAR(24) DEFAULT 'pending_pay'"),
        ("amount", "INT NOT NULL DEFAULT 0"),
        ("commission_rate", "DECIMAL(6,4) DEFAULT 0.1"),
        ("commission_amount", "INT DEFAULT 0"),
        ("settle_amount", "INT DEFAULT 0"),
        ("city", "VARCHAR(64)"),
        ("district", "VARCHAR(64)"),
        ("address", "VARCHAR(255)"),
        ("client_id", "INT"),
        ("pilot_id", "INT"),
        ("remark", "VARCHAR(255)"),
        ("created_at", "DATETIME"),
        ("paid_at", "DATETIME"),
        ("delivered_at", "DATETIME"),
        ("accepted_at", "DATETIME"),
        ("settled_at", "DATETIME"),
        ("cancelled_at", "DATETIME"),
        ("refunded_at", "DATETIME"),
    ],
    "order_timeline": [
        ("id", "BIGINT AUTO_INCREMENT PRIMARY KEY"),
        ("order_id", "INT"),
        ("from_status", "VARCHAR(24)"),
        ("to_status", "VARCHAR(24)"),
        ("action", "VARCHAR(32)"),
        ("remark", "VARCHAR(255)"),
        ("operator_type", "VARCHAR(16) DEFAULT 'admin'"),
        ("operator_id", "INT"),
        ("created_at", "DATETIME"),
    ],
    "order_reports": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("order_id", "INT NOT NULL UNIQUE"),
        ("flight_time", "VARCHAR(64)"),
        ("location", "VARCHAR(255)"),
        ("drone_model", "VARCHAR(64)"),
        ("created_at", "DATETIME"),
    ],
    "order_payments": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("order_id", "INT"),
        ("type", "VARCHAR(16) DEFAULT 'pay'"),
        ("amount", "INT DEFAULT 0"),
        ("status", "VARCHAR(16) DEFAULT 'success'"),
        ("provider", "VARCHAR(32)"),
        ("out_trade_no", "VARCHAR(64)"),
        ("paid_at", "DATETIME"),
    ],
    "order_deliverables": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("order_id", "INT"),
        ("code", "VARCHAR(32)"),
        ("name", "VARCHAR(64)"),
        ("required", "TINYINT(1) DEFAULT 1"),
        ("status", "VARCHAR(16) DEFAULT 'pending'"),
        ("confirmed_at", "DATETIME"),
    ],
    "order_deliverable_files": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("deliverable_id", "INT"),
        ("file_key", "VARCHAR(255)"),
        ("file_name", "VARCHAR(255)"),
        ("size", "BIGINT DEFAULT 0"),
        ("created_at", "DATETIME"),
    ],
    "conversations": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("order_id", "INT"),
        ("created_at", "DATETIME"),
    ],
    "messages": [
        ("id", "BIGINT AUTO_INCREMENT PRIMARY KEY"),
        ("conv_id", "INT"),
        ("sender_id", "INT"),
        ("sender_name", "VARCHAR(64)"),
        ("type", "VARCHAR(16) DEFAULT 'text'"),
        ("content", "TEXT"),
        ("created_at", "DATETIME"),
    ],
}


# --------------------------------------------------------------------------- #
# 通用小工具（参数校验 / JSON 请求体）
# --------------------------------------------------------------------------- #
async def _json_body(request: Request) -> dict:
    """读取 JSON 请求体；无请求体视为 {}，非法 JSON 抛 1001"""
    try:
        raw = await request.body()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(CODE_PARAM, "请求体不是合法的 JSON", 400)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ApiError(CODE_PARAM, "请求体必须是 JSON 对象", 400)
    return payload


def _required_text(body: dict, field: str, label: str, min_len: int, max_len: int) -> str:
    """必填文本：去首尾空格后校验长度（reason 等）"""
    value = body.get(field)
    if value is None:
        raise ApiError(CODE_PARAM, f"{label}不能为空", 400)
    if not isinstance(value, str):
        raise ApiError(CODE_PARAM, f"{label}格式不正确", 400)
    text = value.strip()
    if len(text) < min_len:
        raise ApiError(CODE_PARAM, f"{label}至少需要 {min_len} 个字", 400)
    if len(text) > max_len:
        raise ApiError(CODE_PARAM, f"{label}不能超过 {max_len} 个字", 400)
    return text


def _optional_text(
    body: dict, field: str, label: str, max_len: int, default: Optional[str] = None
) -> Optional[str]:
    """可选文本：空值/空串回落默认值；超过 max_len 抛 1001"""
    value = body.get(field)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ApiError(CODE_PARAM, f"{label}格式不正确", 400)
    text = value.strip()
    if not text:
        return default
    if len(text) > max_len:
        raise ApiError(CODE_PARAM, f"{label}不能超过 {max_len} 个字", 400)
    return text


def _parse_int_amount(value: Any, label: str, minimum: int, maximum: int) -> int:
    """解析整数分（允许 JSON number / 数字字符串，拒绝小数与布尔）"""
    amount: Optional[int] = None
    if isinstance(value, bool) or value is None:
        amount = None
    elif isinstance(value, int):
        amount = value
    elif isinstance(value, float):
        if value.is_integer():
            amount = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            amount = int(text)
    if amount is None:
        raise ApiError(CODE_PARAM, f"{label}必须是整数分", 400)
    if amount < minimum:
        raise ApiError(CODE_PARAM, f"{label}不能小于 {minimum}", 400)
    if amount > maximum:
        raise ApiError(CODE_PARAM, f"{label}不能大于 {maximum}", 400)
    return amount


def _require_order_id(order_id: Any) -> int:
    """路径参数订单 id 校验（>=1）"""
    try:
        value = int(order_id)
    except (TypeError, ValueError):
        raise ApiError(CODE_PARAM, "订单 id 不合法", 400)
    if value < 1:
        raise ApiError(CODE_PARAM, "订单 id 不合法", 400)
    return value


def _status_label(status: Any) -> Optional[str]:
    """订单状态中文文案（未知状态原样返回，避免前端出现空白）"""
    if status is None:
        return None
    return STATUS_LABELS.get(str(status), str(status))


def _yuan_text(amount: Any) -> str:
    """分 → 元字符串（仅用于时间线/裁定的备注文案）"""
    try:
        return f"{int(amount or 0) / 100:.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _flow_done_at(index: int, order: dict, timeline_rows: List[dict]) -> Optional[str]:
    """flow 第 index 步的完成时间（优先订单时间列，其次时间线）"""
    raw: Any = None
    if index == 0:
        raw = order.get("created_at")
    elif index == 1:
        for row in timeline_rows:
            if row.get("to_status") == "in_progress":
                raw = row.get("created_at")
                break
        if raw is None:
            raw = order.get("paid_at")
    elif index == 2:
        raw = order.get("delivered_at")
    elif index == 3:
        raw = order.get("accepted_at")
    else:  # 4 / 5 都用结算时间
        raw = order.get("settled_at")
    return to_iso(raw)


def _build_flow(order: dict, timeline_rows: List[dict]) -> List[dict]:
    """按当前状态 + 时间线实时计算详情 flow[]（不落库）"""
    status = str(order.get("status") or "")
    normal_index = next(
        (i for i, (_, key, _) in enumerate(FLOW_STEPS) if key == status), None
    )
    steps: List[dict] = []

    if normal_index is not None:
        for i, (title, key, desc) in enumerate(FLOW_STEPS):
            if status == "settled":
                step_status = "done" if i <= normal_index else "pending"
            elif i < normal_index:
                step_status = "done"
            elif i == normal_index:
                step_status = "active"
            else:
                step_status = "pending"
            steps.append(
                {
                    "seq": i + 1,
                    "title": title,
                    "desc": desc,
                    "status": step_status,
                    "done_at": _flow_done_at(i, order, timeline_rows) if step_status == "done" else None,
                }
            )
        return steps

    # 异常状态：从时间线里找最后一个走到过的正常节点
    reached = -1
    for row in timeline_rows:
        for candidate in (row.get("from_status"), row.get("to_status")):
            for i, (_, key, _) in enumerate(FLOW_STEPS):
                if candidate == key and i > reached:
                    reached = i
    for i, (title, _, desc) in enumerate(FLOW_STEPS):
        step_status = "done" if i <= reached else "pending"
        steps.append(
            {
                "seq": i + 1,
                "title": title,
                "desc": desc,
                "status": step_status,
                "done_at": _flow_done_at(i, order, timeline_rows) if step_status == "done" else None,
            }
        )
    abnormal = ABNORMAL_STEPS.get(status)
    if abnormal is not None:
        title, desc = abnormal
        terminal = status in ("cancelled", "refunded")
        done_at = None
        if terminal:
            done_at = to_iso(order.get("cancelled_at") or order.get("refunded_at"))
        steps.append(
            {
                "seq": len(steps) + 1,
                "title": title,
                "desc": desc,
                "status": "done" if terminal else "active",
                "done_at": done_at,
            }
        )
    return steps


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #
class ConsoleOrdersPlugin(DatabasePlugin):
    """管理台 - 订单管理（列表 / 详情 / 9 条状态机接口）"""

    prefix = "/admin"

    def __init__(self):
        super().__init__(
            name="console_orders",
            description="管理台 - 订单管理（列表/详情/状态机干预）",
        )
        # 已确认存在的跨插件表（只缓存 True，避免 console_users 后初始化时误判）
        self._table_ok: set = set()

    # ── 生命周期 ────────────────────────────────────────────────

    async def initialize(self, value) -> bool:
        if not await super().initialize(value):
            return False
        await self.ensure_tables(TABLES)
        return True

    def register_routes(self, router: APIRouter) -> None:
        self._route(router)

    # ── 数据访问 / 公共逻辑 ──────────────────────────────────────

    async def _has_table(self, table: str) -> bool:
        """探测跨插件表是否存在（console_users 的表可能尚未创建）"""
        if table in self._table_ok:
            return True
        database = self.database
        if database is None:
            return False
        try:
            exists = bool(await database.table_exists(table))
        except Exception as exc:  # 探测失败不影响订单主流程
            self.logger.warning(f"[{self.name}] 检查数据表 {table} 是否存在失败: {exc}")
            return False
        if exists:
            self._table_ok.add(table)
        return exists

    async def _load_order(self, order_id: int) -> dict:
        """按 id 取订单，不存在抛 404/3001"""
        row = await self.fetch_one("SELECT * FROM orders WHERE id = %s", (int(order_id),))
        if not row:
            raise ApiError(CODE_NOT_FOUND, "订单不存在", 404)
        return row

    async def _append_timeline(
        self,
        order_id: int,
        from_status: Optional[str],
        to_status: str,
        action: str,
        remark: Optional[str],
        admin: dict,
        created_at=None,
    ) -> None:
        """追加一条订单时间线（operator_type 固定 admin）"""
        # order_timeline.remark 只有 255 字符：强制取消（≤300 字）等长文本会被截断，
        # 完整原因仍写在审计日志 detail 里。
        timeline_remark = str(remark)[:255] if remark else remark
        await self.execute(
            "INSERT INTO order_timeline "
            "(order_id, from_status, to_status, action, remark, operator_type, operator_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'admin', %s, %s)",
            (
                int(order_id),
                from_status,
                to_status,
                action,
                timeline_remark,
                int(admin.get("id") or 0),
                created_at or now_dt(),
            ),
        )

    async def _transit(
        self,
        order: dict,
        to_status: str,
        action: str,
        remark: Optional[str],
        admin: dict,
        extra: Optional[dict] = None,
        created_at=None,
    ) -> str:
        """条件 UPDATE 订单状态（防并发）+ 写时间线，返回原状态

        状态被其它请求改动时（rowcount != 1）返回 409/3002。
        """
        current = order.get("status")
        now = created_at or now_dt()
        sets = ["status = %s"]
        params: List[Any] = [to_status]
        for key, value in (extra or {}).items():
            if key not in UPDATABLE_ORDER_FIELDS:
                raise ValueError(f"不允许更新的订单字段: {key}")
            sets.append(f"`{key}` = %s")
            params.append(value)
        params.extend([int(order["id"]), current])
        changed = await self.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id = %s AND status = %s",
            tuple(params),
        )
        if int(changed) != 1:
            raise ApiError(CODE_CONFLICT, "订单状态已变化，请刷新后重试", 409)
        await self._append_timeline(
            order["id"], current, to_status, action, remark, admin, now
        )
        return str(current)

    async def _paid_and_refunded(self, order_id: int) -> tuple:
        """统计订单已支付 / 已退款金额（分）"""
        row = await self.fetch_one(
            "SELECT "
            "COALESCE(SUM(CASE WHEN type = 'pay' AND status = 'success' THEN amount ELSE 0 END), 0) AS paid, "
            "COALESCE(SUM(CASE WHEN type = 'refund' AND status = 'success' THEN amount ELSE 0 END), 0) AS refunded "
            "FROM order_payments WHERE order_id = %s",
            (int(order_id),),
        ) or {}
        return int(row.get("paid") or 0), int(row.get("refunded") or 0)

    async def _pilot_balance(self, pilot_id: Any, fallback: int) -> int:
        """飞手累计结算余额（无钱包表，用结算流水汇总近似；无飞手时回落本次金额）"""
        if not pilot_id:
            return fallback
        row = await self.fetch_one(
            "SELECT COALESCE(SUM(p.amount), 0) AS balance "
            "FROM order_payments p JOIN orders o ON o.id = p.order_id "
            "WHERE o.pilot_id = %s AND p.type = 'settle' AND p.status = 'success'",
            (int(pilot_id),),
        ) or {}
        return int(row.get("balance") or 0)

    async def _insert_payment(
        self,
        order_id: int,
        pay_type: str,
        amount: int,
        out_trade_no: str,
        provider: str = "mock",
        paid_at=None,
    ) -> None:
        """写一条订单资金流水"""
        now = paid_at or now_dt()
        await self.execute(
            "INSERT INTO order_payments "
            "(order_id, type, amount, status, provider, out_trade_no, paid_at) "
            "VALUES (%s, %s, %s, 'success', %s, %s, %s)",
            (int(order_id), pay_type, int(amount), provider, out_trade_no, now),
        )

    async def _user_row(self, user_id: Any) -> Optional[dict]:
        """读 platform_users 基础信息（表未创建时返回 None）"""
        if not user_id or not await self._has_table("platform_users"):
            return None
        try:
            return await self.fetch_one(
                "SELECT id, nickname FROM platform_users WHERE id = %s",
                (int(user_id),),
            )
        except Exception as exc:
            self.logger.warning(f"[{self.name}] 读取 platform_users 失败: {exc}")
            return None

    async def _client_block(self, client_id: Any) -> dict:
        """详情 client{id,name,avatar,kind}（优先 client_profiles 公司名/类型）"""
        user = await self._user_row(client_id)
        name = (user or {}).get("nickname")
        kind = "personal"
        if client_id and await self._has_table("client_profiles"):
            try:
                profile = await self.fetch_one(
                    "SELECT company_name, kind FROM client_profiles WHERE user_id = %s",
                    (int(client_id),),
                ) or {}
                name = profile.get("company_name") or name
                kind = profile.get("kind") or kind
            except Exception as exc:
                self.logger.warning(f"[{self.name}] 读取 client_profiles 失败: {exc}")
        return {
            "id": int(client_id) if client_id else None,
            "name": name,
            "avatar": "",
            "kind": kind,
        }

    async def _pilot_block(self, pilot_id: Any) -> dict:
        """详情 pilot{id,name,avatar,hasLicense,rating}（执照/评分取 pilot_profiles）"""
        user = await self._user_row(pilot_id)
        name = (user or {}).get("nickname")
        has_license = False
        rating = 0
        if pilot_id and await self._has_table("pilot_profiles"):
            try:
                profile = await self.fetch_one(
                    "SELECT license_status, rating FROM pilot_profiles WHERE user_id = %s",
                    (int(pilot_id),),
                ) or {}
                has_license = str(profile.get("license_status") or "") == "approved"
                if profile.get("rating") is not None:
                    rating = float(profile["rating"])
            except Exception as exc:
                self.logger.warning(f"[{self.name}] 读取 pilot_profiles 失败: {exc}")
        return {
            "id": int(pilot_id) if pilot_id else None,
            "name": name,
            "avatar": "",
            "hasLicense": has_license,
            "rating": rating,
        }

    async def _deliverables(self, order_id: int) -> List[dict]:
        """详情 deliverables[{...,files[]}]"""
        rows = await self.fetch_all(
            "SELECT id, code, name, required, status, confirmed_at "
            "FROM order_deliverables WHERE order_id = %s ORDER BY id ASC",
            (int(order_id),),
        )
        if not rows:
            return []
        deliverable_ids = [int(row["id"]) for row in rows]
        placeholders = ", ".join(["%s"] * len(deliverable_ids))
        file_rows = await self.fetch_all(
            f"SELECT id, deliverable_id, file_key, file_name, size, created_at "
            f"FROM order_deliverable_files WHERE deliverable_id IN ({placeholders}) ORDER BY id ASC",
            tuple(deliverable_ids),
        )
        files_map: Dict[int, List[dict]] = {}
        for file_row in file_rows:
            files_map.setdefault(int(file_row["deliverable_id"]), []).append(
                {
                    "id": int(file_row["id"]),
                    "fileKey": file_row.get("file_key"),
                    "fileName": file_row.get("file_name"),
                    "size": int(file_row.get("size") or 0),
                    "createdAt": to_iso(file_row.get("created_at")),
                }
            )
        return [
            {
                "id": int(row["id"]),
                "code": row.get("code"),
                "name": row.get("name"),
                "required": bool(row.get("required")),
                "status": row.get("status"),
                "confirmedAt": to_iso(row.get("confirmed_at")),
                "files": files_map.get(int(row["id"]), []),
            }
            for row in rows
        ]

    async def _payments(self, order_id: int) -> List[dict]:
        """详情 payments[]（字段照清单 §6.2 示例）"""
        rows = await self.fetch_all(
            "SELECT type, amount, status, provider, out_trade_no, paid_at "
            "FROM order_payments WHERE order_id = %s ORDER BY id ASC",
            (int(order_id),),
        )
        return [
            {
                "type": row.get("type"),
                "amount": int(row.get("amount") or 0),
                "status": row.get("status"),
                "provider": row.get("provider"),
                "out_trade_no": row.get("out_trade_no"),
                "paid_at": to_iso(row.get("paid_at")),
            }
            for row in rows
        ]

    async def _messages(self, conversation_ids: List[int]) -> List[dict]:
        """详情 messages[]（最多 200 条，按 id 升序）"""
        if not conversation_ids:
            return []
        placeholders = ", ".join(["%s"] * len(conversation_ids))
        rows = await self.fetch_all(
            f"SELECT id, conv_id, sender_id, sender_name, type, content, created_at "
            f"FROM messages WHERE conv_id IN ({placeholders}) ORDER BY id ASC LIMIT 200",
            tuple(conversation_ids),
        )
        return [
            {
                "id": int(row["id"]),
                "conv_id": int(row["conv_id"]) if row.get("conv_id") else None,
                "sender_id": int(row["sender_id"]) if row.get("sender_id") else None,
                "type": row.get("type"),
                "content": row.get("content"),
                "created_at": to_iso(row.get("created_at")),
                "sender_name": row.get("sender_name"),
            }
            for row in rows
        ]

    async def generate_order_no(self) -> str:
        """生成订单号：OD{YYYYMMDD}{3 位序号}（按当天已有最大序号 +1）"""
        prefix = f"OD{now_dt().strftime('%Y%m%d')}"
        row = await self.fetch_one(
            "SELECT order_no FROM orders WHERE order_no LIKE %s ORDER BY order_no DESC LIMIT 1",
            (f"{prefix}%",),
        )
        sequence = 0
        if row and row.get("order_no"):
            suffix = str(row["order_no"])[len(prefix):]
            if suffix.isdigit():
                sequence = int(suffix)
        return f"{prefix}{sequence + 1:03d}"

    # ── 路由 ────────────────────────────────────────────────────

    def _route(self, router: APIRouter) -> None:
        """注册 9 条订单管理接口（router 已带 /admin 前缀）"""

        # ------------------------------------------------------------------ #
        # 16. 订单列表
        # ------------------------------------------------------------------ #
        @router.get("/orders")
        async def list_orders(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            page, page_size = parse_page(request)
            query = request.query_params
            status = str(query.get("status") or "").strip()
            group = str(query.get("group") or "").strip()
            keyword = str(query.get("keyword") or "").strip()

            where: List[str] = []
            params: List[Any] = []
            if keyword:
                where.append("(o.order_no LIKE %s OR o.title LIKE %s)")
                like = f"%{keyword}%"
                params.extend([like, like])
            if status and status != "all":
                if status not in STATUS_LABELS:
                    raise ApiError(CODE_PARAM, "订单状态不合法", 400)
                where.append("o.status = %s")
                params.append(status)
            if group:
                if group != "pending_payment":
                    raise ApiError(CODE_PARAM, "group 仅支持 pending_payment", 400)
                where.append("o.status IN ('pending_pay', 'material_review')")

            where_sql = (" WHERE " + " AND ".join(where)) if where else ""

            total_row = await self.fetch_one(
                f"SELECT COUNT(*) AS total FROM orders o{where_sql}", tuple(params)
            )
            total = int((total_row or {}).get("total") or 0)

            users_ready = await self._has_table("platform_users")
            fields = (
                "o.id, o.order_no, o.title, o.amount, o.commission_amount, o.settle_amount, "
                "o.status, o.client_id, o.pilot_id, o.created_at, o.paid_at"
            )
            if users_ready:
                fields += ", cu.nickname AS client_name, pu.nickname AS pilot_name"
                from_sql = (
                    " FROM orders o "
                    "LEFT JOIN platform_users cu ON cu.id = o.client_id "
                    "LEFT JOIN platform_users pu ON pu.id = o.pilot_id"
                )
            else:
                fields += ", NULL AS client_name, NULL AS pilot_name"
                from_sql = " FROM orders o"

            offset = (page - 1) * page_size
            rows = await self.fetch_all(
                f"SELECT {fields}{from_sql}{where_sql} ORDER BY o.id DESC LIMIT %s OFFSET %s",
                tuple(params) + (page_size, offset),
            )

            items = [
                {
                    "id": int(row["id"]),
                    "orderNo": row.get("order_no"),
                    "title": row.get("title"),
                    "amount": int(row.get("amount") or 0),
                    "commissionAmount": int(row.get("commission_amount") or 0),
                    "settleAmount": int(row.get("settle_amount") or 0),
                    "status": row.get("status"),
                    "statusLabel": _status_label(row.get("status")),
                    "clientName": row.get("client_name"),
                    "pilotName": row.get("pilot_name"),
                    "createdAt": to_iso(row.get("created_at")),
                    "paidAt": to_iso(row.get("paid_at")),
                }
                for row in rows
            ]

            # 分栏计数：遍历全部 10 个状态键，无记录补 0（前端直接取值不做兜底）
            counts: Dict[str, int] = {key: 0 for key in ORDER_STATUSES}
            count_rows = await self.fetch_all(
                "SELECT status, COUNT(*) AS c FROM orders GROUP BY status"
            )
            for row in count_rows:
                key = str(row.get("status") or "")
                if key in counts:
                    counts[key] = int(row.get("c") or 0)
            all_row = await self.fetch_one("SELECT COUNT(*) AS c FROM orders")
            counts["all"] = int((all_row or {}).get("c") or 0)

            result = paged(items, total, page, page_size)
            result["counts"] = counts
            return ok(result)

        # ------------------------------------------------------------------ #
        # 17. 订单详情（整包）
        # ------------------------------------------------------------------ #
        @router.get("/orders/{order_id}")
        async def get_order_detail(
            order_id: int, request: Request, admin: dict = Depends(admin_guard())
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)

            timeline_rows = await self.fetch_all(
                "SELECT id, from_status, to_status, action, remark, operator_type, operator_id, created_at "
                "FROM order_timeline WHERE order_id = %s ORDER BY id ASC",
                (oid,),
            )
            timeline = [
                {
                    "id": int(row["id"]),
                    "from": row.get("from_status"),
                    "to": row.get("to_status"),
                    "action": row.get("action"),
                    "remark": row.get("remark"),
                    "operatorType": row.get("operator_type"),
                    "at": to_iso(row.get("created_at")),
                }
                for row in timeline_rows
            ]

            report_row = await self.fetch_one(
                "SELECT id, flight_time, location, drone_model FROM order_reports "
                "WHERE order_id = %s LIMIT 1",
                (oid,),
            )
            report = None
            if report_row:
                report = {
                    "id": int(report_row["id"]),
                    "flight_time": report_row.get("flight_time"),
                    "location": report_row.get("location"),
                    "drone_model": report_row.get("drone_model"),
                }

            conversation_rows = await self.fetch_all(
                "SELECT id FROM conversations WHERE order_id = %s ORDER BY id ASC",
                (oid,),
            )
            conversation_ids = [int(row["id"]) for row in conversation_rows]

            city = order.get("city")
            district = order.get("district")
            return ok(
                {
                    "id": int(order["id"]),
                    "orderNo": order.get("order_no"),
                    "title": order.get("title"),
                    "status": order.get("status"),
                    "statusLabel": _status_label(order.get("status")),
                    "amount": int(order.get("amount") or 0),
                    "commissionRate": float(order.get("commission_rate") or 0),
                    "commissionAmount": int(order.get("commission_amount") or 0),
                    "settleAmount": int(order.get("settle_amount") or 0),
                    # 兼容字段（前端 OrderDetail 类型使用，表里没有的字段不返回）
                    "createdAt": to_iso(order.get("created_at")),
                    "paidAt": to_iso(order.get("paid_at")),
                    "deliveredAt": to_iso(order.get("delivered_at")),
                    "acceptedAt": to_iso(order.get("accepted_at")),
                    "settledAt": to_iso(order.get("settled_at")),
                    "addressDetail": order.get("address"),
                    "city": city,
                    "district": district,
                    "area": " ".join([part for part in (city, district) if part]),
                    "client": await self._client_block(order.get("client_id")),
                    "pilot": await self._pilot_block(order.get("pilot_id")),
                    "flow": _build_flow(order, timeline_rows),
                    "timeline": timeline,
                    "report": report,
                    "deliverables": await self._deliverables(oid),
                    "payments": await self._payments(oid),
                    "messages": await self._messages(conversation_ids),
                    "conversationIds": conversation_ids,
                }
            )

        # ------------------------------------------------------------------ #
        # 18. 通过材料审核：material_review → in_progress（必须有飞行报备）
        # ------------------------------------------------------------------ #
        @router.post("/orders/{order_id}/approve-material")
        async def approve_material(
            order_id: int, request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)
            body = await _json_body(request)
            remark = _optional_text(body, "remark", "备注", 200, default="材料审核通过")

            if order.get("status") != "material_review":
                raise ApiError(CODE_CONFLICT, "订单不在材料审核状态", 409)
            report = await self.fetch_one(
                "SELECT id FROM order_reports WHERE order_id = %s LIMIT 1", (oid,)
            )
            if not report:
                raise ApiError(CODE_CONFLICT, "飞手尚未提交飞行报备，不能通过材料审核", 409)

            await self._transit(order, "in_progress", "approve_material", remark, admin)
            await audit(
                request,
                admin,
                module="订单管理",
                action="approve_material",
                target_type="order",
                target_id=oid,
                detail={"remark": remark},
            )
            return ok({"id": oid, "status": "in_progress"})

        # ------------------------------------------------------------------ #
        # 19. 取消待付款订单：pending_pay → cancelled
        # ------------------------------------------------------------------ #
        @router.post("/orders/{order_id}/cancel")
        async def cancel_order(
            order_id: int, request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)
            body = await _json_body(request)
            reason = _optional_text(body, "reason", "原因", 200, default="平台取消")

            if order.get("status") != "pending_pay":
                raise ApiError(CODE_CONFLICT, "仅待付款订单可直接取消，已付款请走退款", 409)

            now = now_dt()
            await self._transit(
                order,
                "cancelled",
                "cancel",
                reason,
                admin,
                extra={"cancelled_at": now},
                created_at=now,
            )
            await audit(
                request,
                admin,
                module="订单管理",
                action="cancel_order",
                target_type="order",
                target_id=oid,
                detail={"reason": reason},
            )
            return ok({"id": oid, "status": "cancelled"})

        # ------------------------------------------------------------------ #
        # 20. 强制取消：非终态均可（reason 5-300 必填）
        # ------------------------------------------------------------------ #
        @router.post("/orders/{order_id}/force-cancel")
        async def force_cancel_order(
            order_id: int, request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)
            body = await _json_body(request)
            reason = _required_text(body, "reason", "原因", 5, 300)

            from_status = str(order.get("status") or "")
            if from_status in TERMINAL_STATUSES:
                raise ApiError(
                    CODE_CONFLICT,
                    f"订单已处于终态（{_status_label(from_status)}），不可取消",
                    409,
                )

            now = now_dt()
            await self._transit(
                order,
                "cancelled",
                "force_cancel",
                f"[强制取消] {reason}",
                admin,
                extra={"cancelled_at": now},
                created_at=now,
            )
            await audit(
                request,
                admin,
                module="订单管理",
                action="force_cancel_order",
                target_type="order",
                target_id=oid,
                detail={"reason": reason, "from": from_status},
            )
            return ok({"id": oid, "status": "cancelled", "from": from_status})

        # ------------------------------------------------------------------ #
        # 21. 强制验收：delivered → accepted（reason 5-300 必填）
        # ------------------------------------------------------------------ #
        @router.post("/orders/{order_id}/force-accept")
        async def force_accept_order(
            order_id: int, request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)
            body = await _json_body(request)
            reason = _required_text(body, "reason", "原因", 5, 300)

            from_status = str(order.get("status") or "")
            if from_status != "delivered":
                raise ApiError(
                    CODE_CONFLICT,
                    f"仅待验收订单可强制验收，当前：{_status_label(from_status)}",
                    409,
                )

            now = now_dt()
            await self._transit(
                order,
                "accepted",
                "force_accept",
                f"[强制验收] {reason}",
                admin,
                extra={"accepted_at": now},
                created_at=now,
            )
            await audit(
                request,
                admin,
                module="订单管理",
                action="force_accept_order",
                target_type="order",
                target_id=oid,
                detail={"reason": reason, "from": from_status},
            )
            return ok({"id": oid, "status": "accepted", "from": from_status})

        # ------------------------------------------------------------------ #
        # 22. 退款：可退款状态 → refunded（默认退可退全额）
        # ------------------------------------------------------------------ #
        @router.post("/orders/{order_id}/refund")
        async def refund_order(
            order_id: int, request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)
            body = await _json_body(request)
            reason = _optional_text(body, "reason", "原因", 200, default="平台退款")

            from_status = str(order.get("status") or "")
            if from_status not in REFUNDABLE_STATUSES:
                raise ApiError(
                    CODE_CONFLICT,
                    f"订单当前为「{_status_label(from_status)}」，不能退款",
                    409,
                )

            paid, refunded = await self._paid_and_refunded(oid)
            # 历史订单可能没有支付流水，此时以订单金额作为可退基数（文档 §6.7「默认全额可退」）
            base = paid if paid > 0 else int(order.get("amount") or 0)
            refundable = base - refunded
            if refundable <= 0:
                raise ApiError(CODE_CONFLICT, "该订单没有可退款金额", 409)

            raw_amount = body.get("amount")
            if raw_amount is None:
                amount = refundable
            else:
                amount = _parse_int_amount(raw_amount, "退款金额", 1, refundable)

            now = now_dt()
            await self._insert_payment(
                oid,
                "refund",
                amount,
                out_trade_no=f"RF-{order.get('order_no') or oid}-{int(now.timestamp() * 1000)}",
                provider="mock",
                paid_at=now,
            )
            # 与旧后台一致：管理员退款后订单直接进入已退款（部分退款同样落 refunded）
            await self._transit(
                order,
                "refunded",
                "refund",
                reason,
                admin,
                extra={"refunded_at": now},
                created_at=now,
            )
            await audit(
                request,
                admin,
                module="订单管理",
                action="refund_order",
                target_type="order",
                target_id=oid,
                detail={"amount": amount, "reason": reason},
            )
            return ok({"orderId": oid, "refunded": amount, "status": "refunded"})

        # ------------------------------------------------------------------ #
        # 23. 手动结算：accepted → settled（只能结算一次）
        # ------------------------------------------------------------------ #
        @router.post("/orders/{order_id}/settle")
        async def settle_order(
            order_id: int, request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)

            from_status = str(order.get("status") or "")
            if from_status != "accepted":
                raise ApiError(
                    CODE_CONFLICT,
                    f"订单当前为「{_status_label(from_status)}」，不能结算",
                    409,
                )
            exists = await self.fetch_one(
                "SELECT id FROM order_payments WHERE order_id = %s "
                "AND type = 'settle' AND status = 'success' LIMIT 1",
                (oid,),
            )
            if exists:
                raise ApiError(CODE_CONFLICT, "该订单已结算，不能重复入账", 409)

            amount = int(order.get("settle_amount") or 0)
            now = now_dt()
            await self._insert_payment(
                oid,
                "settle",
                amount,
                out_trade_no=f"ST-{order.get('order_no') or oid}",
                provider="wallet",
                paid_at=now,
            )
            await self._transit(
                order,
                "settled",
                "settle",
                f"结算 {_yuan_text(amount)} 元",
                admin,
                extra={"settled_at": now},
                created_at=now,
            )
            balance = await self._pilot_balance(order.get("pilot_id"), amount)
            await audit(
                request,
                admin,
                module="订单管理",
                action="settle_order",
                target_type="order",
                target_id=oid,
                detail={"amount": amount},
            )
            return ok({"orderId": oid, "settled": amount, "balance": balance})

        # ------------------------------------------------------------------ #
        # 24. 争议裁定：仅 disputed
        #     result=client → 全额退款给雇主（refunded）
        #     result=pilot  → 飞手无责，回到待结算（accepted）
        #     pilotSharePercent → 按比例分账（settled）
        # ------------------------------------------------------------------ #
        @router.post("/orders/{order_id}/dispute-ruling")
        async def dispute_ruling(
            order_id: int, request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            require_db(self)
            oid = _require_order_id(order_id)
            order = await self._load_order(oid)
            body = await _json_body(request)
            remark = _required_text(body, "remark", "裁定说明", 5, 500)

            from_status = str(order.get("status") or "")
            if from_status != "disputed":
                raise ApiError(CODE_CONFLICT, "订单不在争议状态", 409)

            result = body.get("result")
            percent_raw = body.get("pilotSharePercent")
            if result is not None and percent_raw is not None:
                raise ApiError(CODE_PARAM, "result 与 pilotSharePercent 只能二选一", 400)

            now = now_dt()
            paid, refunded = await self._paid_and_refunded(oid)
            # 可分配金额：已支付 - 已退款；无支付流水时回落订单金额
            gross = max(0, paid - refunded) if paid > 0 else max(0, int(order.get("amount") or 0) - refunded)
            if gross <= 0:
                raise ApiError(CODE_CONFLICT, "该订单没有可分配的金额", 409)

            # ── 按比例分账 ──
            if percent_raw is not None:
                percent = _parse_int_amount(percent_raw, "飞手分成比例", 1, 99)
                # Math.round 口径：四舍五入到整数分
                pilot_gross = (gross * percent + 50) // 100
                refund_amount = gross - pilot_gross
                rate = float(order.get("commission_rate") or 0)
                commission = int(round(pilot_gross * rate))
                settle = pilot_gross - commission

                if refund_amount > 0:
                    await self._insert_payment(
                        oid,
                        "refund",
                        refund_amount,
                        out_trade_no=f"RF-{order.get('order_no') or oid}-SPLIT-{int(now.timestamp() * 1000)}",
                        provider="mock",
                        paid_at=now,
                    )
                if settle > 0:
                    await self._insert_payment(
                        oid,
                        "settle",
                        settle,
                        out_trade_no=f"ST-{order.get('order_no') or oid}-SPLIT",
                        provider="wallet",
                        paid_at=now,
                    )
                # 分成后回写佣金/结算金额（争议单重新按裁定结果计算）
                await self._transit(
                    order,
                    "settled",
                    "split_settle",
                    f"争议裁定：飞手得 {percent}%，雇主退款 {_yuan_text(refund_amount)} 元。{remark}",
                    admin,
                    extra={
                        "commission_amount": commission,
                        "settle_amount": settle,
                        "settled_at": now,
                    },
                    created_at=now,
                )
                balance = await self._pilot_balance(order.get("pilot_id"), settle)
                await audit(
                    request,
                    admin,
                    module="订单管理",
                    action="dispute_ruling_split",
                    target_type="order",
                    target_id=oid,
                    detail={"remark": remark, "percent": percent},
                )
                return ok(
                    {
                        "id": oid,
                        "status": "settled",
                        "pilotSharePercent": percent,
                        "settled": settle,
                        "refunded": refund_amount,
                        "balance": balance,
                    }
                )

            # ── 全额判给一方 ──
            if result is None:
                raise ApiError(CODE_PARAM, "请提供 result 或 pilotSharePercent", 400)
            if result not in ("client", "pilot"):
                raise ApiError(CODE_PARAM, "裁定结果仅支持 client / pilot", 400)

            if result == "client":
                refundable = gross
                await self._insert_payment(
                    oid,
                    "refund",
                    refundable,
                    out_trade_no=f"RF-{order.get('order_no') or oid}-RULING-{int(now.timestamp() * 1000)}",
                    provider="mock",
                    paid_at=now,
                )
                await self._transit(
                    order,
                    "refunded",
                    "rule_to_client",
                    f"争议裁定退款：{remark}",
                    admin,
                    extra={"refunded_at": now},
                    created_at=now,
                )
                await audit(
                    request,
                    admin,
                    module="订单管理",
                    action="dispute_ruling_client",
                    target_type="order",
                    target_id=oid,
                    detail={"remark": remark, "refunded": refundable},
                )
                return ok({"id": oid, "status": "refunded", "refunded": refundable})

            # result == 'pilot'：飞手无责，订单回到待结算，等待结算
            await self._transit(
                order,
                "accepted",
                "rule_to_pilot",
                f"争议裁定：飞手无责。{remark}",
                admin,
                created_at=now,
            )
            await audit(
                request,
                admin,
                module="订单管理",
                action="dispute_ruling_pilot",
                target_type="order",
                target_id=oid,
                detail={"remark": remark},
            )
            return ok({"id": oid, "status": "accepted"})


__all__ = ["ConsoleOrdersPlugin", "TABLES", "STATUS_LABELS"]
