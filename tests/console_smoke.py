# -*- coding:utf-8 -*-
"""管理台接口冒烟测试（对着 API-接口清单.md 的 61 条逐条打）

前置：
    后端已用演示数据启动（SERVER_CONSOLE_SEED=True，见 docs/后端实现约定.md §9）
用法：
    .venv\\Scripts\\python.exe tests\\console_smoke.py [base_url]
默认 http://127.0.0.1:8000

输出：逐条 PASS/FAIL 与汇总；失败时打印响应体，便于定位是契约还是实现问题。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
ADMIN, PWD = "admin_super", "Admin@12345"
DEV, DEV_PWD = "dev_demo", "Dev@12345"

results = []
token = None


def call(method, path, body=None, auth=True, raw=False, token_value=None):
    """发请求，返回 (status, payload, headers)"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    use_token = token_value or (token if auth else None)
    if use_token:
        req.add_header("Authorization", f"Bearer {use_token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read()
            headers = dict(resp.headers)
            if raw:
                return resp.status, payload, headers
            return resp.status, json.loads(payload.decode("utf-8") or "{}"), headers
    except urllib.error.HTTPError as e:
        payload = e.read()
        headers = dict(e.headers)
        if raw:
            return e.code, payload, headers
        try:
            return e.code, json.loads(payload.decode("utf-8") or "{}"), headers
        except Exception:
            return e.code, {"raw": payload[:300].decode("utf-8", "replace")}, headers


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""))


def expect(name, method, path, body=None, status=200, code=0, auth=True, token_value=None):
    st, payload, _ = call(method, path, body, auth=auth, token_value=token_value)
    ok = st == status and (payload.get("code") if isinstance(payload, dict) else None) == code
    check(name, ok, f"HTTP {st} code={payload.get('code') if isinstance(payload, dict) else '-'} "
                    f"msg={payload.get('message') if isinstance(payload, dict) else ''.__str__()}")
    return payload.get("data") if isinstance(payload, dict) else None


def has(obj, *keys):
    return isinstance(obj, dict) and all(k in obj for k in keys)


# --------------------------------------------------------------------------- #
# 0. 登录（失败场景 + 成功）
# --------------------------------------------------------------------------- #
expect("01 POST /admin/login 错误密码 -> 401/2001", "POST", "/admin/login",
       {"username": ADMIN, "password": "Wrongpass1"}, status=401, code=2001, auth=False)
data = expect("01 POST /admin/login 成功", "POST", "/admin/login",
              {"username": ADMIN, "password": PWD}, auth=False)
token = (data or {}).get("token")
check("01b 登录返回 token + admin", isinstance(token, str) and has(data or {}, "token", "admin"))

expect("01c 未带令牌 -> 401/2003", "GET", "/admin/me", status=401, code=2003, auth=False)
expect("01d 伪造令牌 -> 401/2003", "GET", "/admin/me", status=401, code=2003,
       auth=False, token_value="bad.token.value")

me = expect("04 GET /admin/me", "GET", "/admin/me")
check("04b /admin/me 含 role=super", (me or {}).get("role") == "super")

# --------------------------------------------------------------------------- #
# 1. 看板（4.1-4.6）
# --------------------------------------------------------------------------- #
ov = expect("05 GET /admin/stats/overview", "GET", "/admin/stats/overview")
check("05b overview 金额/计数为整数", isinstance((ov or {}).get("gmv"), int) and isinstance((ov or {}).get("orderCount"), int))

trend = expect("06 GET /admin/stats/trend", "GET", "/admin/stats/trend?days=7")
check("06b trend 返回 8 个点", len((trend or {}).get("list") or []) == 8, str(len((trend or {}).get("list") or [])))

cs = expect("07 GET /admin/content/summary", "GET", "/admin/content/summary")
check("07b content summary 四组齐", has(cs or {}, "works", "demands", "reports", "articles"))

vq = expect("08 GET /admin/verify-queue", "GET", "/admin/verify-queue")
check("08b verify-queue 剔除 id_card_enc",
      "id_card_enc" not in json.dumps(vq or {}, ensure_ascii=False) and isinstance((vq or {}).get("list"), list))

params = expect("09 GET /admin/params", "GET", "/admin/params")
keys = [i.get("key") for i in (params or {}).get("list") or []]
check("09b params 7 个固定 key", sorted(keys) == sorted([
    "commission_rate", "settle_days", "auto_accept_days", "pending_pay_minutes",
    "application_expire_days", "recommend_limit", "withdraw_min"]), str(keys))
expect("10 GET /admin/params/history", "GET", "/admin/params/history")

# 11 参数修改（super）
p = expect("11 PUT /admin/params", "PUT", "/admin/params",
           {"key": "commission_rate", "value": 0.12, "reason": "冒烟测试调整佣金"})
check("11b 修改后佣金比例=0.12",
      any(i.get("key") == "commission_rate" and float(i.get("value")) == 0.12
          for i in (expect("11c GET /admin/params", "GET", "/admin/params") or {}).get("list") or []))

# --------------------------------------------------------------------------- #
# 2. 用户（5.1-5.4）+ 认证队列
# --------------------------------------------------------------------------- #
users = expect("12 GET /admin/users", "GET", "/admin/users?page=1&pageSize=10&tab=all")
check("12b 用户列表分页+stats",
      has(users or {}, "list", "total", "page", "pageSize", "hasMore", "stats") and (users or {}).get("total") >= 35)
check("12c 列表手机号脱敏", "****" in json.dumps((users or {}).get("list") or [], ensure_ascii=False))

detail = expect("13 GET /admin/users/7", "GET", "/admin/users/7")
check("13b 用户详情含 profile/verifies", has(detail or {}, "verifies", "profile"))
check("13c 详情不含 id_card_enc", "id_card_enc" not in json.dumps(detail or {}, ensure_ascii=False))

expect("14 POST /admin/users/3/audit 通过", "POST", "/admin/users/3/audit",
       {"verifyType": "identity", "approved": True})
expect("14b 重复审核 -> 409/3002", "POST", "/admin/users/3/audit",
       {"verifyType": "identity", "approved": True}, status=409, code=3002)

# 15 封禁：飞手有进行中订单 -> 409 带 affectedOrders；force 后成功
resp = call("POST", "/admin/users/1/ban", {"banned": True})[1]
check("15 POST /admin/users/1/ban 冲突 -> 409/3002",
      resp.get("code") == 3002 and "affectedOrders" in json.dumps(resp, ensure_ascii=False), str(resp)[:160])
expect("15b 强制封禁 force=true", "POST", "/admin/users/1/ban", {"banned": True, "force": True})
expect("15c 解封", "POST", "/admin/users/1/ban", {"banned": False})

# --------------------------------------------------------------------------- #
# 3. 订单（6.1-6.9）
# --------------------------------------------------------------------------- #
orders = expect("16 GET /admin/orders", "GET", "/admin/orders?page=1&pageSize=10")
check("16b counts 补齐 10 状态 + all",
      has((orders or {}).get("counts") or {}, "pending_pay", "material_review", "in_progress", "delivered",
          "accepted", "settled", "cancelled", "refunding", "refunded", "disputed", "all"))

od = expect("17 GET /admin/orders/2", "GET", "/admin/orders/2")
check("17b 订单详情整包", has(od or {}, "id", "orderNo", "flow", "timeline", "deliverables", "payments", "messages"))

expect("18 POST approve-material/2", "POST", "/admin/orders/2/approve-material", {"remark": "材料齐全"})
expect("18b 重复 approve -> 409/3002", "POST", "/admin/orders/2/approve-material", {}, status=409, code=3002)
expect("19 POST cancel/1", "POST", "/admin/orders/1/cancel", {"reason": "客户取消"})
expect("19b cancel 非待付款 -> 409/3002", "POST", "/admin/orders/1/cancel", {}, status=409, code=3002)
expect("20 POST force-cancel/3", "POST", "/admin/orders/3/force-cancel", {"reason": "线下协商取消订单"})
expect("20b force-cancel reason 太短 -> 400/1001", "POST", "/admin/orders/3/force-cancel",
       {"reason": "短"}, status=400, code=1001)
expect("21 POST force-accept/4", "POST", "/admin/orders/4/force-accept", {"reason": "客户失联，代为验收"})
expect("22 POST refund/4", "POST", "/admin/orders/4/refund", {"reason": "验收后申请退款"})
expect("23 POST settle/5", "POST", "/admin/orders/5/settle")
expect("23b 重复 settle -> 409/3002", "POST", "/admin/orders/5/settle", status=409, code=3002)
expect("24 POST dispute-ruling/6", "POST", "/admin/orders/6/dispute-ruling",
       {"result": "client", "remark": "交付物不符合要求，全额退款"})

# --------------------------------------------------------------------------- #
# 4. 内容（7.1-7.9）
# --------------------------------------------------------------------------- #
works = expect("25 GET /admin/works", "GET", "/admin/works?status=pending")
check("25b works 带 counts", has((works or {}).get("counts") or {}, "all", "pending", "approved", "rejected", "offline"))
expect("26 POST works/1/audit", "POST", "/admin/works/1/audit", {"approved": True})
expect("26b 重复审核 -> 409/3002", "POST", "/admin/works/1/audit", {"approved": True}, status=409, code=3002)
expect("27 POST works/3/offline", "POST", "/admin/works/3/offline", {"reason": "违规内容下架"})
comments = expect("28 GET /admin/comments", "GET", "/admin/comments?page=1&pageSize=10")
check("28b comments 分页", has(comments or {}, "list", "total", "page", "pageSize", "hasMore"))
expect("29 POST comments/1/delete", "POST", "/admin/comments/1/delete")

reports = expect("30 GET /admin/reports（举报）", "GET", "/admin/reports?status=pending")
check("30b 举报列表结构", isinstance((reports or {}).get("list"), list) and "period" not in (reports or {}))
report_data = expect("30c GET /admin/reports?period=day（双实现转报备）", "GET", "/admin/reports?period=day")
check("30d 报备返回 period/list", (report_data or {}).get("period") == "day" and isinstance((report_data or {}).get("list"), list))
expect("31 POST reports/1/handle", "POST", "/admin/reports/1/handle", {"action": "handled", "note": "已警告作者"})

channels = expect("32 GET /admin/channels", "GET", "/admin/channels")
check("32b channels 三键 boolean", all(isinstance((channels or {}).get(k), bool) for k in ("rent", "repair", "training")))
expect("33 PUT /admin/channels", "PUT", "/admin/channels", {"rent": False, "repair": True})

# --------------------------------------------------------------------------- #
# 5. 活动 / 消息（8.1-8.12）
# --------------------------------------------------------------------------- #
acts = expect("34 GET /admin/activities", "GET", "/admin/activities?page=1&pageSize=10")
check("34b 活动列表不含 bodyHtml", "bodyHtml" not in json.dumps((acts or {}).get("list") or [], ensure_ascii=False))
act = expect("35 GET /admin/activities/2", "GET", "/admin/activities/2")
check("35b 活动详情含 bodyHtml", "bodyHtml" in (act or {}))
created_act = expect("36 POST /admin/activities", "POST", "/admin/activities", {
    "title": "冒烟测试活动", "startAt": "2026-11-01T10:00:00+08:00",
    "endAt": "2026-11-10T10:00:00+08:00", "bodyHtml": "<p>测试正文</p>"})
act_id = (created_act or {}).get("id")
expect("37 PUT /admin/activities/:id", "PUT", f"/admin/activities/{act_id}", {"title": "冒烟测试活动（改）"})
expect("38 POST publish", "POST", f"/admin/activities/{act_id}/publish")
expect("38b published 后编辑 -> 409/3002", "PUT", f"/admin/activities/{act_id}", {"title": "x"}, status=409, code=3002)
expect("39 POST offline", "POST", f"/admin/activities/{act_id}/offline")

notices = expect("40 GET /admin/notices", "GET", "/admin/notices?page=1&pageSize=10")
check("40b notices 分页", has(notices or {}, "list", "total", "page", "pageSize", "hasMore"))
expect("41 GET /admin/notices/2", "GET", "/admin/notices/2")
created_notice = expect("42 POST /admin/notices", "POST", "/admin/notices",
                        {"title": "冒烟测试消息", "body": "测试正文内容"})
notice_id = (created_notice or {}).get("id")
expect("43 PUT /admin/notices/:id", "PUT", f"/admin/notices/{notice_id}", {"body": "改后的正文"})
expect("44 POST publish", "POST", f"/admin/notices/{notice_id}/publish")
expect("45 POST withdraw", "POST", f"/admin/notices/{notice_id}/withdraw")

# --------------------------------------------------------------------------- #
# 6. 数据报备（9.1-9.4）
# --------------------------------------------------------------------------- #
rd = expect("46 GET /admin/report-data", "GET", "/admin/report-data?period=day")
check("46b 报备字段齐", has(rd or {}, "period", "from", "to", "list"))
expect("46c GET /admin/report-data?period=week", "GET", "/admin/report-data?period=week")
status, body, headers = call("GET", "/admin/report-data/export.csv", raw=True)
csv_ok = status == 200 and body[:3] == b"\xef\xbb\xbf" and "text/csv" in headers.get("content-type", "")
check("47 GET export.csv（BOM+CSV）", csv_ok, f"status={status} ctype={headers.get('content-type')}")
status, body, headers = call("GET", "/admin/reports/export.csv", raw=True)
check("47b 兼容 /admin/reports/export.csv", status == 200 and body[:3] == b"\xef\xbb\xbf", f"status={status}")
expect("48 GET /admin/report-snapshots", "GET", "/admin/report-snapshots?page=1&pageSize=10")
expect("49 GET /admin/report-snapshots/1", "GET", "/admin/report-snapshots/1")

# --------------------------------------------------------------------------- #
# 7. 对接配置（10.1-10.4）
# --------------------------------------------------------------------------- #
ints = expect("50 GET /admin/integrations", "GET", "/admin/integrations")
check("50b 固定 4 服务", len((ints or {}).get("list") or []) == 4)
expect("51 PUT integrations/wxpay/config", "PUT", "/admin/integrations/wxpay/config",
       {"config": {"mchid": "1234567890", "apiv3Key": "k" * 32, "serialNo": "A1B2C3D4E5F60718"}})
ints2 = expect("50c GET integrations（配置后）", "GET", "/admin/integrations")
body = json.dumps(ints2 or {}, ensure_ascii=False)
wxpay = next((i for i in (ints2 or {}).get("list") or [] if i.get("service") == "wxpay"), {})
mask_values = [str(v) for v in (wxpay.get("configMask") or {}).values() if v is not None]
check("50d 密钥不回显（只回 已配置（****））",
      bool(mask_values) and all("****" in m for m in mask_values)
      and "k" * 32 not in body and "A1B2C3D4E5F60718" not in body,
      body[:200])
expect("52 PUT integrations/wxpay/enabled", "PUT", "/admin/integrations/wxpay/enabled", {"enabled": True})
expect("53 POST integrations/wxpay/detect", "POST", "/admin/integrations/wxpay/detect")
expect("53b 不存在服务 -> 404/3001", "POST", "/admin/integrations/nope/detect", status=404, code=3001)

# --------------------------------------------------------------------------- #
# 8. 账号管理（11.1-11.6，仅 super）
# --------------------------------------------------------------------------- #
wl = expect("54 GET /admin/accounts/whitelist", "GET", "/admin/accounts/whitelist?status=all")
check("54b 白名单列表", isinstance((wl or {}).get("list"), list))
added = expect("55 POST whitelist", "POST", "/admin/accounts/whitelist",
               {"phone": "13900000011", "role": "developer"})
wl_id = (added or {}).get("id")
expect("55b 重复登记 -> 409/3003", "POST", "/admin/accounts/whitelist",
       {"phone": "13900000011", "role": "developer"}, status=409, code=3003)
expect("56 POST whitelist/:id/remove", "POST", f"/admin/accounts/whitelist/{wl_id}/remove")

accounts = expect("57 GET /admin/accounts", "GET", "/admin/accounts")
check("57b 账号列表", isinstance((accounts or {}).get("list"), list))
# 先拿 op_demo 的令牌，再停用，验证令牌立即失效（停用后无法再登录）
op_login = call("POST", "/admin/login", {"username": "op_demo", "password": "Op@12345"}, auth=False)[1]
op_token = (op_login.get("data") or {}).get("token")
check("58 停用前 op_demo 可登录", bool(op_token))
expect("58-1 POST accounts/2/status 停用", "POST", "/admin/accounts/2/status", {"disabled": True})
expect("58-2 停用后旧令牌失效", "GET", "/admin/me", status=401, code=2003, token_value=op_token)
status, payload, _ = call("POST", "/admin/login", {"username": "op_demo", "password": "Op@12345"})
check("58-3 停用后禁止登录 -> 403/2002", status == 403 and payload.get("code") == 2002,
      f"HTTP {status} code={payload.get('code')}")
expect("58-4 恢复 op_demo", "POST", "/admin/accounts/2/status", {"disabled": False})
expect("58-5 恢复后可登录", "POST", "/admin/login", {"username": "op_demo", "password": "Op@12345"}, auth=False)
role = expect("59 POST accounts/3/role", "POST", "/admin/accounts/3/role", {"role": "developer"})
check("59b 改角色返回 role_label", has(role or {}, "id", "role", "role_label"))

# --------------------------------------------------------------------------- #
# 9. 操作日志（12.1-12.2）
# --------------------------------------------------------------------------- #
logs = expect("60 GET /admin/audit-logs", "GET", "/admin/audit-logs?page=1&pageSize=50")
check("60b 日志分页", has(logs or {}, "list", "total", "page", "pageSize", "hasMore"))
check("60c 写操作已落日志", (logs or {}).get("total", 0) >= 5, str((logs or {}).get("total")))
modules = expect("61 GET /admin/audit-logs/modules", "GET", "/admin/audit-logs/modules")
check("61b 模块字典", isinstance((modules or {}).get("list"), list))

# --------------------------------------------------------------------------- #
# 10. 注册（3.2）、开发者只读（§1.7）、登出（3.3）
# --------------------------------------------------------------------------- #
expect("03 POST /admin/logout", "POST", "/admin/logout")
reg = expect("02 POST /admin/register", "POST", "/admin/register",
             {"phone": "13900000000", "password": "Dev123456", "name": "白名单开发者"}, auth=False)
dev_token = (call("POST", "/admin/login", {"username": "13900000000", "password": "Dev123456"},
                  auth=False)[1].get("data") or {}).get("token")
check("02b 白名单注册后可登录", bool(dev_token))
expect("02c developer 写接口 -> 403/2002", "PUT", "/admin/params",
       {"key": "settle_days", "value": 2}, status=403, code=2002, token_value=dev_token)
expect("02d developer 读接口可访问", "GET", "/admin/users", token_value=dev_token)

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
failed = [r for r in results if not r[0]]
print("=" * 92)
print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
for ok, name, detail in failed:
    print("FAIL:", name, detail)
sys.exit(1 if failed else 0)
