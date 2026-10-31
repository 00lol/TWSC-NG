# -*- coding:utf-8 -*-
"""管理台 - 运营插件（看板 / 运营参数 / 对接配置）

负责接口：
- 5-7   GET  /admin/stats/overview、/admin/stats/trend、/admin/content/summary
- 9-11  GET  /admin/params、GET /admin/params/history、PUT /admin/params
- 50-53 GET  /admin/integrations、PUT /admin/integrations/:service/config、
        PUT  /admin/integrations/:service/enabled、POST /admin/integrations/:service/detect

统计口径读其它插件建的表（orders / platform_users / demands / works / content_reports /
activities / notices / verify_records），这些表可能还没建：统一 table_exists 判断，
不存在按空数据返回 0，绝不 500。写接口一律 admin_guard(write=True) + audit，
参数修改仅 super，且写历史 + 追加一条指标快照（触发事件文案见 docs §7）。
"""
import json
import re
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from databases.plugin import DatabasePlugin
from transaction.console import (
    ApiError,
    CODE_NOT_FOUND,
    CODE_PARAM,
    admin_guard,
    audit,
    jsonable,
    now_dt,
    now_iso,
    ok,
    require_db,
    to_int,
    to_iso,
)

# 本插件的 3 张表（docs §6）
TABLES = {
    "console_params": [
        ("key", "VARCHAR(64) PRIMARY KEY"),
        ("label", "VARCHAR(64)"),
        ("unit", "VARCHAR(16)"),
        ("value_type", "VARCHAR(8) DEFAULT 'float'"),
        ("value_num", "DECIMAL(12,4) DEFAULT 0"),
        ("rule_text", "VARCHAR(255)"),
    ],
    "console_param_history": [
        ("id", "BIGINT AUTO_INCREMENT PRIMARY KEY"),
        ("key", "VARCHAR(64)"),
        ("label", "VARCHAR(64)"),
        ("old_value", "VARCHAR(64)"),
        ("new_value", "VARCHAR(64)"),
        # 前端 ParamUpdateBody 必填 reason、ParamHistoryItem 直接展示 reason
        ("reason", "VARCHAR(255)"),
        ("changed_by", "INT"),
        ("changed_by_name", "VARCHAR(64)"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    "console_integrations": [
        ("service", "VARCHAR(16) PRIMARY KEY"),
        ("enabled", "TINYINT(1) DEFAULT 0"),
        ("config", "JSON"),
        ("status", "VARCHAR(16) DEFAULT 'unset'"),
        ("fail_reason", "VARCHAR(255)"),
        ("checked_at", "DATETIME"),
    ],
}

#: 固定 7 个运营参数：(key, label, unit, value_type, 默认值, 生效规则)
PARAM_DEFS = [
    ("commission_rate", "佣金比例", "", "float", 0.1, "仅对之后新成交的订单生效"),
    ("settle_days", "验收后结算", "天", "int", 1, "T+n，影响新验收订单"),
    ("auto_accept_days", "自动验收", "天", "int", 1, "交付后超时未操作自动验收"),
    ("pending_pay_minutes", "待付款超时", "分钟", "int", 15, "超时未支付订单自动取消"),
    ("application_expire_days", "报名有效期", "天", "int", 7, "需求报名超时后不可申请"),
    ("recommend_limit", "推荐位数量", "个", "int", 10, "首页推荐位展示条数，立即生效"),
    ("withdraw_min", "最低提现", "分", "int", 100, "低于该额度的提现申请不允许提交，立即生效"),
]

#: 参数取值区间（防止误填把业务参数改成天文数字）
PARAM_LIMITS = {
    "commission_rate": (0.0001, 1.0),
    "settle_days": (0, 30),
    "auto_accept_days": (0, 30),
    "pending_pay_minutes": (1, 1440),
    "application_expire_days": (1, 90),
    "recommend_limit": (0, 100),
    "withdraw_min": (0, 10000000),
}

#: 参数页顶部说明（前端 alert 展示）
PARAM_RULE_TEXT = (
    "修改后只对之后新成交的订单生效；结算周期与自动验收天数影响新验收订单；"
    "待付款超时、报名有效期、推荐位数量与最低提现额度即时生效；每次修改都会记录历史并生成指标快照。"
)

#: 固定 4 个对接服务（字段与接口清单 §10.1 一致）
INTEGRATIONS = {
    "wxpay": {
        "label": "微信支付",
        "fields": ("mchid", "apiv3Key", "serialNo"),
        "field_labels": {"mchid": "商户号", "apiv3Key": "APIv3 密钥", "serialNo": "证书序列号"},
        "note": "V1 仅配置界面与状态检测，不真实打通收钱",
    },
    "sms": {
        "label": "短信服务",
        "fields": ("secretId", "secretKey", "sign"),
        "field_labels": {"secretId": "SecretId", "secretKey": "SecretKey", "sign": "短信签名"},
        "note": "V1 仅配置界面与格式检测，不真实发送短信",
    },
    "oss": {
        "label": "对象存储",
        "fields": ("bucket", "secretId", "secretKey"),
        "field_labels": {"bucket": "Bucket", "secretId": "SecretId", "secretKey": "SecretKey"},
        "note": "V1 仅配置界面与格式检测，不真实上传文件",
    },
    "push": {
        "label": "消息推送",
        "fields": ("appId", "secret"),
        "field_labels": {"appId": "AppID", "secret": "Secret"},
        "note": "V1 仅配置界面与格式检测，不真实推送",
    },
}

#: 字段格式规则（服务, 字段）-> (正则, 错误提示)；V1 检测只做完整度 + 格式，不真实外呼
FIELD_RULES = {
    ("wxpay", "mchid"): (r"^\d{8,12}$", "商户号需为 8-12 位数字"),
    ("wxpay", "apiv3Key"): (r"^[A-Za-z0-9]{32}$", "APIv3 密钥需为 32 位字母数字"),
    ("wxpay", "serialNo"): (r"^[0-9A-Fa-f]{16,64}$", "证书序列号需为 16-64 位十六进制"),
    ("sms", "secretId"): (r"^\S{8,64}$", "SecretId 需为 8-64 位非空白字符"),
    ("sms", "secretKey"): (r"^\S{8,64}$", "SecretKey 需为 8-64 位非空白字符"),
    ("sms", "sign"): (r"^\S{1,20}$", "短信签名需为 1-20 个字符"),
    ("oss", "bucket"): (r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", "Bucket 需为 3-63 位小写字母/数字/中划线"),
    ("oss", "secretId"): (r"^\S{8,64}$", "SecretId 需为 8-64 位非空白字符"),
    ("oss", "secretKey"): (r"^\S{8,64}$", "SecretKey 需为 8-64 位非空白字符"),
    ("push", "appId"): (r"^\S{4,64}$", "AppID 需为 4-64 位非空白字符"),
    ("push", "secret"): (r"^\S{4,64}$", "Secret 需为 4-64 位非空白字符"),
}

#: 密钥掩码（只回显「已配置（****）」，永不回显明文）
MASKED = "已配置（****）"

#: 进行中订单状态（用于「活跃飞手」口径）
ACTIVE_ORDER_STATUS = ("material_review", "in_progress", "delivered", "accepted")
#: 成交额口径：排除待付款 / 已取消
GMV_EXCLUDED_STATUS = ("pending_pay", "cancelled")


def _as_int(value: Any) -> int:
    """SUM/COUNT 结果统一转 int（MySQL SUM 返回 Decimal，JSON 不能直接序列化）"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_num(value: Any) -> str:
    """参数值转字符串（写入历史：0.1 / 1 / 100，去掉多余小数 0）"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _round_param(value_type: str, number: float) -> Any:
    """按参数类型取整（int 参数返回 int，float 参数保留 4 位）"""
    if value_type == "int":
        return int(round(number))
    return round(float(number), 4)


class ConsoleOpsPlugin(DatabasePlugin):
    """首页看板 + 运营参数 + 对接配置"""

    prefix = "/admin"

    def __init__(self):
        super().__init__(name="console_ops", description="管理台 - 看板/参数/对接配置")

    async def initialize(self, value) -> bool:
        if not await super().initialize(value):
            return False
        await self.ensure_tables(TABLES)
        await self._seed_defaults()
        self.logger.info("[console_ops] 运营插件初始化完成")
        return True

    # ------------------------------------------------------------------ #
    # 种子数据（幂等：INSERT IGNORE，不覆盖已改过的值）
    # ------------------------------------------------------------------ #
    async def _seed_defaults(self) -> None:
        """初始化 7 个运营参数默认值与 4 个对接服务空配置

        只在 async initialize() 里调用（此时 self.database 已注入）；
        数据库不可用时直接跳过，避免刷一串「数据库不可用」ERROR。
        """
        if getattr(self, "database", None) is None:
            self.logger.warning("[console_ops] 数据库不可用，跳过参数/对接服务默认值初始化")
            return
        try:
            for key, label, unit, value_type, default, rule in PARAM_DEFS:
                await self.execute(
                    "INSERT IGNORE INTO console_params "
                    "(`key`, label, unit, value_type, value_num, rule_text) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (key, label, unit, value_type, default, rule),
                )
            for service in INTEGRATIONS:
                await self.execute(
                    "INSERT IGNORE INTO console_integrations "
                    "(service, enabled, config, status) VALUES (%s, 0, %s, 'unset')",
                    (service, "{}"),
                )
            self.logger.info("[console_ops] 运营参数 / 对接服务默认值已就绪")
        except Exception as e:
            self.logger.error(f"[console_ops] 初始化默认数据失败（业务接口会回退到内置默认值）: {e}")

    # ------------------------------------------------------------------ #
    # 通用工具
    # ------------------------------------------------------------------ #
    async def _has_table(self, name: str) -> bool:
        """表是否存在（跨插件表可能还没建）"""
        database = getattr(self, "database", None)
        if database is None:
            return False
        try:
            return bool(await database.table_exists(name))
        except Exception as e:
            self.logger.warning(f"[console_ops] 检查表 {name} 失败: {e}")
            return False

    async def _read_json(self, request: Request) -> dict:
        """读取 JSON 请求体（统一 400/1001）"""
        try:
            payload = await request.json()
        except Exception:
            raise ApiError(CODE_PARAM, "请求体必须是合法 JSON", 400)
        if not isinstance(payload, dict):
            raise ApiError(CODE_PARAM, "请求体必须是 JSON 对象", 400)
        return payload

    async def _count_status(self, table: str, statuses: tuple) -> dict:
        """按状态计数（缺失状态补 0；表不存在返回全 0）"""
        result = {status: 0 for status in statuses}
        if not await self._has_table(table):
            return result
        try:
            rows = await self.fetch_all(
                f"SELECT status, COUNT(*) AS total FROM {table} GROUP BY status"
            )
            for row in rows:
                status = str(row.get("status") or "")
                if status in result:
                    result[status] = _as_int(row.get("total"))
        except Exception as e:
            self.logger.warning(f"[console_ops] 统计表 {table} 状态失败: {e}")
        return result

    # ------------------------------------------------------------------ #
    # 5. 看板总览
    # ------------------------------------------------------------------ #
    async def _stats_overview(self) -> dict:
        """看板指标卡（金额分；无数据源返回 0；payRate 为百分比数值）"""
        stats = {
            "gmv": 0,
            "refunded": 0,
            "netGmv": 0,
            "commission": 0,
            "settled": 0,
            "orderCount": 0,
            "paidCount": 0,
            "demandCount": 0,
            "userCount": 0,
            "newUsersToday": 0,
            "activePilots": 0,
            "settledOrders": 0,
            "payRate": 0,
            "avgOrderAmount": 0,
            "pendingWithdrawals": 0,  # 暂无提现表，固定 0（不造数）
            "pendingAudits": 0,
            "updatedAt": now_iso(),
        }

        if await self._has_table("orders"):
            row = await self.fetch_one(
                "SELECT COUNT(*) AS orderCount, "
                "COUNT(paid_at) AS paidCount, "
                "COALESCE(SUM(CASE WHEN status NOT IN "
                "('pending_pay', 'cancelled') THEN amount ELSE 0 END), 0) AS gmv, "
                "COALESCE(SUM(CASE WHEN status = 'refunded' THEN amount ELSE 0 END), 0) AS refunded, "
                "COALESCE(SUM(CASE WHEN status = 'settled' THEN commission_amount ELSE 0 END), 0) AS commission, "
                "COALESCE(SUM(CASE WHEN status = 'settled' THEN settle_amount ELSE 0 END), 0) AS settled, "
                "COUNT(CASE WHEN status = 'settled' THEN 1 END) AS settledOrders, "
                "COALESCE(ROUND(AVG(CASE WHEN status NOT IN "
                "('pending_pay', 'cancelled') THEN amount END)), 0) AS avgOrderAmount "
                "FROM orders"
            )
            order = row or {}
            stats["orderCount"] = _as_int(order.get("orderCount"))
            stats["paidCount"] = _as_int(order.get("paidCount"))
            stats["gmv"] = _as_int(order.get("gmv"))
            stats["refunded"] = _as_int(order.get("refunded"))
            stats["commission"] = _as_int(order.get("commission"))
            stats["settled"] = _as_int(order.get("settled"))
            stats["settledOrders"] = _as_int(order.get("settledOrders"))
            stats["avgOrderAmount"] = _as_int(order.get("avgOrderAmount"))
            stats["netGmv"] = stats["gmv"] - stats["refunded"]

            # 活跃飞手：当前有进行中订单的飞手去重数
            pilot_row = await self.fetch_one(
                "SELECT COUNT(DISTINCT pilot_id) AS total FROM orders "
                "WHERE pilot_id IS NOT NULL AND status IN "
                "('material_review', 'in_progress', 'delivered', 'accepted')"
            )
            stats["activePilots"] = _as_int((pilot_row or {}).get("total"))

            if stats["orderCount"] > 0:
                stats["payRate"] = round(
                    stats["paidCount"] / stats["orderCount"] * 100, 1
                )

        if await self._has_table("demands"):
            row = await self.fetch_one("SELECT COUNT(*) AS total FROM demands")
            stats["demandCount"] = _as_int((row or {}).get("total"))

        if await self._has_table("platform_users"):
            row = await self.fetch_one("SELECT COUNT(*) AS total FROM platform_users")
            stats["userCount"] = _as_int((row or {}).get("total"))
            today = now_dt().date()
            row = await self.fetch_one(
                "SELECT COUNT(*) AS total FROM platform_users "
                "WHERE created_at >= %s AND created_at < %s",
                (
                    today.strftime("%Y-%m-%d 00:00:00"),
                    (today + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
                ),
            )
            stats["newUsersToday"] = _as_int((row or {}).get("total"))

        if await self._has_table("verify_records"):
            row = await self.fetch_one(
                "SELECT COUNT(*) AS total FROM verify_records WHERE status = 'pending'"
            )
            stats["pendingAudits"] = _as_int((row or {}).get("total"))

        return stats

    # ------------------------------------------------------------------ #
    # 6. 趋势
    # ------------------------------------------------------------------ #
    async def _stats_trend(self, days: int) -> dict:
        """近 N 天趋势（返回 days+1 个点，含今天；无数据补 0）"""
        today = now_dt().date()
        start = today - timedelta(days=days)
        buckets = {}
        if await self._has_table("orders"):
            rows = await self.fetch_all(
                "SELECT DATE(created_at) AS day, COUNT(*) AS orders, "
                "COALESCE(SUM(CASE WHEN status NOT IN "
                "('pending_pay', 'cancelled') THEN amount ELSE 0 END), 0) AS gmv "
                "FROM orders WHERE created_at >= %s AND created_at < %s "
                "GROUP BY day",
                (
                    start.strftime("%Y-%m-%d 00:00:00"),
                    (today + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
                ),
            )
            for row in rows:
                key = row.get("day")
                key = key.isoformat() if hasattr(key, "isoformat") else str(key)[:10]
                buckets[key] = {
                    "orders": _as_int(row.get("orders")),
                    "gmv": _as_int(row.get("gmv")),
                }

        points = []
        for offset in range(days + 1):
            day = start + timedelta(days=offset)
            key = day.isoformat()
            hit = buckets.get(key) or {"orders": 0, "gmv": 0}
            points.append({"date": key, "orders": hit["orders"], "gmv": hit["gmv"]})
        return {"list": points, "days": days}

    # ------------------------------------------------------------------ #
    # 7. 内容/需求/举报/文章 计数
    # ------------------------------------------------------------------ #
    async def _content_summary(self) -> dict:
        """内容看板计数（固定键，缺失补 0）"""
        works = await self._count_status(
            "works", ("pending", "approved", "rejected", "offline")
        )
        demands = await self._count_status(
            "demands", ("recruiting", "closed", "expired")
        )
        reports = await self._count_status(
            "content_reports", ("pending", "handled", "rejected")
        )

        # articles 为合成口径：发布数取「活动（activities）已发布」，
        # 草稿数取「系统消息（notices）草稿」——两块运营内容在前端合并成「文章」一栏。
        published = 0
        draft = 0
        if await self._has_table("activities"):
            row = await self.fetch_one(
                "SELECT COUNT(*) AS total FROM activities WHERE stored_status = 'published'"
            )
            published = _as_int((row or {}).get("total"))
        if await self._has_table("notices"):
            row = await self.fetch_one(
                "SELECT COUNT(*) AS total FROM notices WHERE status = 'draft'"
            )
            draft = _as_int((row or {}).get("total"))

        return {
            "works": works,
            "demands": demands,
            "reports": reports,
            "articles": {"published": published, "draft": draft},
        }

    # ------------------------------------------------------------------ #
    # 9-11. 运营参数
    # ------------------------------------------------------------------ #
    @staticmethod
    def _param_def(key: str) -> Optional[tuple]:
        for item in PARAM_DEFS:
            if item[0] == key:
                return item
        return None

    @staticmethod
    def _cast_value(value: Any, value_type: str) -> Any:
        """DB 里的 DECIMAL -> 前端要的 number（int 参数给 int）"""
        try:
            if value_type == "int":
                return int(round(float(value or 0)))
            return round(float(value or 0), 4)
        except (TypeError, ValueError):
            return 0

    async def _params_response(self) -> dict:
        """参数列表：以固定 7 个 key 为准，DB 里的额外 key 追加在后"""
        rows = []
        if await self._has_table("console_params"):
            try:
                rows = await self.fetch_all(
                    "SELECT `key`, label, unit, value_type, value_num, rule_text "
                    "FROM console_params"
                )
            except Exception as e:
                self.logger.error(f"[console_ops] 读取运营参数失败: {e}")
        by_key = {str(row.get("key")): row for row in rows or []}

        items = []
        for key, label, unit, value_type, default, rule in PARAM_DEFS:
            row = by_key.pop(key, None)
            if row is None:
                # 表缺失或默认值没灌进去：回退内置默认值，保证接口可用
                value = self._cast_value(default, value_type)
                items.append(
                    {"key": key, "label": label, "unit": unit, "value": value, "rule": rule}
                )
                continue
            row_type = str(row.get("value_type") or value_type)
            items.append(
                {
                    "key": key,
                    "label": row.get("label") or label,
                    "unit": row.get("unit") if row.get("unit") is not None else unit,
                    "value": self._cast_value(row.get("value_num"), row_type),
                    "rule": row.get("rule_text") or rule,
                }
            )

        # 数据库里存在的其它参数（预留扩展）也带出去，避免前端看不到
        for key, row in by_key.items():
            row_type = str(row.get("value_type") or "float")
            items.append(
                {
                    "key": key,
                    "label": row.get("label") or key,
                    "unit": row.get("unit") or "",
                    "value": self._cast_value(row.get("value_num"), row_type),
                    "rule": row.get("rule_text") or "",
                }
            )
        return {"ruleText": PARAM_RULE_TEXT, "list": items}

    async def _param_history(self) -> dict:
        """参数修改历史（最多 100 条，倒序）"""
        if not await self._has_table("console_param_history"):
            return {"list": []}
        rows = await self.fetch_all(
            "SELECT id, `key`, label, old_value, new_value, reason, changed_by, "
            "changed_by_name, created_at FROM console_param_history "
            "ORDER BY id DESC LIMIT 100"
        )
        return {
            "list": [
                {
                    "id": _as_int(row.get("id")),
                    "key": row.get("key"),
                    "label": row.get("label"),
                    "old_value": row.get("old_value"),
                    "new_value": row.get("new_value"),
                    "reason": row.get("reason"),
                    "actor_id": row.get("changed_by"),
                    "actor_name": row.get("changed_by_name"),
                    "created_at": to_iso(row.get("created_at")),
                }
                for row in rows
            ]
        }

    async def _update_param(self, request: Request, admin: dict, payload: dict) -> dict:
        """PUT /admin/params：写参数 + 写历史 + 追加指标快照"""
        key = str(payload.get("key") or "").strip()
        definition = self._param_def(key)
        if definition is None:
            raise ApiError(CODE_PARAM, f"不支持的参数 key: {key or '空'}", 400)
        _, label, unit, value_type, default, rule = definition

        # 数值校验
        raw_value = payload.get("value")
        if isinstance(raw_value, bool) or raw_value is None:
            raise ApiError(CODE_PARAM, "参数值必须是数字", 400)
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            raise ApiError(CODE_PARAM, "参数值必须是数字", 400)
        low, high = PARAM_LIMITS[key]
        if not (low <= number <= high):
            raise ApiError(
                CODE_PARAM, f"{label}取值需在 {low} ~ {high} 之间", 400
            )
        new_number = _round_param(value_type, number)

        # 修改原因：可选（清单 §4 未定义请求体，前端可能只发 {key,value}）
        # 缺省记为「参数修改」；一旦传了就按 5-200 字校验。
        raw_reason = payload.get("reason")
        reason = "" if raw_reason is None else str(raw_reason).strip()
        if reason and not (5 <= len(reason) <= 200):
            raise ApiError(CODE_PARAM, "修改原因需 5-200 个字", 400)
        if not reason:
            reason = "参数修改"

        # 改前值
        old_row = None
        if await self._has_table("console_params"):
            old_row = await self.fetch_one(
                "SELECT `key`, label, unit, value_type, value_num, rule_text "
                "FROM console_params WHERE `key` = %s",
                (key,),
            )
        old_type = str((old_row or {}).get("value_type") or value_type)
        old_value = _format_num(
            (old_row or {}).get("value_num")
            if old_row is not None
            else self._cast_value(default, value_type)
        )
        label = (old_row or {}).get("label") or label
        unit = (old_row or {}).get("unit") if (old_row or {}).get("unit") is not None else unit
        rule = (old_row or {}).get("rule_text") or rule
        new_value = _format_num(new_number)

        await self.execute(
            "INSERT INTO console_params "
            "(`key`, label, unit, value_type, value_num, rule_text) "
            "VALUES (%s, %s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE value_num = new.value_num, "
            "label = new.label, unit = new.unit, "
            "value_type = new.value_type, rule_text = new.rule_text",
            (key, label, unit, old_type, new_number, rule),
        )
        await self.execute(
            "INSERT INTO console_param_history "
            "(`key`, label, old_value, new_value, reason, changed_by, changed_by_name, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                key,
                label,
                old_value,
                new_value,
                reason,
                admin.get("id"),
                admin.get("name") or admin.get("username") or "",
                now_dt(),
            ),
        )

        # 关键事件：追加一条指标快照（失败不影响参数修改）
        trigger_event = f"参数修改 {label}: {old_value} → {new_value}"
        await self._append_snapshot(trigger_event)

        await audit(
            request,
            admin,
            module="数据修改",
            action="update_param",
            target_type="param",
            target_id=key,
            detail={"oldValue": old_value, "newValue": new_value, "reason": reason},
        )
        self.logger.info(
            f"[console_ops] 运营参数 {label}({key}) {old_value} -> {new_value}，操作人 {admin.get('id')}"
        )
        return {
            "key": key,
            "label": label,
            "unit": unit,
            "value": new_number,
            "oldValue": old_value,
        }

    async def _append_snapshot(
        self, trigger_event: str, metrics: Optional[dict] = None
    ) -> None:
        """追加指标快照：优先转调 console_report 插件，插件缺失时直接写表兜底"""
        plugins = getattr(getattr(self, "value", None), "plugin", None)
        report = getattr(plugins, "database_business", None) or {}
        report = report.get("console_report") if isinstance(report, dict) else None
        if report is not None and hasattr(report, "add_snapshot"):
            try:
                await report.add_snapshot(trigger_event, metrics)
                return
            except Exception as e:
                self.logger.warning(f"[console_ops] 转调 console_report 生成快照失败: {e}")

        try:
            if not await self._has_table("console_report_snapshots"):
                return
            payload = json.dumps(metrics or {}, ensure_ascii=False)
            await self.execute(
                "INSERT INTO console_report_snapshots (trigger_event, metrics) "
                "VALUES (%s, %s)",
                (str(trigger_event)[:255], payload),
            )
        except Exception as e:
            self.logger.warning(f"[console_ops] 直接写指标快照失败（不影响主流程）: {e}")

    # ------------------------------------------------------------------ #
    # 50-53. 对接配置
    # ------------------------------------------------------------------ #
    @staticmethod
    def _service_meta(service: Any) -> dict:
        """取服务元信息；不存在抛 404"""
        name = str(service or "").strip()
        meta = INTEGRATIONS.get(name)
        if meta is None:
            raise ApiError(CODE_NOT_FOUND, "对接服务不存在", 404)
        return {"service": name, **meta}

    @staticmethod
    def _config_of(row: Optional[dict]) -> dict:
        """DB JSON 列 -> dict（非法值当空配置）"""
        config = jsonable((row or {}).get("config"))
        return config if isinstance(config, dict) else {}

    def _config_mask(self, meta: dict, config: dict) -> dict:
        """配置掩码：只回「已配置（****）」，未配置的字段返回 null（永不回显明文）"""
        return {
            field: (MASKED if str(config.get(field) or "").strip() else None)
            for field in meta["fields"]
        }

    def _detect(self, meta: dict, config: dict) -> tuple:
        """完整度 + 格式检测（V1 不真实外呼），返回 (status, failReason)"""
        fields = meta["fields"]
        labels = meta.get("field_labels") or {}
        filled = {field: str(config.get(field) or "").strip() for field in fields}
        missing = [field for field in fields if not filled[field]]

        if missing and not any(filled.values()):
            return "unset", None  # 完全没配：未配置
        if missing:
            names = "、".join(labels.get(field, field) for field in missing)
            return "failed", f"缺少配置：{names}"

        for field in fields:
            rule = FIELD_RULES.get((meta["service"], field))
            if not rule:
                continue
            pattern, message = rule
            if not re.match(pattern, filled[field]):
                return "failed", f"{labels.get(field, field)}格式不正确：{message}"
        return "ok", None

    async def _integration_rows(self) -> list:
        """固定 4 个服务的配置列表（DB 缺失行时用默认值）"""
        rows = []
        if await self._has_table("console_integrations"):
            try:
                rows = await self.fetch_all(
                    "SELECT service, enabled, config, status, fail_reason, checked_at "
                    "FROM console_integrations"
                )
            except Exception as e:
                self.logger.error(f"[console_ops] 读取对接配置失败: {e}")
        by_service = {str(row.get("service")): row for row in rows or []}

        items = []
        for service, meta in INTEGRATIONS.items():
            row = by_service.get(service) or {}
            config = self._config_of(row)
            items.append(
                {
                    "service": service,
                    "label": meta["label"],
                    "enabled": bool(_as_int(row.get("enabled"))),
                    "status": row.get("status") or "unset",
                    "failReason": row.get("fail_reason"),
                    "checkedAt": to_iso(row.get("checked_at")),
                    "configMask": self._config_mask(meta, config),
                    "note": meta["note"],
                }
            )
        return items

    async def _save_config(
        self, request: Request, admin: dict, service: Any, payload: dict
    ) -> dict:
        """PUT /config：只合并传入字段，其他字段保留；保存即做一次检测"""
        meta = self._service_meta(service)
        config_in = payload.get("config")
        if not isinstance(config_in, dict):
            raise ApiError(CODE_PARAM, "config 必须是对象", 400)

        row = None
        if await self._has_table("console_integrations"):
            row = await self.fetch_one(
                "SELECT config FROM console_integrations WHERE service = %s",
                (meta["service"],),
            )
        merged = dict(self._config_of(row))

        allowed = set(meta["fields"])
        for field, value in config_in.items():
            if field not in allowed:
                raise ApiError(CODE_PARAM, f"不支持的配置项：{field}", 400)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                merged[field] = text
            else:
                merged.pop(field, None)  # 空串 = 清除该字段

        status, fail_reason = self._detect(meta, merged)
        await self.execute(
            "INSERT INTO console_integrations "
            "(service, enabled, config, status, fail_reason, checked_at) "
            "VALUES (%s, 0, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE config = new.config, "
            "status = new.status, fail_reason = new.fail_reason, "
            "checked_at = new.checked_at",
            (
                meta["service"],
                json.dumps(merged, ensure_ascii=False),
                status,
                fail_reason,
                now_dt(),
            ),
        )
        await audit(
            request,
            admin,
            module="对接配置",
            action="save_integration_config",
            target_type="integration",
            target_id=meta["service"],
            detail={"status": status, "fields": sorted(merged.keys())},
        )
        self.logger.info(
            f"[console_ops] 对接服务 {meta['service']} 配置已保存，检测结果 {status}"
        )
        return {"service": meta["service"], "status": status, "failReason": fail_reason}

    async def _toggle_enabled(
        self, request: Request, admin: dict, service: Any, payload: dict
    ) -> dict:
        """PUT /enabled：未配置 / 校验未通过不允许启用"""
        meta = self._service_meta(service)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(CODE_PARAM, "enabled 必须是布尔值", 400)

        row = None
        if await self._has_table("console_integrations"):
            row = await self.fetch_one(
                "SELECT config FROM console_integrations WHERE service = %s",
                (meta["service"],),
            )
        config = self._config_of(row)

        if enabled:
            status, fail_reason = self._detect(meta, config)
            if status == "unset":
                raise ApiError(CODE_PARAM, "尚未完成配置，不能启用", 400)
            if status == "failed":
                raise ApiError(
                    CODE_PARAM, f"配置校验未通过，不能启用：{fail_reason}", 400
                )

        await self.execute(
            "INSERT INTO console_integrations (service, enabled, config) "
            "VALUES (%s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE enabled = new.enabled",
            (meta["service"], 1 if enabled else 0, json.dumps(config, ensure_ascii=False)),
        )
        await audit(
            request,
            admin,
            module="对接配置",
            action="toggle_integration",
            target_type="integration",
            target_id=meta["service"],
            detail={"enabled": enabled},
        )
        self.logger.info(
            f"[console_ops] 对接服务 {meta['service']} 已{'启用' if enabled else '停用'}"
        )
        return {"service": meta["service"], "enabled": enabled}

    async def _detect_service(
        self, request: Request, admin: dict, service: Any
    ) -> dict:
        """POST /detect：只做完整度 + 格式检测，不真实外呼"""
        meta = self._service_meta(service)
        row = None
        if await self._has_table("console_integrations"):
            row = await self.fetch_one(
                "SELECT config FROM console_integrations WHERE service = %s",
                (meta["service"],),
            )
        config = self._config_of(row)
        status, fail_reason = self._detect(meta, config)
        checked_at = now_dt()

        await self.execute(
            "INSERT INTO console_integrations "
            "(service, enabled, config, status, fail_reason, checked_at) "
            "VALUES (%s, 0, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE status = new.status, "
            "fail_reason = new.fail_reason, checked_at = new.checked_at",
            (
                meta["service"],
                json.dumps(config, ensure_ascii=False),
                status,
                fail_reason,
                checked_at,
            ),
        )
        await audit(
            request,
            admin,
            module="对接配置",
            action="detect_integration",
            target_type="integration",
            target_id=meta["service"],
            detail={"status": status, "failReason": fail_reason},
        )
        self.logger.info(
            f"[console_ops] 对接服务 {meta['service']} 检测完成：{status}"
        )
        return {
            "service": meta["service"],
            "status": status,
            "failReason": fail_reason,
            "checkedAt": to_iso(checked_at),
        }

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def register_routes(self, router: APIRouter) -> None:
        self._route(router)

    def _route(self, router: APIRouter) -> None:
        # ---------------- 5-7. 看板 ----------------
        @router.get("/stats/overview", summary="看板指标卡")
        async def stats_overview(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            return ok(await self._stats_overview())

        @router.get("/stats/trend", summary="近 N 天趋势")
        async def stats_trend(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            days = to_int(request.query_params.get("days"), 14)
            days = max(1, min(90, days))
            return ok(await self._stats_trend(days))

        @router.get("/content/summary", summary="内容/需求/举报计数")
        async def content_summary(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            return ok(await self._content_summary())

        # ---------------- 9-11. 运营参数 ----------------
        @router.get("/params", summary="运营参数列表")
        async def params_list(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            return ok(await self._params_response())

        @router.get("/params/history", summary="参数修改历史")
        async def params_history(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            return ok(await self._param_history())

        @router.put("/params", summary="修改运营参数")
        async def params_update(
            request: Request,
            admin: dict = Depends(admin_guard(roles=("super",), write=True)),
        ):
            require_db(self)
            payload = await self._read_json(request)
            return ok(await self._update_param(request, admin, payload))

        # ---------------- 50-53. 对接配置 ----------------
        @router.get("/integrations", summary="对接服务列表")
        async def integrations_list(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            return ok({"list": await self._integration_rows()})

        @router.put("/integrations/{service}/config", summary="保存服务配置")
        async def integration_config(
            service: str,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            payload = await self._read_json(request)
            return ok(await self._save_config(request, admin, service, payload))

        @router.put("/integrations/{service}/enabled", summary="启用/停用服务")
        async def integration_enabled(
            service: str,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            payload = await self._read_json(request)
            return ok(await self._toggle_enabled(request, admin, service, payload))

        @router.post("/integrations/{service}/detect", summary="连通检测")
        async def integration_detect(
            service: str,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            return ok(await self._detect_service(request, admin, service))
