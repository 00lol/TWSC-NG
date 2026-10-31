# -*- coding:utf-8 -*-
"""管理台演示数据（种子数据）

触发条件：``SERVER_CONSOLE_SEED=True`` 且库里还没有管理台数据（以 console_admins 是否为空判断）。
目的：前端 14 个页面接上后端后立刻有数据可看，不用手工造数。

写入顺序固定（有外键语义，用显式 id 保证跨插件引用一致）：
管理员/白名单 -> 平台用户/档案/认证/需求 -> 作品/评论/举报/频道 -> 订单及明细
-> 活动/消息 -> 参数/历史/对接服务/指标快照 -> 操作日志

只插一次，不覆盖已有数据；任何一段失败只记日志，不影响其它段与服务启动。
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TZ = timezone(timedelta(hours=8))

# 账号（与 docs/后端实现约定.md §8 一致）
ACCOUNTS = [
    # id, username, name, role, phone, password
    (1, "admin_super", "超级管理员", "super", "13800000001", "Admin@12345"),
    (2, "op_demo", "运营小张", "op", "13800000002", "Op@12345"),
    (3, "dev_demo", "开发者小李", "developer", "13800000003", "Dev@12345"),
]

WHITELIST = [
    (1, "13800000001", "super", 1),
    (2, "13800000002", "op", 1),
    (3, "13800000003", "developer", 1),
    (4, "13900000000", "developer", 1),  # 待注册
]

PILOT_NAMES = ["飞手小王", "航拍老李", "云台阿飞", "镜头行者", "空中视角", "老陈航拍", "小周飞手", "云上摄影",
               "疾风飞手", "夜航小赵", "大疆玩家", "飞影阿杰", "山野航拍", "晓峰航拍", "川行飞手", "逸飞航拍",
               "小鹿航拍", "北辰飞手", "海风航拍", "老胡飞手"]
CLIENT_NAMES = ["某科技有限公司", "杭州影像工作室", "西湖文旅", "城建集团", "传媒小刘", "地产推广部",
                "广告公司A", "婚礼策划", "测绘公司", "农业合作社", "学校宣传处", "景区运营部",
                "新闻中心", "民宿老板", "电商摄影"]

PROFILE_BIO = ["专注航拍 5 年", "持证飞手，可夜航", "擅长建筑与风光", "接单快，出片稳", "自有 M300"]


def _now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def _dt(days: int = 0, hours: int = 0) -> str:
    return (_now() - timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _dt_before(days: int = 0, hours: int = 0) -> str:
    """N 天前"""
    return _dt(days=days, hours=hours)


def _dt_after(days: int = 0, hours: int = 0) -> str:
    """N 天后（负数天数即未来的可读写法）"""
    return _dt(days=-days, hours=-hours)


def _day(days: int = 0) -> str:
    return (_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


async def run(value: Any) -> bool:
    """执行种子数据；返回 True 表示本次写入了数据，False 表示已存在/跳过"""
    logger = getattr(value, "logger", None)
    database = getattr(value, "database", None)

    def log(level: str, message: str):
        if logger is None:
            return
        try:
            getattr(logger, level)(message)
        except Exception:
            pass

    if database is None or not getattr(database, "connected", False):
        log("warning", "数据库不可用，跳过演示数据")
        return False

    # 是否已灌过：有管理员就不再重复
    try:
        row = await database.fetch_one("SELECT COUNT(*) AS c FROM console_admins")
        if row and int(row.get("c") or 0) > 0:
            log("debug", "管理台演示数据已存在，跳过")
            return False
    except Exception as e:
        log("warning", f"检查演示数据失败（继续尝试）: {e}")

    from transaction.auth import Password

    steps = [
        ("管理员", _accounts),
        ("白名单", _whitelist),
        ("平台用户", _users),
        ("用户档案", _profiles),
        ("认证记录", _verifies),
        ("需求", _demands),
        ("作品/评论/举报/频道", _content),
        ("订单", _orders),
        ("活动/消息", _activities),
        ("参数/对接/快照", _ops),
        ("操作日志", _audit),
    ]
    done = []
    for name, func in steps:
        try:
            await func(database, Password)
            done.append(name)
        except Exception as e:
            log("error", f"演示数据[{name}]写入失败（不影响其它段）: {e}")

    log("info", f"演示数据写入完成：{', '.join(done)}")
    return bool(done)


# --------------------------------------------------------------------------- #
# 各段数据
# --------------------------------------------------------------------------- #
async def _accounts(db, Password):
    for admin_id, username, name, role, phone, pwd in ACCOUNTS:
        await db.execute(
            "INSERT INTO console_admins (id, username, password_hash, name, role, phone, status, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'active',%s)",
            (admin_id, username, Password.hash(pwd), name, role, phone, _dt(days=30)),
        )


async def _whitelist(db, Password):
    for wl_id, phone, role, created_by in WHITELIST:
        await db.execute(
            "INSERT INTO console_whitelist (id, phone, role, created_by, created_at, removed_at) "
            "VALUES (%s,%s,%s,%s,%s,NULL)",
            (wl_id, phone, role, created_by, _dt(days=25)),
        )


async def _users(db, Password):
    """35 个平台用户：1-20 飞手，21-35 客户"""
    rows = []
    for user_id in range(1, 36):
        if user_id <= 20:
            nickname = PILOT_NAMES[user_id - 1]
            role, phone = "pilot", f"138{user_id:08d}"
        else:
            nickname = CLIENT_NAMES[user_id - 21]
            role, phone = "client", f"139{user_id:08d}"
        status = "banned" if user_id == 5 else "active"       # 一个被封禁的飞手
        verified = 1 if user_id in (1, 2, 3, 4, 21, 22, 23) else 0
        real_name = f"飞手{user_id:02d}" if role == "pilot" else f"客户{user_id:02d}"
        rows.append((
            user_id, nickname, phone, Password.hash("User@12345"), role, verified, real_name,
            status, _dt(days=60 - user_id), _dt(days=60 - user_id),
        ))
    await db.execute_many(
        "INSERT INTO platform_users "
        "(id, nickname, phone, password_hash, role, real_verified, real_name, status, created_at, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        rows,
    )


async def _profiles(db, Password):
    intents = json.dumps(["aerial"], ensure_ascii=False)
    skill = json.dumps(["航拍", "夜景"], ensure_ascii=False)
    pilot_rows = []
    for user_id in range(1, 21):
        status = "approved" if user_id <= 4 else ("pending" if user_id <= 6 else "approved")
        pilot_rows.append((
            user_id,
            f"LIC{user_id:06d}" if user_id <= 6 else None,
            status,                                    # license_status
            "approved" if user_id <= 4 else "pending",  # identity_status
            intents, 5 + user_id % 20, 2 + user_id % 3, 4.5 + (user_id % 5) / 10,
            PROFILE_BIO[user_id % len(PROFILE_BIO)],
        ))
    await db.execute_many(
        "INSERT INTO pilot_profiles "
        "(user_id, license_no, license_status, identity_status, intent_types, avail_days, skill_count, rating, bio) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        pilot_rows,
    )
    client_rows = []
    for user_id in range(21, 36):
        kind = "company" if user_id % 3 == 0 else "personal"
        company = CLIENT_NAMES[user_id - 21] if kind == "company" else None
        client_rows.append((user_id, company, kind, "approved" if user_id <= 23 else "pending"))
    await db.execute_many(
        "INSERT INTO client_profiles (user_id, company_name, kind, identity_status) VALUES (%s,%s,%s,%s)",
        client_rows,
    )


async def _verifies(db, Password):
    rows = [
        (1, 3, "identity", "pending", "飞手03", None, _dt(days=2), None),
        (2, 4, "license", "pending", "飞手04", "LIC000004", _dt(days=1), None),
        (3, 1, "identity", "approved", "飞手01", None, _dt(days=20), _dt(days=19)),
        (4, 2, "license", "approved", "飞手02", "LIC000002", _dt(days=18), _dt(days=17)),
        (5, 21, "identity", "approved", "客户21", None, _dt(days=15), _dt(days=14)),
    ]
    for vid, uid, vtype, status, name, lic, submitted, reviewed in rows:
        await db.execute(
            "INSERT INTO verify_records "
            "(id, user_id, verify_type, status, real_name, license_no, id_card_enc, reason, "
            " submitted_at, reviewed_at, reviewer_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)",
            (vid, uid, vtype, status, name, lic, "ENC_SAMPLE_ONLY", submitted, reviewed,
             None if reviewed is None else 1),
        )


async def _demands(db, Password):
    rows = []
    for i in range(1, 49):
        user_id = 21 + (i % 15)
        status = "recruiting" if i <= 10 else ("closed" if i <= 40 else "expired")
        rows.append((
            i, user_id, f"航拍需求 #{i:02d}", "aerial",
            f"需要航拍服务，地点杭州，需求编号 {i}", 20000 + (i % 5) * 5000, status, _dt(days=45 - i % 45),
        ))
    await db.execute_many(
        "INSERT INTO demands (id, user_id, title, task_type, description, budget, status, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        rows,
    )


async def _content(db, Password):
    works = []
    for i in range(1, 8):
        status = "pending" if i <= 2 else "approved"
        works.append((
            i, 1 + (i % 20), f"西湖航拍 #{i}", f"作品描述 {i}",
            f"cos://cover-{i}.jpg", json.dumps([f"cos://media-{i}-1.jpg"], ensure_ascii=False),
            "aerial", json.dumps(["夜景"] if i % 2 else ["风光"], ensure_ascii=False),
            status, 3 + i, i % 3, None, _dt(days=10 - i), None if status == "pending" else _dt(days=9 - i),
        ))
    await db.execute_many(
        "INSERT INTO works "
        "(id, author_id, title, description, cover, media, task_type, tags, status, "
        " like_count, comment_count, reject_reason, created_at, reviewed_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        works,
    )

    comments = [
        (1, 3, 21, "拍得真好", 0, _dt(days=5)),
        (2, 4, 22, "这个机位在哪", 0, _dt(days=4)),
        (3, 1, 23, "已三连", 0, _dt(days=3)),
    ]
    for cid, wid, uid, content, deleted, created in comments:
        await db.execute(
            "INSERT INTO comments (id, work_id, user_id, content, deleted, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (cid, wid, uid, content, deleted, created),
        )

    await db.execute(
        "INSERT INTO content_reports "
        "(id, reporter_id, target_type, target_id, reason, detail, status, handled_by, handle_note, created_at) "
        "VALUES (1, 7, 'work', 5, '盗图', '疑似盗用他人作品', 'pending', NULL, NULL, %s)",
        (_dt(days=2),),
    )

    for key, enabled in (("rent", 1), ("repair", 0), ("training", 1)):
        await db.execute(
            "INSERT IGNORE INTO console_channels (`key`, enabled) VALUES (%s,%s)",
            (key, enabled),
        )


async def _orders(db, Password):
    """6 个订单：覆盖状态机全部分支（待付款/材料审核/履约中/已交付/待结算/争议）"""
    orders = [
        # id, order_no, title, status, amount, rate, commission, settle, city, district,
        # client, pilot, created, paid, delivered, accepted
        (1, "OD20261001001", "航拍巡检（待付款）", "pending_pay", 32000, 0.1, 3200, 28800,
         "杭州市", "西湖区", 21, None, _dt(days=1), None, None, None),
        (2, "OD20261001002", "厂区航拍（材料审核）", "material_review", 48000, 0.1, 4800, 43200,
         "宁波市", "鄞州区", 22, 1, _dt(days=3), _dt(days=3), None, None),
        (3, "OD20261001003", "城市夜景（履约中）", "in_progress", 36000, 0.1, 3600, 32400,
         "杭州市", "上城区", 23, 2, _dt(days=5), _dt(days=5), None, None),
        (4, "OD20261001004", "婚礼跟拍（已交付）", "delivered", 28000, 0.1, 2800, 25200,
         "绍兴市", "越城区", 24, 3, _dt(days=7), _dt(days=7), _dt(days=2), None),
        (5, "OD20261001005", "活动跟拍（待结算）", "accepted", 32000, 0.1, 3200, 28800,
         "杭州市", "余杭区", 25, 4, _dt(days=9), _dt(days=9), _dt(days=3), _dt(days=1)),
        (6, "OD20261001006", "工程测绘（争议中）", "disputed", 52000, 0.1, 5200, 46800,
         "嘉兴市", "南湖区", 26, 5, _dt(days=12), _dt(days=12), _dt(days=5), None),
    ]
    for (oid, order_no, title, status, amount, rate, commission, settle,
         city, district, client, pilot, created, paid, delivered, accepted) in orders:
        await db.execute(
            "INSERT INTO orders "
            "(id, order_no, title, status, amount, commission_rate, commission_amount, settle_amount, "
            " city, district, address, client_id, pilot_id, remark, created_at, paid_at, delivered_at, accepted_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s)",
            (oid, order_no, title, status, amount, rate, commission, settle, city, district,
             f"{city}{district}示例地址", client, pilot, created, paid, delivered, accepted),
        )

    timeline_defs = [
        # order_id, from, to, action, remark, operator_type, operator_id, created_at
        (1, None, "pending_pay", "create", "创建订单", "user", 21, _dt(days=1)),
        (2, None, "pending_pay", "create", "创建订单", "user", 22, _dt(days=3)),
        (2, "pending_pay", "material_review", "pay", "支付成功", "user", 22, _dt(days=3)),
        (3, None, "pending_pay", "create", "创建订单", "user", 23, _dt(days=5)),
        (3, "pending_pay", "material_review", "pay", "支付成功", "user", 23, _dt(days=5)),
        (3, "material_review", "in_progress", "approve_material", "材料审核通过", "admin", 1, _dt(days=4)),
        (4, None, "pending_pay", "create", "创建订单", "user", 24, _dt(days=7)),
        (4, "pending_pay", "material_review", "pay", "支付成功", "user", 24, _dt(days=7)),
        (4, "material_review", "in_progress", "approve_material", "材料审核通过", "admin", 1, _dt(days=6)),
        (4, "in_progress", "delivered", "deliver", "飞手提交交付物", "user", 3, _dt(days=2)),
        (5, None, "pending_pay", "create", "创建订单", "user", 25, _dt(days=9)),
        (5, "pending_pay", "material_review", "pay", "支付成功", "user", 25, _dt(days=9)),
        (5, "material_review", "in_progress", "approve_material", "材料审核通过", "admin", 1, _dt(days=8)),
        (5, "in_progress", "delivered", "deliver", "飞手提交交付物", "user", 4, _dt(days=3)),
        (5, "delivered", "accepted", "accept", "客户验收通过", "user", 25, _dt(days=1)),
        (6, None, "pending_pay", "create", "创建订单", "user", 26, _dt(days=12)),
        (6, "pending_pay", "material_review", "pay", "支付成功", "user", 26, _dt(days=12)),
        (6, "material_review", "in_progress", "approve_material", "材料审核通过", "admin", 1, _dt(days=10)),
        (6, "in_progress", "delivered", "deliver", "飞手提交交付物", "user", 5, _dt(days=6)),
        (6, "delivered", "disputed", "dispute", "客户发起争议", "user", 26, _dt(days=5)),
    ]
    for idx, (oid, from_status, to_status, action, remark, optype, opid, created) in enumerate(timeline_defs, start=1):
        await db.execute(
            "INSERT INTO order_timeline "
            "(id, order_id, from_status, to_status, action, remark, operator_type, operator_id, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (idx, oid, from_status, to_status, action, remark, optype, opid, created),
        )

    reports = [
        (1, 2, _dt(days=2), "宁波鄞州区某厂区", "M300"),
        (2, 3, _dt(days=4), "杭州上城区钱江新城", "Mavic 3E"),
        (3, 4, _dt(days=6), "绍兴越城区某酒店", "M300"),
        (4, 5, _dt(days=8), "杭州余杭区某园区", "Mavic 3E"),
        (5, 6, _dt(days=10), "嘉兴南湖区某工地", "M300"),
    ]
    for rid, oid, flight, location, model in reports:
        await db.execute(
            "INSERT INTO order_reports (id, order_id, flight_time, location, drone_model, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (rid, oid, flight, location, model, _dt(days=2)),
        )

    for pid, oid, amount, trade_no, paid_at in [
        (1, 2, 48000, "PAY-OD20261001002", _dt(days=3)),
        (2, 3, 36000, "PAY-OD20261001003", _dt(days=5)),
        (3, 4, 28000, "PAY-OD20261001004", _dt(days=7)),
        (4, 5, 32000, "PAY-OD20261001005", _dt(days=9)),
        (5, 6, 52000, "PAY-OD20261001006", _dt(days=12)),
    ]:
        await db.execute(
            "INSERT INTO order_payments (id, order_id, type, amount, status, provider, out_trade_no, paid_at) "
            "VALUES (%s,%s,'pay',%s,'success','mock',%s,%s)",
            (pid, oid, amount, trade_no, paid_at),
        )

    deliverables = [
        (1, 4, "video", "成片", 1, "pending", None),
        (2, 5, "video", "成片", 1, "done", _dt(days=1)),
        (3, 5, "photo", "原片", 1, "done", _dt(days=1)),
        (4, 6, "video", "成片", 1, "done", _dt(days=5)),
    ]
    for did, oid, code, name, required, status, confirmed in deliverables:
        await db.execute(
            "INSERT INTO order_deliverables "
            "(id, order_id, code, name, required, status, confirmed_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (did, oid, code, name, required, status, confirmed),
        )
    await db.execute(
        "INSERT INTO order_deliverable_files (id, deliverable_id, file_key, file_name, size, created_at) "
        "VALUES (1, 2, 'cos://order5/a.mp4', 'a.mp4', 1048576, %s)",
        (_dt(days=1),),
    )

    await db.execute("INSERT INTO conversations (id, order_id, created_at) VALUES (1, 2, %s)", (_dt(days=3),))
    await db.execute("INSERT INTO conversations (id, order_id, created_at) VALUES (2, 3, %s)", (_dt(days=5),))
    await db.execute(
        "INSERT INTO messages (id, conv_id, sender_id, sender_name, type, content, created_at) "
        "VALUES (1, 1, 22, '客户22', 'text', '你好，什么时候能飞？', %s)",
        (_dt(days=3),),
    )
    await db.execute(
        "INSERT INTO messages (id, conv_id, sender_id, sender_name, type, content, created_at) "
        "VALUES (2, 2, 2, '飞手02', 'text', '明天上午可以起飞', %s)",
        (_dt(days=4),),
    )


async def _activities(db, Password):
    await db.execute(
        "INSERT INTO activities "
        "(id, title, start_at, end_at, body_html, cover_key, stored_status, publisher_id, published_at, created_at, updated_at) "
        "VALUES (1, '双十一航拍优惠', %s, %s, '<p>活动正文示例</p>', 'cos://act1.jpg', 'draft', 1, NULL, %s, %s)",
        (_dt_after(3), _dt_after(10), _dt(days=2), _dt(days=2)),
    )
    await db.execute(
        "INSERT INTO activities "
        "(id, title, start_at, end_at, body_html, cover_key, stored_status, publisher_id, published_at, created_at, updated_at) "
        "VALUES (2, '冬季航拍征集', %s, %s, '<p>已发布活动正文</p>', 'cos://act2.jpg', 'published', 1, %s, %s, %s)",
        (_dt_before(1), _dt_after(7), _dt(days=1), _dt(days=2), _dt(days=1)),
    )

    await db.execute(
        "INSERT INTO notices (id, title, body, status, publisher_id, published_at, removed_at, created_at) "
        "VALUES (1, '系统维护通知（草稿）', '本周日凌晨维护', 'draft', 1, NULL, NULL, %s)",
        (_dt(days=1),),
    )
    await db.execute(
        "INSERT INTO notices (id, title, body, status, publisher_id, published_at, removed_at, created_at) "
        "VALUES (2, '平台上线公告', '平台正式上线，欢迎使用', 'published', 1, %s, NULL, %s)",
        (_dt(days=1), _dt(days=2)),
    )


async def _ops(db, Password):
    params = [
        ("commission_rate", "佣金比例", "", "float", 0.1, "仅对之后新成交的订单生效"),
        ("settle_days", "验收后结算", "天", "int", 1, "T+n，影响新验收订单"),
        ("auto_accept_days", "自动验收", "天", "int", 1, "交付后 n 天自动验收"),
        ("pending_pay_minutes", "待付款超时", "分钟", "int", 15, "超时自动取消"),
        ("application_expire_days", "报名有效期", "天", "int", 7, "需求报名有效期"),
        ("recommend_limit", "推荐位数量", "个", "int", 10, "首页推荐位上限"),
        ("withdraw_min", "最低提现", "分", "int", 100, "低于该金额不可提现"),
    ]
    for key, label, unit, vtype, value, rule in params:
        await db.execute(
            "INSERT IGNORE INTO console_params (`key`, label, unit, value_type, value_num, rule_text) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (key, label, unit, vtype, value, rule),
        )

    for i, (key, label, old, new) in enumerate([
        ("commission_rate", "佣金比例", "0.08", "0.1"),
        ("settle_days", "验收后结算", "3", "1"),
    ], start=1):
        await db.execute(
            "INSERT INTO console_param_history "
            "(id, `key`, label, old_value, new_value, reason, changed_by, changed_by_name, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,'超级管理员',%s)",
            (i, key, label, old, new, "运营参数调整", _dt(days=5 - i)),
        )

    integrations = [
        ("wxpay", 0, None, "unset", None, None),
        ("sms", 0, None, "unset", None, None),
        ("oss", 0, json.dumps({"bucket": "twsc-demo"}, ensure_ascii=False), "unset", "配置不完整", None),
        ("push", 0, None, "unset", None, None),
    ]
    for service, enabled, config, status, fail, checked in integrations:
        await db.execute(
            "INSERT IGNORE INTO console_integrations "
            "(service, enabled, config, status, fail_reason, checked_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (service, enabled, config, status, fail, checked),
        )
    # oss 预置一个「已配置但不完整」的样例，让前端能看到脱敏展示
    await db.execute(
        "UPDATE console_integrations SET config = %s, status = 'unset', fail_reason = '配置不完整' "
        "WHERE service = 'oss' AND (config IS NULL OR JSON_LENGTH(config) = 0)",
        (json.dumps({"bucket": "twsc-demo"}, ensure_ascii=False),),
    )

    await db.execute(
        "INSERT INTO console_report_snapshots (id, trigger_event, metrics, created_at) "
        "VALUES (1, '参数修改 佣金比例: 0.08 → 0.1', %s, %s)",
        (json.dumps({"orders": 3, "gmv": 112000, "commission": 11200, "users": 35}, ensure_ascii=False),
         _dt(days=4)),
    )


async def _audit(db, Password):
    rows = [
        (1, "超级管理员", "订单管理", "force_cancel_order", "order", "1", {"reason": "重复下单"}, _dt(days=3)),
        (2, "运营小张", "内容管理", "audit_work", "work", "3", {"approved": True}, _dt(days=4)),
        (3, "超级管理员", "数据修改", "update_params", "params", "commission_rate",
         {"old": 0.08, "new": 0.1}, _dt(days=4)),
        (4, "运营小张", "用户管理", "audit_user", "user", "3", {"verifyType": "identity"}, _dt(days=2)),
        (5, "超级管理员", "对接配置", "detect_integration", "integration", "oss", {"status": "failed"}, _dt(days=1)),
    ]
    for aid, actor, module, action, ttype, tid, detail, created in rows:
        await db.execute(
            "INSERT INTO console_audit_logs "
            "(id, actor_id, actor_name, module, action, target_type, target_id, detail, result, ip, created_at) "
            "VALUES (%s,1,%s,%s,%s,%s,%s,%s,'success','127.0.0.1',%s)",
            (aid, actor, module, action, ttype, tid, json.dumps(detail, ensure_ascii=False), created),
        )
