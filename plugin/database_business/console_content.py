# -*- coding:utf-8 -*-
"""管理台 - 内容管理业务插件（作品 / 评论 / 举报 / 频道）

插件名 ``console_content``，挂载前缀 ``/admin``，负责 ``API-接口清单.md`` §7.1-7.9
（序号 25-33）共 9 条接口：

    25. GET    /admin/works                     作品队列（不分页，上限 200；带 counts）
    26. POST   /admin/works/{id}/audit          作品审核（通过 / 驳回）
    27. POST   /admin/works/{id}/offline        作品下架
    28. GET    /admin/comments                  评论列表（分页，默认 20）
    29. POST   /admin/comments/{id}/delete      评论删除（软删 deleted=1）
    30. GET    /admin/reports                   举报列表 / 数据报备（**双实现**）
    31. POST   /admin/reports/{id}/handle       举报处理
    32. GET    /admin/channels                  频道开关读取（rent/repair/training）
    33. PUT    /admin/channels                  频道开关修改（任意子集）

关于 §9.1 的 ``GET /admin/reports`` 双实现（团队决定，见 docs/后端实现约定.md §0/§1）：

- 请求**不带** ``period`` / ``from`` / ``to`` 任一参数 -> 返回举报列表（§7.6）；
- 请求**带**上述任一参数 -> 数据报备，转调 ``console_report`` 插件的方法；
  插件未安装 / 方法不存在 / 调用失败时返回 ``{"period","from","to","list":[]}``，
  **绝不报错**（前端「数据报备」页保持可用，只是数据为空）。

实现约定：
- 表结构照 docs/后端实现约定.md §6（works / comments / content_reports / console_channels）；
- ``platform_users`` / ``pilot_profiles`` 由 ``console_users`` 插件创建，本插件只读，
  且表不存在时降级（author.name 用 author_id、hasLicense=false），绝不报错；
- 写接口一律 ``Depends(admin_guard(write=True))`` + ``await audit(...)``（模块「内容管理」）；
- 数据库不可用 ``require_db(self)``；业务错误 ``raise ApiError(...)``；时间统一 ``to_iso``。
"""
import inspect
import json
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
    jsonable,
    now_dt,
    ok,
    paged,
    parse_page,
    require_db,
    to_int,
    to_iso,
)

# --------------------------------------------------------------------------- #
# 表结构（唯一权威来源：docs/后端实现约定.md §6）
# --------------------------------------------------------------------------- #
TABLES = {
    # 作品
    "works": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("author_id", "INT"),
        ("title", "VARCHAR(128)"),
        ("description", "TEXT"),
        ("cover", "VARCHAR(255)"),
        ("media", "JSON"),
        ("task_type", "VARCHAR(32)"),
        ("tags", "JSON"),
        ("status", "VARCHAR(16) DEFAULT 'pending'"),
        ("like_count", "INT DEFAULT 0"),
        ("comment_count", "INT DEFAULT 0"),
        ("reject_reason", "VARCHAR(255)"),
        ("created_at", "DATETIME"),
        ("reviewed_at", "DATETIME"),
    ],
    # 评论
    "comments": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("work_id", "INT"),
        ("user_id", "INT"),
        ("content", "VARCHAR(1000)"),
        ("deleted", "TINYINT(1) DEFAULT 0"),
        ("created_at", "DATETIME"),
    ],
    # 举报
    "content_reports": [
        ("id", "INT AUTO_INCREMENT PRIMARY KEY"),
        ("reporter_id", "INT"),
        ("target_type", "VARCHAR(16)"),
        ("target_id", "INT"),
        ("reason", "VARCHAR(64)"),
        ("detail", "VARCHAR(500)"),
        ("status", "VARCHAR(16) DEFAULT 'pending'"),
        ("handled_by", "INT"),
        ("handle_note", "VARCHAR(500)"),
        ("created_at", "DATETIME"),
        ("handled_at", "DATETIME"),
    ],
    # 频道开关（固定 3 行：rent / repair / training）
    "console_channels": [
        ("key", "VARCHAR(32) PRIMARY KEY"),
        ("enabled", "TINYINT(1) DEFAULT 0"),
    ],
}

# 作品状态（counts 必须补齐全部键）
WORK_STATUSES = ("pending", "approved", "rejected", "offline")
# 举报状态
REPORT_STATUSES = ("pending", "handled", "rejected")
# 频道固定三键
CHANNEL_KEYS = ("rent", "repair", "training")
# 任务类型中文名（未知类型原样返回）
TASK_TYPE_LABELS = {
    "aerial": "航拍",
    "rent": "租赁",
    "repair": "维修",
    "training": "培训",
}
# 不分页列表的后端上限
LIST_LIMIT = 200
# 作品列表显式列（列名固定，不来自用户输入）
WORK_COLUMNS = (
    "id",
    "author_id",
    "title",
    "description",
    "cover",
    "media",
    "task_type",
    "tags",
    "status",
    "like_count",
    "comment_count",
    "reject_reason",
    "created_at",
)
# 数据报备转调 console_report 时依次探测的方法名（各小组并行开发，先按约定名探测）
REPORT_DATA_METHODS = (
    "get_report_data",
    "report_data",
    "get_reports_data",
    "list_report_data",
    "build_report_data",
    "report_summary",
    "reports_data",
)


# --------------------------------------------------------------------------- #
# 纯函数小工具
# --------------------------------------------------------------------------- #
def _clean_text(value: Any) -> Optional[str]:
    """去空白后的字符串；空值返回 None（契约：空值一律 null）"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_list(value: Any) -> list:
    """JSON 文本（媒体 / 标签）-> list：非法 JSON 或空值降级为空数组"""
    parsed = jsonable(value)
    if isinstance(parsed, list):
        return parsed
    if parsed is None or parsed == "":
        return []
    return [parsed]


def _as_bool(value: Any) -> Optional[bool]:
    """严格解析 JSON 布尔：只接受 true/false 与 0/1；其他值返回 None（由调用方报 400）

    契约里 approved / rent 等字段都是布尔，不能把 "yes"、2、"1" 之类含糊值当布尔放过去。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _len_between(text: Optional[str], low: int, high: int) -> bool:
    """文本长度是否在 [low, high]（按字符数）"""
    if text is None:
        return False
    return low <= len(text) <= high


def _task_type_label(task_type: Optional[str]) -> Optional[str]:
    """任务类型 -> 中文标签（未知类型原样返回）"""
    if task_type is None:
        return None
    key = str(task_type).strip()
    if not key:
        return None
    return TASK_TYPE_LABELS.get(key, key)


def _in_clause(count: int) -> str:
    """IN 查询占位符：3 -> '%s,%s,%s'（值仍然走参数化）"""
    return ",".join(["%s"] * max(0, int(count)))


def _normalize_report_data(
    result: Any, period: str, date_from: Optional[str], date_to: Optional[str]
) -> dict:
    """把 console_report 的返回统一成 §9.1 契约结构（period/from/to/list）"""
    if isinstance(result, dict):
        data = dict(result)
    elif isinstance(result, (list, tuple)):
        data = {"list": list(result)}
    else:
        data = {}
    data.setdefault("period", period)
    data.setdefault("from", date_from)
    data.setdefault("to", date_to)
    if not isinstance(data.get("list"), list):
        data["list"] = []
    return data


def _empty_report_data(period: str, date_from: Optional[str], date_to: Optional[str]) -> dict:
    """数据报备兜底空结构（console_report 未安装 / 调用失败时）"""
    return {"period": period, "from": date_from, "to": date_to, "list": []}


class ConsoleContentPlugin(DatabasePlugin):
    """管理台 - 内容管理（作品 / 评论 / 举报 / 频道）"""

    # 由 main.py 挂载时读取：管理台插件统一 /admin
    prefix = "/admin"

    def __init__(self):
        super().__init__(
            name="console_content",
            description="管理台 - 内容管理（作品/评论/举报/频道）",
        )
        # 外部插件表（platform_users / pilot_profiles）的字段缓存：
        # 只在表存在时缓存（表由 console_users 建，可能晚于本插件初始化）
        self._meta_cache = {}

    # ── 初始化：建表 + 频道三行 ─────────────────────────────────

    async def initialize(self, value) -> bool:
        if not await super().initialize(value):
            return False
        await self.ensure_tables(TABLES)
        await self._ensure_channels()
        return True

    async def _ensure_channels(self) -> None:
        """频道表固定 3 行 rent/repair/training（INSERT IGNORE，幂等）"""
        if self.database is None:
            return
        for key in CHANNEL_KEYS:
            try:
                await self.execute(
                    "INSERT IGNORE INTO console_channels (`key`, enabled) VALUES (%s, 0)",
                    (key,),
                )
            except Exception as e:
                # 建表失败不阻断插件初始化，读写时会再兜底
                try:
                    self.logger.warning(f"[console_content] 初始化频道 {key} 失败: {e}")
                except Exception:
                    pass

    # ── 外部表探测（platform_users / pilot_profiles 由 console_users 建）──

    async def _table_columns(self, table: str) -> list:
        """读取外部表字段名；表不存在/不可用返回 []（结果只缓存「存在」的情况）"""
        cached = self._meta_cache.get(table)
        if cached is not None:
            return cached
        database = self.database
        if database is None:
            return []
        try:
            exists = await database.table_exists(table)
            if not exists:
                return []
            columns = list(await database.table_columns(table) or [])
        except Exception as e:
            try:
                self.logger.debug(f"[console_content] 探测表 {table} 失败（按不存在处理）: {e}")
            except Exception:
                pass
            return []
        self._meta_cache[table] = columns
        return columns

    async def _load_users(self, user_ids) -> dict:
        """批量读 platform_users（只取存在的字段）；表不存在返回 {}"""
        ids = sorted({int(i) for i in user_ids if i is not None})
        if not ids:
            return {}
        columns = await self._table_columns("platform_users")
        if not columns or "id" not in columns:
            return {}
        select = ["id"]
        for name in ("nickname", "real_name", "avatar"):
            if name in columns:
                select.append(name)
        try:
            rows = await self.fetch_all(
                f"SELECT {', '.join(select)} FROM platform_users "
                f"WHERE id IN ({_in_clause(len(ids))})",
                tuple(ids),
            )
        except Exception as e:
            try:
                self.logger.warning(f"[console_content] 读取用户信息失败（降级）: {e}")
            except Exception:
                pass
            return {}
        result = {}
        for row in rows or []:
            try:
                result[int(row["id"])] = row
            except (KeyError, TypeError, ValueError):
                continue
        return result

    async def _load_approved_license_ids(self, user_ids) -> set:
        """批量读 pilot_profiles.license_status='approved' 的用户集合；表不存在返回空集"""
        ids = sorted({int(i) for i in user_ids if i is not None})
        if not ids:
            return set()
        columns = await self._table_columns("pilot_profiles")
        if not columns or "user_id" not in columns or "license_status" not in columns:
            return set()
        try:
            rows = await self.fetch_all(
                f"SELECT user_id FROM pilot_profiles "
                f"WHERE user_id IN ({_in_clause(len(ids))}) AND license_status = %s",
                tuple(ids) + ("approved",),
            )
        except Exception as e:
            try:
                self.logger.warning(f"[console_content] 读取飞手执照状态失败（降级 false）: {e}")
            except Exception:
                pass
            return set()
        approved = set()
        for row in rows or []:
            try:
                approved.add(int(row["user_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return approved

    async def _load_work_titles(self, work_ids) -> dict:
        """批量读作品标题（comments 列表用）"""
        ids = sorted({int(i) for i in work_ids if i is not None})
        if not ids:
            return {}
        rows = await self.fetch_all(
            f"SELECT id, title FROM works WHERE id IN ({_in_clause(len(ids))})",
            tuple(ids),
        )
        titles = {}
        for row in rows or []:
            try:
                titles[int(row["id"])] = row.get("title")
            except (KeyError, TypeError, ValueError):
                continue
        return titles

    # ── 作品视图组装 ────────────────────────────────────────────

    async def _build_work_items(self, rows: list) -> list:
        """DB 行 -> 前端 Work 视图（§7.1）：join platform_users / pilot_profiles"""
        author_ids = [row.get("author_id") for row in rows or []]
        users = await self._load_users(author_ids)
        approved_ids = await self._load_approved_license_ids(author_ids)
        items = []
        for row in rows or []:
            author_id = row.get("author_id")
            author = None
            if author_id is not None:
                try:
                    author_id = int(author_id)
                except (TypeError, ValueError):
                    author_id = None
            if author_id is not None:
                user = users.get(author_id) or {}
                name = _clean_text(user.get("nickname")) or _clean_text(user.get("real_name"))
                if name is None:
                    # platform_users 不存在 / 用户已删除：降级用 author_id 展示
                    name = str(author_id)
                author = {
                    "id": author_id,
                    "name": name,
                    "avatar": _clean_text(user.get("avatar")),
                    "hasLicense": author_id in approved_ids,
                    "following": False,
                }
            task_type = row.get("task_type")
            items.append(
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "description": row.get("description"),
                    "cover": row.get("cover"),
                    "media": _json_list(row.get("media")),
                    "taskType": task_type,
                    "taskTypeLabel": _task_type_label(task_type),
                    "tags": _json_list(row.get("tags")),
                    "status": row.get("status"),
                    "likeCount": to_int(row.get("like_count"), 0),
                    "commentCount": to_int(row.get("comment_count"), 0),
                    "createdAt": to_iso(row.get("created_at")),
                    "author": author,
                    # 管理台是后台视角，没有「当前登录的 C 端用户」，
                    # 点赞 / 收藏关系表也不在本插件表结构内 -> 固定 false
                    "liked": False,
                    "favorited": False,
                    "rejectReason": row.get("reject_reason"),
                }
            )
        return items

    async def _work_counts(self) -> dict:
        """作品状态计数（补齐全部键；不随 keyword 过滤）"""
        counts = {"all": 0}
        for status in WORK_STATUSES:
            counts[status] = 0
        rows = await self.fetch_all(
            "SELECT status, COUNT(*) AS cnt FROM works GROUP BY status"
        )
        for row in rows or []:
            cnt = to_int(row.get("cnt"), 0)
            status = _clean_text(row.get("status")) or ""
            counts["all"] += cnt
            if status in counts and status != "all":
                counts[status] = cnt
        return counts

    # ── 举报视图组装 ────────────────────────────────────────────

    async def _build_report_items(self, rows: list) -> list:
        """DB 行 -> 前端 ReportItem 视图（§7.6）"""
        users = await self._load_users([row.get("reporter_id") for row in rows or []])
        items = []
        for row in rows or []:
            reporter_id = row.get("reporter_id")
            reporter = None
            if reporter_id is not None:
                try:
                    reporter_id = int(reporter_id)
                except (TypeError, ValueError):
                    reporter_id = None
            if reporter_id is not None:
                user = users.get(reporter_id) or {}
                reporter = {
                    "id": reporter_id,
                    "nickname": _clean_text(user.get("nickname"))
                    or _clean_text(user.get("real_name")),
                }
            items.append(
                {
                    "id": row.get("id"),
                    "reporter_id": row.get("reporter_id"),
                    "target_type": row.get("target_type"),
                    "target_id": row.get("target_id"),
                    "reason": row.get("reason"),
                    "detail": row.get("detail"),
                    "status": row.get("status"),
                    "handled_by": row.get("handled_by"),
                    "handle_note": row.get("handle_note"),
                    "created_at": to_iso(row.get("created_at")),
                    "reporter": reporter,
                }
            )
        return items

    # ── 数据报备转调 console_report（§9.1 双实现）──────────────

    def _report_plugin(self):
        """从 value.plugin.database_business 取 console_report 插件；取不到返回 None"""
        value = getattr(self, "value", None)
        holder = getattr(value, "plugin", None) if value is not None else None
        plugins = getattr(holder, "database_business", None) or {}
        try:
            return plugins.get("console_report")
        except Exception:
            return None

    async def _invoke_report_plugin(
        self, plugin, period: str, date_from: Optional[str], date_to: Optional[str]
    ) -> Any:
        """按约定方法名探测并调用 console_report 的数据报备方法

        各小组并行开发，方法名/参数名可能不同，这里先按参数名过滤再调用：
        支持 period / date_from / date_to / from_ / to_ / start / end 等命名；
        全部探测失败返回 None（由调用方兜底空结构，不报错）。
        """
        aliases = {
            "period": period,
            "period_type": period,
            "from": date_from,
            "from_": date_from,
            "date_from": date_from,
            "from_date": date_from,
            "start": date_from,
            "start_date": date_from,
            "begin": date_from,
            "to": date_to,
            "to_": date_to,
            "date_to": date_to,
            "to_date": date_to,
            "end": date_to,
            "end_date": date_to,
            "finish": date_to,
        }
        for method_name in REPORT_DATA_METHODS:
            method = getattr(plugin, method_name, None)
            if not callable(method):
                continue
            try:
                signature = inspect.signature(method)
                params = signature.parameters
                accepted = {k: v for k, v in aliases.items() if k in params}
                if accepted:
                    result = method(**accepted)
                else:
                    required = [
                        p
                        for p in params.values()
                        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                        and p.default is p.empty
                    ]
                    if not required:
                        result = method()
                    else:
                        # 兜底：按参数名猜位置参数（period/from/to 三类）
                        values = []
                        for p in required:
                            lower = p.name.lower()
                            if "period" in lower:
                                values.append(period)
                            elif lower in (
                                "from",
                                "from_",
                                "date_from",
                                "from_date",
                                "start",
                                "start_date",
                                "begin",
                            ):
                                values.append(date_from)
                            elif lower in (
                                "to",
                                "to_",
                                "date_to",
                                "to_date",
                                "end",
                                "end_date",
                                "finish",
                            ):
                                values.append(date_to)
                            else:
                                values.append(None)
                        result = method(*values)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception as e:
                try:
                    self.logger.warning(
                        f"[console_content] 转调 console_report.{method_name} 失败: {e}"
                    )
                except Exception:
                    pass
                continue
        return None

    async def _report_data(
        self, period: str, date_from: Optional[str], date_to: Optional[str]
    ) -> dict:
        """数据报备（§9.1）：转调 console_report；不可用时返回空结构不报错"""
        plugin = self._report_plugin()
        if plugin is None:
            return _empty_report_data(period, date_from, date_to)
        result = await self._invoke_report_plugin(plugin, period, date_from, date_to)
        if result is None:
            return _empty_report_data(period, date_from, date_to)
        return _normalize_report_data(result, period, date_from, date_to)

    # ── 请求体读取 ──────────────────────────────────────────────

    @staticmethod
    async def _read_body(request: Request, required: bool = True) -> dict:
        """读取 JSON 请求体（对象）；非法 JSON / 空体按 400/1001 处理"""
        try:
            raw = await request.body()
        except Exception:
            raw = b""
        if not raw or not raw.strip():
            if required:
                raise ApiError(CODE_PARAM, "请求体不能为空", 400)
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise ApiError(CODE_PARAM, "请求体必须是合法的 JSON", 400)
        if not isinstance(data, dict):
            raise ApiError(CODE_PARAM, "请求体必须是 JSON 对象", 400)
        return data

    # ── 路由注册 ────────────────────────────────────────────────

    def register_routes(self, router: APIRouter) -> None:
        self._route(router)

    def _route(self, router: APIRouter) -> None:
        # ------------------------------------------------------------------ #
        # 25. GET /admin/works —— 作品队列（不分页，上限 200）
        # ------------------------------------------------------------------ #
        @router.get("/works")
        async def list_works(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            query = request.query_params
            status = (_clean_text(query.get("status")) or "pending").lower()
            keyword = _clean_text(query.get("keyword"))
            if status not in ("pending", "approved", "rejected", "offline", "all"):
                raise ApiError(
                    CODE_PARAM,
                    "status 仅支持 pending / approved / rejected / offline / all",
                    400,
                )
            where = []
            params = []
            if status != "all":
                where.append("status = %s")
                params.append(status)
            if keyword:
                like = f"%{keyword}%"
                where.append("(title LIKE %s OR description LIKE %s)")
                params.extend([like, like])
            sql = f"SELECT {', '.join(WORK_COLUMNS)} FROM works"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT %s"
            params.append(LIST_LIMIT)
            rows = await self.fetch_all(sql, tuple(params))
            items = await self._build_work_items(rows)
            counts = await self._work_counts()
            return ok({"list": items, "counts": counts})

        # ------------------------------------------------------------------ #
        # 26. POST /admin/works/{id}/audit —— 作品审核
        # ------------------------------------------------------------------ #
        @router.post("/works/{work_id}/audit")
        async def audit_work(
            work_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            body = await self._read_body(request)
            if "approved" not in body:
                raise ApiError(CODE_PARAM, "approved 必填（布尔值）", 400)
            approved = _as_bool(body.get("approved"))
            if approved is None:
                raise ApiError(CODE_PARAM, "approved 必须是布尔值", 400)
            reason = _clean_text(body.get("reason"))
            if not approved:
                if reason is None:
                    raise ApiError(CODE_PARAM, "驳回时必须填写驳回原因", 400)
                if not _len_between(reason, 1, 200):
                    raise ApiError(CODE_PARAM, "驳回原因不能超过 200 字", 400)
            else:
                # 通过时忽略驳回原因
                reason = None

            row = await self.fetch_one(
                "SELECT id, status FROM works WHERE id = %s", (work_id,)
            )
            if not row:
                raise ApiError(CODE_NOT_FOUND, "作品不存在", 404)
            current_status = _clean_text(row.get("status")) or "pending"
            if current_status != "pending":
                if current_status == "offline":
                    raise ApiError(CODE_CONFLICT, "该作品已下架，无法审核", 409)
                raise ApiError(CODE_CONFLICT, "该作品已审核", 409)

            target_status = "approved" if approved else "rejected"
            changed = await self.execute(
                "UPDATE works SET status = %s, reject_reason = %s, reviewed_at = %s "
                "WHERE id = %s AND status = 'pending'",
                (target_status, reason, now_dt(), work_id),
            )
            if not changed:
                # 并发审核：状态已被别的请求改掉
                raise ApiError(CODE_CONFLICT, "该作品已审核，请刷新后重试", 409)

            await audit(
                request,
                admin,
                module="内容管理",
                action="audit_work",
                target_type="work",
                target_id=work_id,
                detail={
                    "approved": approved,
                    "reason": reason,
                    "from": current_status,
                    "to": target_status,
                },
            )
            return ok({"id": work_id, "status": target_status})

        # ------------------------------------------------------------------ #
        # 27. POST /admin/works/{id}/offline —— 作品下架
        # ------------------------------------------------------------------ #
        @router.post("/works/{work_id}/offline")
        async def offline_work(
            work_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            body = await self._read_body(request)
            reason = _clean_text(body.get("reason"))
            if reason is None:
                raise ApiError(CODE_PARAM, "下架原因必填（2-200 字）", 400)
            if not _len_between(reason, 2, 200):
                raise ApiError(CODE_PARAM, "下架原因需为 2-200 字", 400)

            row = await self.fetch_one(
                "SELECT id, status FROM works WHERE id = %s", (work_id,)
            )
            if not row:
                raise ApiError(CODE_NOT_FOUND, "作品不存在", 404)
            current_status = _clean_text(row.get("status")) or "pending"
            if current_status == "offline":
                raise ApiError(CODE_CONFLICT, "该作品已下架", 409)

            # 表结构无 offline_reason 字段：下架原因复用 reject_reason 供前端展示，
            # 同时完整记录到操作日志（audit 的 detail）
            changed = await self.execute(
                "UPDATE works SET status = 'offline', reject_reason = %s, reviewed_at = %s "
                "WHERE id = %s AND status <> 'offline'",
                (reason, now_dt(), work_id),
            )
            if not changed:
                raise ApiError(CODE_CONFLICT, "该作品已下架，请刷新后重试", 409)

            await audit(
                request,
                admin,
                module="内容管理",
                action="offline_work",
                target_type="work",
                target_id=work_id,
                detail={"reason": reason, "from": current_status, "to": "offline"},
            )
            return ok({"id": work_id, "status": "offline"})

        # ------------------------------------------------------------------ #
        # 28. GET /admin/comments —— 评论列表（分页）
        # ------------------------------------------------------------------ #
        @router.get("/comments")
        async def list_comments(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            page, page_size = parse_page(request, default=20)
            query = request.query_params
            keyword = _clean_text(query.get("keyword"))
            raw_work_id = _clean_text(query.get("workId"))
            work_id = None
            if raw_work_id is not None:
                work_id = to_int(raw_work_id, 0)
                if work_id <= 0:
                    raise ApiError(CODE_PARAM, "workId 必须是正整数", 400)

            where = ["deleted = 0"]
            params = []
            if work_id is not None:
                where.append("work_id = %s")
                params.append(work_id)
            if keyword:
                where.append("content LIKE %s")
                params.append(f"%{keyword}%")
            where_sql = " WHERE " + " AND ".join(where)

            total_row = await self.fetch_one(
                f"SELECT COUNT(*) AS total FROM comments{where_sql}", tuple(params)
            )
            total = to_int((total_row or {}).get("total"), 0)

            offset = (page - 1) * page_size
            rows = await self.fetch_all(
                f"SELECT id, work_id, user_id, content, created_at FROM comments"
                f"{where_sql} ORDER BY id DESC LIMIT %s OFFSET %s",
                tuple(params) + (page_size, offset),
            )

            users = await self._load_users([row.get("user_id") for row in rows or []])
            titles = await self._load_work_titles([row.get("work_id") for row in rows or []])
            items = []
            for row in rows or []:
                user_id = row.get("user_id")
                nickname = None
                if user_id is not None:
                    try:
                        user = users.get(int(user_id)) or {}
                    except (TypeError, ValueError):
                        user = {}
                    nickname = _clean_text(user.get("nickname")) or _clean_text(
                        user.get("real_name")
                    )
                work_id_value = row.get("work_id")
                work_title = None
                if work_id_value is not None:
                    try:
                        work_title = titles.get(int(work_id_value))
                    except (TypeError, ValueError):
                        work_title = None
                items.append(
                    {
                        "id": row.get("id"),
                        "work_id": work_id_value,
                        "user_id": user_id,
                        "content": row.get("content"),
                        "created_at": to_iso(row.get("created_at")),
                        "user_nickname": nickname,
                        "work_title": work_title,
                    }
                )
            return ok(paged(items, total, page, page_size))

        # ------------------------------------------------------------------ #
        # 29. POST /admin/comments/{id}/delete —— 评论软删（无请求体）
        # ------------------------------------------------------------------ #
        @router.post("/comments/{comment_id}/delete")
        async def delete_comment(
            comment_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            row = await self.fetch_one(
                "SELECT id, work_id, user_id, deleted FROM comments WHERE id = %s",
                (comment_id,),
            )
            if not row:
                raise ApiError(CODE_NOT_FOUND, "评论不存在", 404)
            already_deleted = to_int(row.get("deleted"), 0) == 1
            if not already_deleted:
                changed = await self.execute(
                    "UPDATE comments SET deleted = 1 WHERE id = %s AND deleted = 0",
                    (comment_id,),
                )
                if changed:
                    # 同步作品评论数（管理台删除后列表计数保持一致，最小 0）
                    work_id = row.get("work_id")
                    if work_id is not None:
                        try:
                            await self.execute(
                                "UPDATE works SET comment_count = GREATEST(comment_count - 1, 0) "
                                "WHERE id = %s",
                                (int(work_id),),
                            )
                        except Exception as e:
                            try:
                                self.logger.warning(
                                    f"[console_content] 同步作品评论数失败（不影响删除）: {e}"
                                )
                            except Exception:
                                pass
                await audit(
                    request,
                    admin,
                    module="内容管理",
                    action="delete_comment",
                    target_type="comment",
                    target_id=comment_id,
                    detail={"workId": row.get("work_id"), "userId": row.get("user_id")},
                )
            return ok({"id": comment_id, "deleted": True})

        # ------------------------------------------------------------------ #
        # 30. GET /admin/reports —— 双实现：举报列表 / 数据报备
        # ------------------------------------------------------------------ #
        @router.get("/reports")
        async def list_reports(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            query = request.query_params
            period = _clean_text(query.get("period"))
            date_from = _clean_text(query.get("from"))
            date_to = _clean_text(query.get("to"))
            # 带 period/from/to 任一参数 -> 数据报备（§9.1），转调 console_report
            if period is not None or date_from is not None or date_to is not None:
                data = await self._report_data(period or "day", date_from, date_to)
                return ok(data)

            # 否则 -> 举报列表（§7.6，不分页）
            status = (_clean_text(query.get("status")) or "pending").lower()
            if status not in REPORT_STATUSES:
                raise ApiError(
                    CODE_PARAM,
                    "status 仅支持 pending / handled / rejected",
                    400,
                )
            rows = await self.fetch_all(
                "SELECT id, reporter_id, target_type, target_id, reason, detail, status, "
                "handled_by, handle_note, created_at FROM content_reports "
                "WHERE status = %s ORDER BY id DESC LIMIT %s",
                (status, LIST_LIMIT),
            )
            items = await self._build_report_items(rows)
            return ok({"list": items})

        # ------------------------------------------------------------------ #
        # 31. POST /admin/reports/{id}/handle —— 举报处理
        # ------------------------------------------------------------------ #
        @router.post("/reports/{report_id}/handle")
        async def handle_report(
            report_id: int,
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            body = await self._read_body(request)
            action = (_clean_text(body.get("action")) or "").lower()
            if action not in ("handled", "rejected"):
                raise ApiError(CODE_PARAM, "action 仅支持 handled / rejected", 400)
            note = _clean_text(body.get("note"))
            if note is None:
                raise ApiError(CODE_PARAM, "处理备注必填（2-300 字）", 400)
            if not _len_between(note, 2, 300):
                raise ApiError(CODE_PARAM, "处理备注需为 2-300 字", 400)

            row = await self.fetch_one(
                "SELECT id, status FROM content_reports WHERE id = %s", (report_id,)
            )
            if not row:
                raise ApiError(CODE_NOT_FOUND, "举报不存在", 404)
            current_status = _clean_text(row.get("status")) or "pending"
            if current_status != "pending":
                raise ApiError(CODE_CONFLICT, "该举报已处理", 409)

            changed = await self.execute(
                "UPDATE content_reports SET status = %s, handled_by = %s, "
                "handle_note = %s, handled_at = %s WHERE id = %s AND status = 'pending'",
                (action, admin.get("id"), note, now_dt(), report_id),
            )
            if not changed:
                raise ApiError(CODE_CONFLICT, "该举报已处理，请刷新后重试", 409)

            await audit(
                request,
                admin,
                module="内容管理",
                action="handle_report",
                target_type="report",
                target_id=report_id,
                detail={"action": action, "note": note, "from": current_status, "to": action},
            )
            return ok({"id": report_id, "status": action})

        # ------------------------------------------------------------------ #
        # 32. GET /admin/channels —— 频道开关读取
        # ------------------------------------------------------------------ #
        @router.get("/channels")
        async def get_channels(request: Request, admin: dict = Depends(admin_guard())):
            require_db(self)
            rows = await self.fetch_all(
                "SELECT `key`, enabled FROM console_channels"
            )
            mapping = {}
            for row in rows or []:
                key = _clean_text(row.get("key"))
                if key:
                    mapping[key] = bool(to_int(row.get("enabled"), 0))
            if any(key not in mapping for key in CHANNEL_KEYS):
                # 表里缺行时补一次（initialize 已 INSERT IGNORE，这里兜底）
                await self._ensure_channels()
                mapping = {key: mapping.get(key, False) for key in CHANNEL_KEYS}
            return ok({key: bool(mapping.get(key, False)) for key in CHANNEL_KEYS})

        # ------------------------------------------------------------------ #
        # 33. PUT /admin/channels —— 频道开关修改（任意子集）
        # ------------------------------------------------------------------ #
        @router.put("/channels")
        async def put_channels(
            request: Request,
            admin: dict = Depends(admin_guard(write=True)),
        ):
            require_db(self)
            body = await self._read_body(request)
            changes = {}
            for key in CHANNEL_KEYS:
                if key not in body:
                    continue
                value = _as_bool(body.get(key))
                if value is None:
                    raise ApiError(CODE_PARAM, f"{key} 必须是布尔值", 400)
                changes[key] = value
            if not changes:
                raise ApiError(
                    CODE_PARAM,
                    "至少需要提供一个频道开关（rent / repair / training）",
                    400,
                )
            for key, enabled in changes.items():
                await self.execute(
                    "INSERT INTO console_channels (`key`, enabled) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE enabled = %s",
                    (key, 1 if enabled else 0, 1 if enabled else 0),
                )
            await audit(
                request,
                admin,
                module="内容管理",
                action="update_channels",
                target_type="channel",
                target_id=",".join(sorted(changes)),
                detail={key: changes[key] for key in changes},
            )
            return ok({"ok": True})
