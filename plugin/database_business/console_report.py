# -*- coding:utf-8 -*-
"""管理台 - 数据报备插件（接口 46-49 + CSV 导出）

负责：
- GET /admin/report-data            数据报备（period=day/week/month + from/to）
- GET /admin/report-data/export.csv 导出（新规范路径）
- GET /admin/reports/export.csv     导出（兼容前端现有路径，同一处理器）
- GET /admin/report-snapshots       指标快照列表（分页 20）
- GET /admin/report-snapshots/:id   指标快照详情

数据来源：报备数据从 orders / platform_users 现算（口径见 API-接口清单 §9.1），
快照存自己表 console_report_snapshots。跨插件读取一律先 table_exists 判断，
表不存在按空数据返回 0，绝不 500。

对外复用：``get_report_data(period, date_from, date_to)`` 给
``console_content`` 的 ``GET /admin/reports``（带 period/from/to 的双实现）转调，
该函数不抛异常，失败返回空 list。
"""
import json
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from databases.plugin import DatabasePlugin
from transaction.console import (
    ApiError,
    CODE_NOT_FOUND,
    CODE_PARAM,
    admin_guard,
    csv_response,
    jsonable,
    now_dt,
    ok,
    paged,
    parse_date,
    parse_page,
    require_db,
    to_int,
    to_iso,
)

# 本插件自己的表（docs §6：console_report_snapshots）
TABLES = {
    "console_report_snapshots": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("trigger_event", "VARCHAR(255)"),
        ("metrics", "JSON"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
}

#: 报备周期
PERIODS = ("day", "week", "month")
#: 单次查询最大跨度（天），防止一次拉爆数据库
MAX_RANGE_DAYS = 366
#: 成交额口径：当期创建且非「待付款 / 已取消」
GMV_EXCLUDED_STATUS = ("pending_pay", "cancelled")
#: CSV 表头（与接口清单 §9.2 一致）
CSV_HEADER = ["周期", "订单数", "成交额(分)", "佣金收入(分)", "新增用户", "活跃用户"]


def _as_int(value: Any) -> int:
    """SUM/COUNT 结果统一转 int（MySQL 的 SUM 会返回 Decimal，JSON 不能直接序列化）"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class ConsoleReportPlugin(DatabasePlugin):
    """数据报备 + 指标快照"""

    # 由 main.py 挂载时读取：管理台统一 /admin
    prefix = "/admin"

    def __init__(self):
        super().__init__(name="console_report", description="管理台 - 数据报备")

    async def initialize(self, value) -> bool:
        if not await super().initialize(value):
            return False
        await self.ensure_tables(TABLES)
        self.logger.info("[console_report] 数据报备插件初始化完成")
        return True

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    async def _has_table(self, name: str) -> bool:
        """表是否存在（跨插件表可能还没建）"""
        database = getattr(self, "database", None)
        if database is None:
            return False
        try:
            return bool(await database.table_exists(name))
        except Exception as e:
            self.logger.warning(f"[console_report] 检查表 {name} 失败: {e}")
            return False

    @staticmethod
    def _parse_period(period: Any) -> str:
        """校验 period；非法抛 1001"""
        text = str(period or "day").strip().lower()
        if text not in PERIODS:
            raise ApiError(CODE_PARAM, "period 只支持 day / week / month", 400)
        return text

    @staticmethod
    def _default_range(period: str, end: date) -> date:
        """未传 from 时的默认起点：日=近 30 天，周=近 12 周，月=近 12 个月"""
        if period == "week":
            monday = end - timedelta(days=end.weekday())
            return monday - timedelta(days=11 * 7)
        if period == "month":
            first = end.replace(day=1)
            year, month = first.year, first.month - 11
            while month <= 0:
                month += 12
                year -= 1
            return date(year, month, 1)
        return end - timedelta(days=29)

    @staticmethod
    def _align_range(period: str, start: date, end: date) -> tuple:
        """把用户区间对齐到自然周期（周=周一~周日，月=整月），保证每个桶完整"""
        if period == "week":
            start = start - timedelta(days=start.weekday())
            end = end + timedelta(days=6 - end.weekday())
        elif period == "month":
            start = start.replace(day=1)
            # 下个月 1 号 - 1 天 = 本月最后一天
            first_next = (end.replace(day=1) + timedelta(days=32)).replace(day=1)
            end = first_next - timedelta(days=1)
        return start, end

    def _resolve_range(
        self, period: str, date_from: Any, date_to: Any, strict: bool = True
    ) -> tuple:
        """解析并校验 from/to，返回对齐后的 (start, end)

        strict=True 时参数错误抛 ApiError（路由用）；False 时退化为默认值（跨插件转调用）。
        """
        end = parse_date(date_to) if date_to else None
        if end is None:
            if strict and str(date_to or "").strip():
                raise ApiError(CODE_PARAM, "to 日期格式应为 YYYY-MM-DD", 400)
            end = now_dt().date()

        start = parse_date(date_from) if date_from else None
        if start is None:
            if strict and str(date_from or "").strip():
                raise ApiError(CODE_PARAM, "from 日期格式应为 YYYY-MM-DD", 400)
            start = self._default_range(period, end)

        if start > end:
            if strict:
                raise ApiError(CODE_PARAM, "from 不能晚于 to", 400)
            start, end = end, start

        span = (end - start).days + 1
        if span > MAX_RANGE_DAYS:
            if strict:
                raise ApiError(
                    CODE_PARAM, f"查询跨度不能超过 {MAX_RANGE_DAYS} 天", 400
                )
            start = end - timedelta(days=MAX_RANGE_DAYS - 1)

        return self._align_range(period, start, end)

    @staticmethod
    def _bucket_starts(period: str, start: date, end: date) -> list:
        """生成每个周期的桶起始日（day=每天；week=每周一；month=每月 1 号）"""
        starts = []
        if period == "week":
            cursor = start
            while cursor <= end:
                starts.append(cursor)
                cursor += timedelta(days=7)
            return starts
        if period == "month":
            cursor = start.replace(day=1)
            while cursor <= end:
                starts.append(cursor)
                # 下个月 1 号
                cursor = (cursor + timedelta(days=32)).replace(day=1)
            return starts
        cursor = start
        while cursor <= end:
            starts.append(cursor)
            cursor += timedelta(days=1)
        return starts

    @staticmethod
    def _window_expr(period: str, column: str) -> tuple:
        """按周期分桶的 SQL 表达式 + 该表达式要占的参数

        返回 (expr, params)：
        - day  : DATEDIFF(DATE(column), start)                         -> 0,1,2...
        - week : FLOOR(DATEDIFF(DATE(column), start) / 7)              -> 0,1,2...
        - month: (YEAR*12+MONTH) - (YEAR(start)*12+MONTH(start))       -> 0,1,2...
        """
        if period == "month":
            expr = (
                f"(YEAR({column}) * 12 + MONTH({column})) "
                f"- (YEAR(%s) * 12 + MONTH(%s))"
            )
            # 参数占两位，稍后由调用方替换成 start 的日期字符串
            return expr, 2
        if period == "week":
            return f"FLOOR(DATEDIFF(DATE({column}), %s) / 7)", 1
        return f"DATEDIFF(DATE({column}), %s)", 1

    def _bucket_metrics(self) -> dict:
        """每个桶的指标累加容器"""
        return {"orders": 0, "gmv": 0, "commission": 0, "new_users": 0, "active_users": 0}

    async def _build_report(self, period: str, start: date, end: date) -> dict:
        """按周期分桶统计报备数据（4 条聚合 SQL，表缺失自动降级为 0）"""
        starts = self._bucket_starts(period, start, end)
        buckets = {index: self._bucket_metrics() for index in range(len(starts))}
        start_text = start.strftime("%Y-%m-%d")
        start_dt = start.strftime("%Y-%m-%d 00:00:00")
        end_dt = (end + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")

        def _merge(rows: list, field: str) -> None:
            """把 SQL 结果合并进桶（下标越界的数据丢弃）"""
            for row in rows or []:
                index = _as_int(row.get("bucket_idx"))
                if index in buckets:
                    buckets[index][field] = _as_int(row.get(field))
                else:
                    self.logger.debug(
                        f"[console_report] 忽略越界分桶数据 idx={index} field={field}"
                    )

        # 1) 订单数 + 成交额（当期创建；成交额排除待付款/已取消）
        if await self._has_table("orders"):
            expr, count = self._window_expr(period, "created_at")
            sql = (
                f"SELECT {expr} AS bucket_idx, "
                f"COUNT(*) AS orders, "
                f"COALESCE(SUM(CASE WHEN status NOT IN "
                f"('{GMV_EXCLUDED_STATUS[0]}', '{GMV_EXCLUDED_STATUS[1]}') "
                f"THEN amount ELSE 0 END), 0) AS gmv "
                f"FROM orders WHERE created_at >= %s AND created_at < %s "
                f"GROUP BY bucket_idx"
            )
            params = [start_text] * count + [start_dt, end_dt]
            rows = await self.fetch_all(sql, tuple(params))
            _merge([{"bucket_idx": r["bucket_idx"], "orders": r["orders"]} for r in rows], "orders")
            _merge([{"bucket_idx": r["bucket_idx"], "gmv": r["gmv"]} for r in rows], "gmv")

            # 2) 佣金：当期结算（settled）订单的 commission_amount，按结算时间归期
            expr, count = self._window_expr(period, "settled_at")
            sql = (
                f"SELECT {expr} AS bucket_idx, "
                f"COALESCE(SUM(commission_amount), 0) AS commission "
                f"FROM orders WHERE status = 'settled' AND settled_at IS NOT NULL "
                f"AND settled_at >= %s AND settled_at < %s GROUP BY bucket_idx"
            )
            params = [start_text] * count + [start_dt, end_dt]
            _merge(await self.fetch_all(sql, tuple(params)), "commission")

            # 3) 活跃用户：当期有订单行为的去重用户（下单客户 + 接单飞手）
            expr, count = self._window_expr(period, "created_at")
            sql = (
                f"SELECT bucket_idx, COUNT(DISTINCT uid) AS active_users FROM ("
                f"SELECT {expr} AS bucket_idx, client_id AS uid FROM orders "
                f"WHERE created_at >= %s AND created_at < %s AND client_id IS NOT NULL "
                f"UNION ALL "
                f"SELECT {expr} AS bucket_idx, pilot_id AS uid FROM orders "
                f"WHERE created_at >= %s AND created_at < %s AND pilot_id IS NOT NULL"
                f") AS t GROUP BY bucket_idx"
            )
            window = [start_text] * count
            params = window + [start_dt, end_dt] + window + [start_dt, end_dt]
            rows = await self.fetch_all(sql, tuple(params))
            _merge(
                [{"bucket_idx": r["bucket_idx"], "active_users": r["active_users"]} for r in rows],
                "active_users",
            )

        # 4) 新增用户：当期注册
        if await self._has_table("platform_users"):
            expr, count = self._window_expr(period, "created_at")
            sql = (
                f"SELECT {expr} AS bucket_idx, COUNT(*) AS new_users "
                f"FROM platform_users WHERE created_at >= %s AND created_at < %s "
                f"GROUP BY bucket_idx"
            )
            params = [start_text] * count + [start_dt, end_dt]
            _merge(await self.fetch_all(sql, tuple(params)), "new_users")

        rows = [
            {"period": bucket_start.isoformat(), **buckets[index]}
            for index, bucket_start in enumerate(starts)
        ]
        return {
            "period": period,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "list": rows,
        }

    async def get_report_data(
        self, period: Any = "day", date_from: Any = None, date_to: Any = None
    ) -> dict:
        """报备数据（供 console_content 的 /admin/reports 双实现转调）

        约定：本方法**绝不抛异常**；参数非法或数据库异常时返回空 list。
        """
        try:
            normalized = self._parse_period(period)
        except ApiError:
            normalized = "day"
        try:
            start, end = self._resolve_range(normalized, date_from, date_to, strict=False)
            return await self._build_report(normalized, start, end)
        except Exception as e:
            self.logger.error(f"[console_report] 生成报备数据失败: {e}")
            today = now_dt().date().isoformat()
            return {
                "period": normalized,
                "from": str(date_from or today)[:10],
                "to": str(date_to or today)[:10],
                "list": [],
            }

    # ------------------------------------------------------------------ #
    # 指标快照
    # ------------------------------------------------------------------ #
    async def build_snapshot_metrics(self) -> dict:
        """生成快照指标：今日订单数/成交额/佣金 + 平台累计用户数（口径同 §9.4）"""
        metrics = {"orders": 0, "gmv": 0, "commission": 0, "users": 0}
        try:
            today = now_dt().date()
            data = await self._build_report("day", today, today)
            rows = data.get("list") or []
            if rows:
                row = rows[0]
                metrics["orders"] = _as_int(row.get("orders"))
                metrics["gmv"] = _as_int(row.get("gmv"))
                metrics["commission"] = _as_int(row.get("commission"))
            if await self._has_table("platform_users"):
                row = await self.fetch_one(
                    "SELECT COUNT(*) AS total FROM platform_users"
                )
                metrics["users"] = _as_int((row or {}).get("total"))
        except Exception as e:
            self.logger.warning(f"[console_report] 计算快照指标失败，按 0 处理: {e}")
        return metrics

    async def add_snapshot(
        self, trigger_event: str, metrics: Optional[dict] = None
    ) -> bool:
        """追加一条指标快照（参数修改等关键事件调用）；失败只记日志，不影响主流程"""
        try:
            if metrics is None:
                metrics = await self.build_snapshot_metrics()
            text = str(trigger_event or "")[:255]
            await self.execute(
                "INSERT INTO console_report_snapshots (trigger_event, metrics) "
                "VALUES (%s, %s)",
                (text, json.dumps(metrics, ensure_ascii=False)),
            )
            self.logger.info(f"[console_report] 已生成指标快照: {text}")
            return True
        except Exception as e:
            self.logger.warning(f"[console_report] 写指标快照失败（不影响主流程）: {e}")
            return False

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def register_routes(self, router: APIRouter) -> None:
        self._route(router)

    def _route(self, router: APIRouter) -> None:
        # ---------------- 46. 数据报备 ----------------
        @router.get("/report-data", summary="数据报备列表")
        async def report_data(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            period = self._parse_period(request.query_params.get("period"))
            start, end = self._resolve_range(
                period,
                request.query_params.get("from"),
                request.query_params.get("to"),
                strict=True,
            )
            data = await self._build_report(period, start, end)
            return ok(data)

        # ---------------- 47. 数据报备导出（两个路径同一处理器）----------------
        @router.get("/report-data/export.csv", summary="数据报备导出 CSV")
        @router.get("/reports/export.csv", summary="数据报备导出 CSV（兼容旧路径）")
        async def export_report_csv(
            request: Request, admin: dict = Depends(admin_guard())
        ):
            require_db(self)
            period = self._parse_period(request.query_params.get("period"))
            start, end = self._resolve_range(
                period,
                request.query_params.get("from"),
                request.query_params.get("to"),
                strict=True,
            )
            data = await self._build_report(period, start, end)
            rows = [
                [
                    row["period"],
                    row["orders"],
                    row["gmv"],
                    row["commission"],
                    row["new_users"],
                    row["active_users"],
                ]
                for row in data["list"]
            ]
            filename = f"report-{period}-{data['from']}-{data['to']}.csv"
            return csv_response(filename, CSV_HEADER, rows)

        # ---------------- 48. 指标快照列表 ----------------
        @router.get("/report-snapshots", summary="指标快照列表")
        async def list_snapshots(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            page, page_size = parse_page(request, 20)
            if not await self._has_table("console_report_snapshots"):
                return ok(paged([], 0, page, page_size))

            total_row = await self.fetch_one(
                "SELECT COUNT(*) AS total FROM console_report_snapshots"
            )
            total = _as_int((total_row or {}).get("total"))
            rows = await self.fetch_all(
                "SELECT id, trigger_event, created_at FROM console_report_snapshots "
                "ORDER BY id DESC LIMIT %s OFFSET %s",
                (page_size, (page - 1) * page_size),
            )
            items = [
                {
                    "id": _as_int(row.get("id")),
                    "trigger_event": row.get("trigger_event"),
                    "created_at": to_iso(row.get("created_at")),
                }
                for row in rows
            ]
            return ok(paged(items, total, page, page_size))

        # ---------------- 49. 指标快照详情 ----------------
        @router.get("/report-snapshots/{snapshot_id}", summary="指标快照详情")
        async def snapshot_detail(
            snapshot_id: str, admin: dict = Depends(admin_guard())
        ):
            require_db(self)
            sid = to_int(snapshot_id, 0)
            if sid <= 0:
                raise ApiError(CODE_PARAM, "快照 id 不合法", 400)
            if not await self._has_table("console_report_snapshots"):
                raise ApiError(CODE_NOT_FOUND, "快照不存在", 404)
            row = await self.fetch_one(
                "SELECT id, trigger_event, metrics, created_at "
                "FROM console_report_snapshots WHERE id = %s",
                (sid,),
            )
            if not row:
                raise ApiError(CODE_NOT_FOUND, "快照不存在", 404)
            metrics = jsonable(row.get("metrics"))
            return ok(
                {
                    "id": _as_int(row.get("id")),
                    "trigger_event": row.get("trigger_event"),
                    "created_at": to_iso(row.get("created_at")),
                    "metrics": metrics if isinstance(metrics, dict) else None,
                }
            )
