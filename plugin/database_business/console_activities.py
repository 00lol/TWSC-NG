# -*- coding:utf-8 -*-
"""管理台 - 活动 / 系统消息 业务插件（接口清单 §8，序号 34-45）

插件：``console_activities``，前缀 ``/admin``，自建表 ``activities`` / ``notices``。

| # | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| 34 | GET  | /admin/activities                | 活动列表（分页 20，列表项不含 bodyHtml） |
| 35 | GET  | /admin/activities/{id}           | 活动详情（含 bodyHtml） |
| 36 | POST | /admin/activities                | 新建活动（草稿） |
| 37 | PUT  | /admin/activities/{id}           | 编辑活动（仅草稿，任意子集部分更新） |
| 38 | POST | /admin/activities/{id}/publish   | 发布活动（返回 published / in_progress） |
| 39 | POST | /admin/activities/{id}/offline   | 下架活动（仅 published） |
| 40 | GET  | /admin/notices                   | 系统消息列表（分页 20，表原始行） |
| 41 | GET  | /admin/notices/{id}              | 系统消息详情（视图层，read 恒为 null） |
| 42 | POST | /admin/notices                   | 新建消息（草稿） |
| 43 | PUT  | /admin/notices/{id}              | 编辑消息（仅草稿） |
| 44 | POST | /admin/notices/{id}/publish      | 发布消息 |
| 45 | POST | /admin/notices/{id}/withdraw     | 撤回消息（仅 published） |

两个容易踩坑的契约点：

1. **活动 status 是算出来的**：库中 ``activities`` 只有 ``stored_status``（draft/published/
   removed），出参 ``status`` 按「当前时间 + start_at/end_at」实时计算：
   ``stored=removed -> removed``；``stored=draft -> draft``；``stored=published`` 时
   ``now < start_at -> published``（已发布未开始）、``start_at <= now <= end_at ->
   in_progress``、``now > end_at -> published``（前端据 endAt 显示「已结束」）。
   同时回 ``storedStatus`` 供前端区分「未开始 / 已结束」。
2. **列表与详情的字段不同**：活动列表不含正文（bodyHtml 体积大），详情才回；
   消息列表是**表原始行**（snake_case），详情是视图层（camelCase + read=null）。

所有时间：入参 ISO8601 字符串 -> 解析成 ``+08:00`` 的 naive DATETIME 入库；
出参统一 ``to_iso()``（``2026-10-30T10:00:00.000+08:00``）。
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asyncmy.cursors import DictCursor
from fastapi import APIRouter, Depends, Request

from databases.plugin import DatabasePlugin
from transaction.console import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_PARAM,
    CODE_SERVER,
    ApiError,
    admin_guard,
    audit,
    ok,
    paged,
    parse_page,
    require_db,
    to_iso,
)

# --------------------------------------------------------------------------- #
# 表结构（权威定义见 docs/后端实现约定.md §6：字段名 = 出参字段名）
# --------------------------------------------------------------------------- #
TABLES = {
    "activities": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("title", "VARCHAR(64)"),
        ("start_at", "DATETIME"),
        ("end_at", "DATETIME"),
        ("body_html", "MEDIUMTEXT"),
        ("cover_key", "VARCHAR(255)"),
        ("stored_status", "VARCHAR(16) DEFAULT 'draft'"),
        ("publisher_id", "INT"),
        ("published_at", "DATETIME"),
        ("created_at", "DATETIME"),
        ("updated_at", "DATETIME"),
    ],
    "notices": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("title", "VARCHAR(64)"),
        ("body", "TEXT"),
        ("status", "VARCHAR(16) DEFAULT 'draft'"),
        ("publisher_id", "INT"),
        ("published_at", "DATETIME"),
        ("removed_at", "DATETIME"),
        ("created_at", "DATETIME"),
    ],
}

# 接口统一 +08:00（与 transaction.console.TZ 一致）
TZ = timezone(timedelta(hours=8))

# 入参校验区间（照接口清单 §8）
TITLE_MIN, TITLE_MAX = 2, 30
ACTIVITY_BODY_MIN, ACTIVITY_BODY_MAX = 2, 50000
NOTICE_BODY_MIN, NOTICE_BODY_MAX = 2, 5000
COVER_KEY_MAX = 255

# 列表可用的 status 过滤值
ACTIVITY_STATUS_FILTERS = ("draft", "published", "in_progress", "removed")
NOTICE_STATUS_FILTERS = ("draft", "published", "removed")

# 活动列表查询列（显式列出 = 天然不含 body_html）
ACTIVITY_LIST_COLUMNS = (
    "`id`,`title`,`start_at`,`end_at`,`cover_key`,`stored_status`,"
    "`publisher_id`,`published_at`,`created_at`,`updated_at`"
)

# 消息列表查询列（表原始行）
NOTICE_LIST_COLUMNS = (
    "`id`,`title`,`body`,`status`,`publisher_id`,`published_at`,`removed_at`,`created_at`"
)


# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #
def _now_local() -> datetime:
    """当前时间（+08:00 的墙上时间，naive）——库里 DATETIME 都是这个口径，可直接比较"""
    return datetime.now(TZ).replace(tzinfo=None)


def _as_local_dt(value: Any) -> Optional[datetime]:
    """把库里的时间值统一成 +08:00 naive datetime（供状态计算 / 区间比较）"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(TZ).replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(TZ).replace(tzinfo=None) if parsed.tzinfo else parsed


def _parse_dt(value: Any, label: str) -> datetime:
    """ISO8601 入参 -> +08:00 naive datetime；空值 / 格式错误抛 400/1001"""
    text = str(value or "").strip()
    if not text:
        raise ApiError(CODE_PARAM, f"参数不合法：{label}不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(
            CODE_PARAM,
            f"参数不合法：{label}格式不正确（需 ISO8601，如 2026-10-30T10:00:00+08:00）",
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TZ).replace(tzinfo=None)
    return parsed


def _activity_status(
    stored: Any,
    start_at: Any,
    end_at: Any,
    now: Optional[datetime] = None,
) -> str:
    """活动实时状态（清单 §8.1 的语义）

    - ``removed`` -> removed；
    - ``draft`` -> draft；
    - ``published`` 再按起止时间细分：
      ``now < start_at`` -> published（已发布未开始）；
      ``start_at <= now <= end_at`` -> in_progress；
      ``now > end_at`` -> published（已结束，前端据 endAt 显示「已结束」）。
    """
    text = str(stored or "draft")
    if text == "removed":
        return "removed"
    if text != "published":
        return "draft"

    current = now or _now_local()
    start = _as_local_dt(start_at)
    end = _as_local_dt(end_at)
    # 起止时间缺失属于脏数据：按「已发布」兜底，不做进行中判断
    if start is None or end is None:
        return "published"
    if current < start:
        return "published"
    if current <= end:
        return "in_progress"
    return "published"


# --------------------------------------------------------------------------- #
# 请求参数工具
# --------------------------------------------------------------------------- #
async def _json_body(request: Request) -> dict:
    """读 JSON 请求体，必须是对象；空 / 非法 JSON / 非对象一律 400/1001"""
    try:
        raw = await request.body()
    except Exception:
        raise ApiError(CODE_PARAM, "参数不合法：无法读取请求体") from None
    if not raw:
        raise ApiError(CODE_PARAM, "参数不合法：请求体不能为空")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(CODE_PARAM, "参数不合法：请求体必须是合法的 JSON") from None
    if not isinstance(data, dict):
        raise ApiError(CODE_PARAM, "参数不合法：请求体必须是 JSON 对象")
    return data


def _text(data: dict, key: str, label: str, low: int, high: int) -> str:
    """取必填字符串字段，按字符数校验长度（去除首尾空白后再校验）"""
    if key not in data or data.get(key) is None:
        raise ApiError(CODE_PARAM, f"参数不合法：{label}不能为空")
    value = data.get(key)
    if not isinstance(value, str):
        raise ApiError(CODE_PARAM, f"参数不合法：{label}必须是字符串")
    text = value.strip()
    if len(text) < low or len(text) > high:
        raise ApiError(CODE_PARAM, f"参数不合法：{label}长度需为 {low}-{high} 个字符")
    return text


def _cover_key(data: dict) -> Optional[str]:
    """取可选封面 key：缺省 / null / 空串都存 NULL，超 255 字符抛 400"""
    value = data.get("coverKey")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(CODE_PARAM, "参数不合法：coverKey 必须是字符串")
    text = value.strip()
    if not text:
        return None
    if len(text) > COVER_KEY_MAX:
        raise ApiError(
            CODE_PARAM, f"参数不合法：coverKey 长度不能超过 {COVER_KEY_MAX} 个字符"
        )
    return text


def _status_filter(request: Request, allowed: Sequence[str]) -> Optional[str]:
    """解析列表 status 过滤：缺省 / 空 / all 视为不过滤，非法值 400/1001"""
    raw = (request.query_params.get("status") or "").strip()
    if not raw or raw.lower() == "all":
        return None
    if raw not in allowed:
        raise ApiError(CODE_PARAM, "参数不合法：status 只能是 " + " / ".join(allowed))
    return raw


def _activity_status_where(status: Optional[str]) -> Tuple[str, List[Any]]:
    """把「实时状态」过滤翻译成 SQL 条件（库中只有 stored_status）

    返回 (where 片段, 参数列表)；条件为空时返回 ("", [])。
    """
    if status is None:
        return "", []
    if status in ("draft", "removed"):
        return "stored_status = %s", [status]
    now = _now_local()
    if status == "in_progress":
        return (
            "stored_status = 'published' "
            "AND `start_at` IS NOT NULL AND `end_at` IS NOT NULL "
            "AND %s >= `start_at` AND %s <= `end_at`",
            [now, now],
        )
    # published：已发布但当前不在进行中（未开始 / 已结束 / 脏数据）
    return (
        "stored_status = 'published' "
        "AND (`start_at` IS NULL OR `end_at` IS NULL "
        "OR %s < `start_at` OR %s > `end_at`)",
        [now, now],
    )


# --------------------------------------------------------------------------- #
# 出参映射
# --------------------------------------------------------------------------- #
def _activity_view(row: dict, with_body: bool = False) -> dict:
    """活动行 -> 视图层（status 实时计算，同时回 storedStatus）"""
    start_at = _as_local_dt(row.get("start_at"))
    end_at = _as_local_dt(row.get("end_at"))
    stored = str(row.get("stored_status") or "draft")
    item = {
        "id": int(row.get("id") or 0),
        "title": row.get("title"),
        "startAt": to_iso(start_at),
        "endAt": to_iso(end_at),
        "coverKey": row.get("cover_key"),
        "status": _activity_status(stored, start_at, end_at),
        "storedStatus": stored,
        "publisherId": row.get("publisher_id"),
        "publishedAt": to_iso(row.get("published_at")),
        "createdAt": to_iso(row.get("created_at")),
        "updatedAt": to_iso(row.get("updated_at")),
    }
    if with_body:
        item["bodyHtml"] = row.get("body_html") or ""
    return item


def _notice_row_view(row: dict) -> dict:
    """消息行 -> 列表视图（清单 §8.7：表原始行，字段名 snake_case）"""
    return {
        "id": int(row.get("id") or 0),
        "title": row.get("title"),
        "body": row.get("body"),
        "status": str(row.get("status") or "draft"),
        "publisher_id": row.get("publisher_id"),
        "published_at": to_iso(row.get("published_at")),
        "removed_at": to_iso(row.get("removed_at")),
        "created_at": to_iso(row.get("created_at")),
    }


def _notice_detail_view(row: dict) -> dict:
    """消息行 -> 详情视图（清单 §8.8：camelCase + read 恒为 null）"""
    return {
        "id": int(row.get("id") or 0),
        "title": row.get("title"),
        "body": row.get("body"),
        "status": str(row.get("status") or "draft"),
        "publishedAt": to_iso(row.get("published_at")),
        "removedAt": to_iso(row.get("removed_at")),
        "createdAt": to_iso(row.get("created_at")),
        "read": None,
    }


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #
class ConsoleActivitiesPlugin(DatabasePlugin):
    """管理台 - 活动 / 系统消息"""

    prefix = "/admin"  # 由 main.py 挂载插件路由时读取

    def __init__(self):
        super().__init__(
            name="console_activities",
            description="管理台 - 活动与系统消息",
        )

    async def initialize(self, value) -> bool:
        if not await super().initialize(value):
            return False
        await self.ensure_tables(TABLES)
        return True

    def register_routes(self, router: APIRouter) -> None:
        self._route(router)

    # ── 内部工具 ────────────────────────────────────────────────

    async def _insert_id(self, sql: str, params: tuple) -> int:
        """执行 INSERT 并取回自增主键

        ``DatabaseManager.execute`` 每次都会重新 acquire 连接，而
        ``cursor.lastrowid`` / ``LAST_INSERT_ID()`` 只在同一连接上有效，
        所以这里显式取一次驱动连接，把 INSERT 与 lastrowid 放在同一个连接里。
        """
        driver = getattr(self.database, "driver", None)
        require_pool = getattr(driver, "_require_pool", None)
        if not callable(require_pool):
            raise ApiError(CODE_SERVER, "服务异常：数据库驱动不可用", 500)
        try:
            pool = require_pool()
            async with pool.acquire() as conn:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute(sql, params)
                    new_id = int(cur.lastrowid or 0)
        except ApiError:
            raise
        except Exception as e:
            self.logger.error(f"[{self.name}] 新增记录失败: {e}")
            raise ApiError(CODE_SERVER, "服务异常，请稍后重试", 500) from e
        if new_id <= 0:
            raise ApiError(CODE_SERVER, "服务异常：未取回新增记录 ID", 500)
        return new_id

    async def _require_activity(self, activity_id: int) -> dict:
        """取活动行；不存在抛 404/3001"""
        row = await self.fetch_one(
            "SELECT * FROM `activities` WHERE `id` = %s", (activity_id,)
        )
        if not row:
            raise ApiError(CODE_NOT_FOUND, "活动不存在", 404)
        return row

    async def _require_notice(self, notice_id: int) -> dict:
        """取消息行；不存在抛 404/3001"""
        row = await self.fetch_one(
            "SELECT * FROM `notices` WHERE `id` = %s", (notice_id,)
        )
        if not row:
            raise ApiError(CODE_NOT_FOUND, "消息不存在", 404)
        return row

    # ── 路由 ────────────────────────────────────────────────────

    def _route(self, router: APIRouter) -> None:
        # ================= 活动（34-39） =================
        @router.get("/activities")
        async def list_activities(
            request: Request, admin: dict = Depends(admin_guard())
        ):
            """34 GET /admin/activities：活动列表（分页 20，列表项不含 bodyHtml）"""
            require_db(self)
            page, page_size = parse_page(request, default=20)
            status = _status_filter(request, ACTIVITY_STATUS_FILTERS)
            where, where_params = _activity_status_where(status)
            where_sql = f" WHERE {where}" if where else ""

            total_row = await self.fetch_one(
                f"SELECT COUNT(*) AS total FROM `activities`{where_sql}",
                tuple(where_params),
            )
            total = int((total_row or {}).get("total") or 0)

            rows = await self.fetch_all(
                f"SELECT {ACTIVITY_LIST_COLUMNS} FROM `activities`{where_sql} "
                "ORDER BY `id` DESC LIMIT %s OFFSET %s",
                tuple(where_params) + (page_size, (page - 1) * page_size),
            )
            items = [_activity_view(row, with_body=False) for row in rows]
            return ok(paged(items, total, page, page_size))

        @router.get("/activities/{activity_id}")
        async def get_activity(
            activity_id: int, admin: dict = Depends(admin_guard())
        ):
            """35 GET /admin/activities/{id}：活动详情（含 bodyHtml）"""
            require_db(self)
            row = await self._require_activity(activity_id)
            return ok(_activity_view(row, with_body=True))

        @router.post("/activities")
        async def create_activity(
            request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            """36 POST /admin/activities：新建活动（草稿）"""
            require_db(self)
            data = await _json_body(request)

            title = _text(data, "title", "标题", TITLE_MIN, TITLE_MAX)
            start_at = _parse_dt(data.get("startAt"), "开始时间")
            end_at = _parse_dt(data.get("endAt"), "结束时间")
            if end_at < start_at:
                raise ApiError(CODE_PARAM, "参数不合法：结束时间不能早于开始时间")
            body_html = _text(
                data, "bodyHtml", "正文", ACTIVITY_BODY_MIN, ACTIVITY_BODY_MAX
            )
            cover_key = _cover_key(data)

            now = _now_local()
            new_id = await self._insert_id(
                "INSERT INTO `activities` "
                "(`title`,`start_at`,`end_at`,`body_html`,`cover_key`,"
                "`stored_status`,`publisher_id`,`published_at`,`created_at`,`updated_at`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    title,
                    start_at,
                    end_at,
                    body_html,
                    cover_key,
                    "draft",
                    admin.get("id"),
                    None,
                    now,
                    now,
                ),
            )
            await audit(
                request,
                admin,
                module="活动管理",
                action="create_activity",
                target_type="activity",
                target_id=new_id,
                detail={
                    "title": title,
                    "startAt": to_iso(start_at),
                    "endAt": to_iso(end_at),
                },
            )
            return ok({"id": new_id, "status": "draft"})

        @router.put("/activities/{activity_id}")
        async def update_activity(
            activity_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            """37 PUT /admin/activities/{id}：编辑活动（仅草稿，任意子集部分更新）"""
            require_db(self)
            row = await self._require_activity(activity_id)
            stored = str(row.get("stored_status") or "draft")
            if stored != "draft":
                raise ApiError(CODE_CONFLICT, "只有草稿状态的活动可以编辑", 409)

            data = await _json_body(request)
            # 列名全部来自下面的硬编码分支，不做字符串拼接用户输入
            fields: Dict[str, Any] = {}
            if "title" in data:
                fields["title"] = _text(data, "title", "标题", TITLE_MIN, TITLE_MAX)
            if "startAt" in data:
                fields["start_at"] = _parse_dt(data.get("startAt"), "开始时间")
            if "endAt" in data:
                fields["end_at"] = _parse_dt(data.get("endAt"), "结束时间")
            if "bodyHtml" in data:
                fields["body_html"] = _text(
                    data, "bodyHtml", "正文", ACTIVITY_BODY_MIN, ACTIVITY_BODY_MAX
                )
            if "coverKey" in data:
                fields["cover_key"] = _cover_key(data)  # null / "" -> 清空封面

            # 部分更新也要保证区间合法：用「更新后」的有效值校验
            start_at = fields.get("start_at", _as_local_dt(row.get("start_at")))
            end_at = fields.get("end_at", _as_local_dt(row.get("end_at")))
            if start_at is not None and end_at is not None and end_at < start_at:
                raise ApiError(CODE_PARAM, "参数不合法：结束时间不能早于开始时间")

            if fields:
                assignments = ", ".join(f"`{column}` = %s" for column in fields)
                values: List[Any] = list(fields.values())
                values.extend([_now_local(), activity_id])
                await self.execute(
                    f"UPDATE `activities` SET {assignments}, `updated_at` = %s "
                    "WHERE `id` = %s",
                    tuple(values),
                )
                await audit(
                    request,
                    admin,
                    module="活动管理",
                    action="update_activity",
                    target_type="activity",
                    target_id=activity_id,
                    detail={"fields": sorted(fields.keys()), "title": data.get("title")},
                )

            return ok(
                {"id": activity_id, "status": _activity_status("draft", start_at, end_at)}
            )

        @router.post("/activities/{activity_id}/publish")
        async def publish_activity(
            activity_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            """38 POST /admin/activities/{id}/publish：发布活动"""
            require_db(self)
            row = await self._require_activity(activity_id)
            stored = str(row.get("stored_status") or "draft")
            if stored == "published":
                raise ApiError(CODE_CONFLICT, "活动已发布，请勿重复发布", 409)
            if stored == "removed":
                raise ApiError(CODE_CONFLICT, "活动已下架，不能再次发布", 409)

            now = _now_local()
            await self.execute(
                "UPDATE `activities` SET `stored_status` = %s, `published_at` = %s, "
                "`updated_at` = %s WHERE `id` = %s",
                ("published", now, now, activity_id),
            )
            status = _activity_status(
                "published",
                _as_local_dt(row.get("start_at")),
                _as_local_dt(row.get("end_at")),
                now,
            )
            await audit(
                request,
                admin,
                module="活动管理",
                action="publish_activity",
                target_type="activity",
                target_id=activity_id,
                detail={"title": row.get("title"), "status": status},
            )
            return ok({"id": activity_id, "status": status})

        @router.post("/activities/{activity_id}/offline")
        async def offline_activity(
            activity_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            """39 POST /admin/activities/{id}/offline：下架活动（仅 published）"""
            require_db(self)
            row = await self._require_activity(activity_id)
            stored = str(row.get("stored_status") or "draft")
            if stored == "removed":
                raise ApiError(CODE_CONFLICT, "活动已下架，请勿重复操作", 409)
            if stored != "published":
                raise ApiError(CODE_CONFLICT, "仅已发布的活动可以下架", 409)

            now = _now_local()
            await self.execute(
                "UPDATE `activities` SET `stored_status` = %s, `updated_at` = %s "
                "WHERE `id` = %s",
                ("removed", now, activity_id),
            )
            await audit(
                request,
                admin,
                module="活动管理",
                action="offline_activity",
                target_type="activity",
                target_id=activity_id,
                detail={"title": row.get("title")},
            )
            return ok({"id": activity_id, "status": "removed"})

        # ================= 系统消息（40-45） =================
        @router.get("/notices")
        async def list_notices(request: Request, admin: dict = Depends(admin_guard())):
            """40 GET /admin/notices：系统消息列表（分页 20，表原始行）"""
            require_db(self)
            page, page_size = parse_page(request, default=20)
            status = _status_filter(request, NOTICE_STATUS_FILTERS)
            where_sql = " WHERE `status` = %s" if status else ""
            where_params: Tuple[Any, ...] = (status,) if status else ()

            total_row = await self.fetch_one(
                f"SELECT COUNT(*) AS total FROM `notices`{where_sql}", where_params
            )
            total = int((total_row or {}).get("total") or 0)

            rows = await self.fetch_all(
                f"SELECT {NOTICE_LIST_COLUMNS} FROM `notices`{where_sql} "
                "ORDER BY `id` DESC LIMIT %s OFFSET %s",
                where_params + (page_size, (page - 1) * page_size),
            )
            items = [_notice_row_view(row) for row in rows]
            return ok(paged(items, total, page, page_size))

        @router.get("/notices/{notice_id}")
        async def get_notice(notice_id: int, admin: dict = Depends(admin_guard())):
            """41 GET /admin/notices/{id}：系统消息详情（视图层，read=null）"""
            require_db(self)
            row = await self._require_notice(notice_id)
            return ok(_notice_detail_view(row))

        @router.post("/notices")
        async def create_notice(
            request: Request, admin: dict = Depends(admin_guard(write=True))
        ):
            """42 POST /admin/notices：新建消息（草稿）"""
            require_db(self)
            data = await _json_body(request)

            title = _text(data, "title", "标题", TITLE_MIN, TITLE_MAX)
            body = _text(data, "body", "正文", NOTICE_BODY_MIN, NOTICE_BODY_MAX)

            now = _now_local()
            new_id = await self._insert_id(
                "INSERT INTO `notices` "
                "(`title`,`body`,`status`,`publisher_id`,`published_at`,`removed_at`,`created_at`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (title, body, "draft", admin.get("id"), None, None, now),
            )
            await audit(
                request,
                admin,
                module="消息管理",
                action="create_notice",
                target_type="notice",
                target_id=new_id,
                detail={"title": title},
            )
            return ok({"id": new_id, "status": "draft"})

        @router.put("/notices/{notice_id}")
        async def update_notice(
            notice_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            """43 PUT /admin/notices/{id}：编辑消息（仅草稿）"""
            require_db(self)
            row = await self._require_notice(notice_id)
            stored = str(row.get("status") or "draft")
            if stored != "draft":
                raise ApiError(CODE_CONFLICT, "只有草稿状态的消息可以编辑", 409)

            data = await _json_body(request)
            fields: Dict[str, Any] = {}
            if "title" in data:
                fields["title"] = _text(data, "title", "标题", TITLE_MIN, TITLE_MAX)
            if "body" in data:
                fields["body"] = _text(
                    data, "body", "正文", NOTICE_BODY_MIN, NOTICE_BODY_MAX
                )

            if fields:
                assignments = ", ".join(f"`{column}` = %s" for column in fields)
                values: List[Any] = list(fields.values())
                values.append(notice_id)
                await self.execute(
                    f"UPDATE `notices` SET {assignments} WHERE `id` = %s",
                    tuple(values),
                )
                await audit(
                    request,
                    admin,
                    module="消息管理",
                    action="update_notice",
                    target_type="notice",
                    target_id=notice_id,
                    detail={"fields": sorted(fields.keys()), "title": data.get("title")},
                )

            # 仅草稿可编辑，编辑不改状态，仍回 draft
            return ok({"id": notice_id, "status": "draft"})

        @router.post("/notices/{notice_id}/publish")
        async def publish_notice(
            notice_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            """44 POST /admin/notices/{id}/publish：发布消息"""
            require_db(self)
            row = await self._require_notice(notice_id)
            stored = str(row.get("status") or "draft")
            if stored == "published":
                raise ApiError(CODE_CONFLICT, "消息已发布，请勿重复发布", 409)
            if stored == "removed":
                raise ApiError(CODE_CONFLICT, "消息已撤回，不能再次发布", 409)

            now = _now_local()
            await self.execute(
                "UPDATE `notices` SET `status` = %s, `published_at` = %s "
                "WHERE `id` = %s",
                ("published", now, notice_id),
            )
            await audit(
                request,
                admin,
                module="消息管理",
                action="publish_notice",
                target_type="notice",
                target_id=notice_id,
                detail={"title": row.get("title"), "publishedAt": to_iso(now)},
            )
            return ok(
                {"id": notice_id, "status": "published", "publishedAt": to_iso(now)}
            )

        @router.post("/notices/{notice_id}/withdraw")
        async def withdraw_notice(
            notice_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            """45 POST /admin/notices/{id}/withdraw：撤回消息（仅 published）"""
            require_db(self)
            row = await self._require_notice(notice_id)
            stored = str(row.get("status") or "draft")
            if stored == "removed":
                raise ApiError(CODE_CONFLICT, "消息已撤回，请勿重复操作", 409)
            if stored != "published":
                raise ApiError(CODE_CONFLICT, "仅已发布的消息可以撤回", 409)

            now = _now_local()
            await self.execute(
                "UPDATE `notices` SET `status` = %s, `removed_at` = %s WHERE `id` = %s",
                ("removed", now, notice_id),
            )
            await audit(
                request,
                admin,
                module="消息管理",
                action="withdraw_notice",
                target_type="notice",
                target_id=notice_id,
                detail={"title": row.get("title")},
            )
            return ok({"id": notice_id, "status": "removed"})
