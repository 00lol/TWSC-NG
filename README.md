# TWSC-NG
twsc的继承

TWSC-NG 是 TWSC 的轻量化重写：保留 TWSC 的类风格（`__init__` 组装 + `main()` 返回可用对象、loguru 日志、中文注释、
`APIRouter` 路由类、插件体系），把同步实现整体换成 **全异步**（asyncmy + 自研异步 Redis 客户端），
配置从 `ConfigFiles/*.yaml` 换成 `config/<组>/.env.*` 环境文件。

当前阶段目标：**后端骨架**（Redis、数据库插件、web、中间件、饼干认证、部分路由、邮箱），
**不写后台管理界面**，但后台认证（权限等级）必须可用。

---

## 一、目录结构

```
TWSC-NG/
├── main.py                     # 启动入口：同步组装 + 异步 startup/shutdown
├── 启动流程                     # 启动顺序说明
├── config/                     # 配置组（A/B/...），每组四个 .env.* 文件
│   └── A/{.env.server,.env.db,.env.redis,.env.email}
├── basic/                      # 基础设施：日志、系统检测、配置加载、文件检查、web、邮箱
│   ├── __init__.py
│   ├── init.py                 # LoggerInit / SystemTest / Go
│   ├── files.py                # Test / Create / Read（配置读取与补全）
│   ├── test.py                 # 独立自检：数据库连通性
│   ├── web.py                  # FastAPIWebApp：app、lifespan、CORS、静态/模板、启动
│   └── mail.py                 # AutoEmail：SMTP 发信
├── value/                      # 全局变量
│   ├── Vmain.py                # 全局对象（logger/config/database/cache/security/plugin...）
│   ├── Vconfig.py              # 环境变量 -> 结构化配置字典
│   └── 规则                      # 变量新增规则
├── cache/                      # 缓存（Redis 优先，内存降级）
│   ├── redis.py                # 自研异步 RESP2 客户端（无第三方依赖）
│   ├── memory.py               # 进程内降级缓存（同接口）
│   ├── session.py              # 会话存储（登录饼干背后的服务端会话）
│   └── mains.py                # Cache：统一缓存服务（value.cache）
├── databases/                  # 数据库（插件形态，MySQL）
│   ├── base.py                 # Database 抽象基类
│   ├── mysql.py                # MySQL（asyncmy 连接池）
│   ├── user.py                 # 默认用户表 users + 用户操作
│   ├── plugin.py               # 插件基类 / 插件管理器
│   └── mains.py                # DatabaseManager：配置->引擎->连接->建表->插件
├── plugin/                     # 业务插件目录（运行时扫描）
│   └── database_business/      # 数据库业务插件
│       └── example.py          # 插件模板（可复制修改）
├── transaction/                # 请求事务层：中间件 / 认证 / 路由
│   ├── middleware.py           # 请求上下文、访问日志、安全头、会话装载、CSRF、异常兜底
│   ├── auth.py                 # 密码哈希、饼干签名、会话、权限依赖
│   ├── routing.py              # 路由聚合入口 Routing
│   └── routes/
│       ├── index.py            # /、/health
│       ├── auth.py             # /auth/*：登录、登出、注册、当前用户、改密、邮箱验证码
│       └── admin.py            # /system/*：框架自带的 Cookie 后台认证（只有 API，没有界面）
└── log/                        # 日志输出
```

## 二、启动流程

```
python main.py
 │
 ├─ basic.Go().main()            日志 + 系统兼容检测 + 文件/配置检查（同步）
 ├─ value.Config(logger).main()  读取 config/<组>/.env.* -> Value.config（同步）
 ├─ 邮箱初始化（SERVER_EMAIL=True 时）                      （同步）
 ├─ 创建 Value.cache / Value.database / Value.security      （同步，只组装不连网）
 ├─ basic.FastAPIWebApp(Value)  -> Value.app                （同步，注册中间件、CORS）
 ├─ transaction.Routing(Value)  -> 挂载路由                  （同步）
 │
 └─ uvicorn 启动 -> lifespan:
      ├─ cache.init()        Redis 连接（失败自动降级内存缓存）
      ├─ database.init()     连接池 -> 建表/补字段 -> 加载 plugin/database_business 插件
      ├─ security.init()     密钥、饼干签名器、会话存储
      ├─ 初始化超级管理员（无超级管理员时：交互式创建 / 非交互随机生成）
      └─ yield               开始对外服务
      └─ shutdown: database.close() -> cache.close()
```

**关键设计**：数据库/缓存连接必须建在 uvicorn 的事件循环里（asyncmy 连接池绑定循环），
所以 `main.py` 只做同步组装，真正连网放在 `lifespan` 的 startup 阶段。

## 三、配置

配置按**组**管理：`config/A`、`config/B`……通过环境变量 `TWSC_CONFIG` 选择（默认第一个目录，即 `A`）。
每组四个文件，缺失时自动写入默认值（`basic/files.py`）：

| 文件 | 变量 |
| --- | --- |
| `.env.server` | `SERVER_IP` `SERVER_PORT` `SERVER_DEBUG` `SERVER_EMAIL` `SERVER_CONFIG` `SERVER_SECRET` `SERVER_LOGIN_VALIDITY` `SERVER_REGISTER` `SERVER_ADMIN_USER` `SERVER_ADMIN_PASSWORD` `SERVER_CORS` `SERVER_TRUSTED_PROXIES` `SERVER_COOKIE_SECURE` `SERVER_CONSOLE_TOKEN_HOURS` `SERVER_CONSOLE_SEED` `SERVER_CONSOLE_ADMIN_USER` `SERVER_CONSOLE_ADMIN_PASSWORD` `SERVER_SSL` `SERVER_SSL_CERT` `SERVER_SSL_KEY` |
| `.env.db` | `DB_ENGINE` `DB_HOST` `DB_PORT` `DB_USER` `DB_PASSWORD` `DB_DB` `DB_CHARSET` `DB_MAXSIZE` `DB_MININSIZE` |
| `.env.redis` | `REDIS_ENABLED` `REDIS_HOST` `REDIS_PORT` `REDIS_PASSWORD` `REDIS_DB` `REDIS_MAXSIZE` `REDIS_TIMEOUT` |
| `.env.email` | `EMAIL_USER` `EMAIL_PASSWORD` |

**优先级：系统环境变量 > 配置文件**（`load_dotenv(override=False)`），便于容器化部署覆盖。

- 优先使用系统环境变量；值为空时按上面的默认值处理。
- `cookie_secure` 默认跟随 `ssl`：HTTPS 部署时登录饼干自动带 `Secure`；反向代理终止 TLS
  （应用自身还是 HTTP）时，在 `.env.server` 显式设置 `SERVER_COOKIE_SECURE=True`；
  纯 HTTP 调试时保持默认 `False`，否则浏览器不会回传登录饼干。

`value.Config` 解析后的结构（**所有模块只读这份字典，不再直接 `os.getenv`**）：

```python
{
  "server": {"ip","port","debug","email","config","secret","login_validity","register",
             "admin_user","admin_password","cors","trusted_proxies","base_url",
             "ssl","cookie_secure","ssl_cert","ssl_key"},
  "db":     {"engine","host","port","user","password","database","charset","maxsize","minsize"},
  "redis":  {"enabled","host","port","password","db","maxsize","timeout"},
  "email":  {"user","password"},
}
```

## 四、模块契约

> 改动接口时先改本节。

### 4.1 value（全局对象）

```python
class Main:                       # value/Vmain.py
    logger                        # loguru.Logger
    email                         # send(receiver, html_content, html_title) -> (bool, str)
    config                        # 见"三、配置"
    database                      # DatabaseManager | None
    cache                         # Cache | None
    security                      # transaction.auth.Security | None
    app                           # FastAPI
    templates                     # Jinja2Templates | None
    socketio / sio_app            # python-socketio
    start_time / system           # 启动时间 / 系统信息
    plugin                        # .database_business: dict[str, DatabasePlugin]
    startup / shutdown            # async callable，由 main.py 注入，web.py 的 lifespan 调用
    reload_config                 # 同步 callable
```

### 4.2 cache（缓存）

```python
# cache/mains.py
class Cache:
    def __init__(self, logger, config: dict)
    async def init(self) -> bool          # 永不抛异常；Redis 失败降级内存并记 warning
    async def close(self) -> None
    async def ping(self) -> tuple[bool, str]
    async def get(key) -> str | None
    async def set(key, value, ex: int | None = None) -> bool
    async def delete(*keys) -> int
    async def exists(key) -> int
    async def expire(key, seconds) -> bool
    async def ttl(key) -> int
    async def incr(key) -> int
    async def get_json(key) -> dict | None
    async def set_json(key, value, ex=None) -> bool
    client                                 # RedisClient | MemoryCache | None
    mode                                   # "redis" | "memory" | "none"
    enabled: bool
    session: SessionStore                  # 会话存储

# cache/session.py
class SessionStore:
    def __init__(self, cache, prefix="twsc:session:", ttl=86400)
    async def create(self, data: dict, ttl: int | None = None) -> str   # 返回 sid
    async def load(self, sid: str) -> dict | None
    async def refresh(self, sid: str, ttl: int | None = None) -> bool
    async def destroy(self, sid: str) -> bool
```

### 4.3 databases（数据库）

```python
# databases/base.py
class Database:
    engine: str
    def __init__(self, logger, config: dict)
    async def connect(self) -> bool
    async def close(self) -> None
    def close_pool(self) -> None                 # 进程退出兜底（同步）
    async def ping(self) -> tuple[bool, str]
    async def execute(sql, params=None) -> int
    async def execute_many(sql, params_list) -> int
    async def fetch_one(sql, params=None) -> dict | None
    async def fetch_all(sql, params=None) -> list[dict]
    async def table_exists(table) -> bool
    async def table_columns(table) -> list[str]
    async def ensure_tables(table_defs: dict) -> None   # 建表 / 补字段，不删不改
    connected: bool

# databases/mains.py
class DatabaseManager:
    def __init__(self, value)                    # 读 value.config["db"]，engine 注册表选驱动
    driver: Database
    user: User
    plugins: dict[str, DatabasePlugin]
    async def init(self) -> bool                 # 连接 -> 建表 -> 加载插件；失败不抛，返回 False
    async def close(self) -> None
    def close_pool(self) -> None
    async def ping/execute/fetch_one/fetch_all(...)   # 便捷转发

# databases/user.py  默认用户表
class User:                                      # LEVEL_SUPER=-128 / LEVEL_ADMIN=-100 / LEVEL_USER=0
    async def verify_user(username) -> dict | None
    async def get_user_by_id / get_user_by_username / get_user_by_email
    async def get_users(belong=None) -> list[dict]      # 不含 password_hash
    async def register_user(username, password_hash, email=None, nickname=None) -> (bool, str)
    async def add_user(username, password_hash, level=0, belong=0, ...) -> (bool, str)
    async def update_login(user_id, ip) / update_password / update_level / update_active / delete_user
    async def transfer() -> dict | None          # 是否存在超级管理员
    async def count() -> int

# databases/plugin.py
class Plugin:                                    # 通用插件基类
    def initialize(self, value) -> bool          # 同步基类
    def shutdown(self) -> bool
    def get_info(self) -> dict
class DatabasePlugin(Plugin):
    async def initialize(self, value) -> bool    # 拿到 self.logger / self.database / self.user
    async def execute/fetch_one/fetch_all(...)   # 转发 self.database
    async def ensure_tables(table_defs)
    def register_routes(self, router: APIRouter) # 可选，注册到 /plugin/<name>
class PluginManager:
    def register_plugin_dir(dir_path) -> bool
    def load_plugins_from_dir(dir_path) -> list[str]
    def get_plugin(name) / unregister_plugin(name) / shutdown_all()
    async def initialize_all(value) -> list[str]
def get_plugin_manager(logger=None, plugin_type="database_business") -> PluginManager
```

### 4.4 transaction（请求事务层）

```python
# transaction/auth.py
class Password:            # pbkdf2_sha256$<迭代>$<salt>$<hash>，标准库实现，不依赖 bcrypt
    @staticmethod def hash(password) -> str
    @staticmethod def verify(password, hashed) -> bool
    @staticmethod def needs_update(hashed) -> bool
class CookieSigner:        # HMAC-SHA256 饼干签名：base64url(payload).ts.sig
    def __init__(self, secret: str, salt="twsc.session")
    def sign(data: dict) -> str
    def verify(token: str, max_age: int = 0) -> dict | None
class Security:            # value.security
    COOKIE_NAME="twsc_session"  CSRF_COOKIE_NAME="twsc_csrf"  CSRF_HEADER="X-CSRF-Token"
    async def init(self) -> bool
    async def login(self, user: dict) -> dict | None      # {"cookie","csrf","sid"}
    async def logout(self, request) -> bool
    async def resolve(self, request) -> dict | None       # 饼干 -> 会话 -> 用户（中间件调用）
    def set_cookie(self, response, cookie, csrf) -> None
    def clear_cookie(self, response) -> None
async def current_user(request) -> dict                   # 依赖：未登录 401
def require_level(level: int) -> Callable                 # 依赖工厂：等级越负越高
```

```python
# transaction/middleware.py
class Middleware:
    def __init__(self, value)
    def main(self) -> None        # 注册到 value.app：请求上下文/会话装载/CSRF/安全头/访问日志/异常兜底
```

统一 JSON 约定：`{"success": true/false, "message": str, "data": any}`；
未登录 `401`、权限不足 `403`、依赖不可用（数据库/缓存）`503`。

### 4.5 路由

| 方法 | 路径 | 说明 | 认证 |
| --- | --- | --- | --- |
| GET | `/` | 服务信息 | 否 |
| GET | `/health` | 数据库/缓存状态 | 否 |
| POST | `/auth/login` | 登录（JSON：`username`/`password`），下发饼干 | 否 |
| POST | `/auth/logout` | 登出，清除饼干 | 是 |
| GET | `/auth/me` | 当前用户 | 是 |
| POST | `/auth/register` | 注册（`SERVER_REGISTER=True`） | 否 |
| POST | `/auth/password` | 修改密码 | 是 |
| POST | `/auth/email/code` | 发送邮箱验证码（需 `SERVER_EMAIL=True`） | 否 |
| GET | `/system/ping` | 后台认证探针 | ≥管理员 |
| GET | `/system/whoami` | 后台身份 | ≥管理员 |
| GET | `/system/users` | 用户列表 | 超级管理员 |
| POST | `/system/users` | 新增用户 | 超级管理员 |
| POST | `/system/users/{id}/active` | 启用/禁用 | 超级管理员 |
| POST | `/system/users/{id}/level` | 调整等级 | 超级管理员 |

> `/admin/*` 已整体交给管理台业务插件（见第九节），框架自带的 Cookie 后台接口统一在 `/system/*`。

所有路由前缀带 `value.config["server"]["base_url"]`（默认空）。

## 五、编写约定（与 TWSC 保持一致）

1. 文件头 `# -*- coding:utf-8 -*-` 或中文标题注释；注释、日志一律中文。
2. 类风格：`__init__` 只做**对象组装与参数校验**，连网/建表等 I/O 放在 `async init/connect`；
   需要"返回一个可用对象"的模块提供 `main()`。
3. 依赖注入：`logger`、`value`、`config` 从构造函数传入，模块内部不 `import logger`。
4. 包对外导出统一写在 `__init__.py` 的 `__all__`。
5. I/O 全部 `async`；同步入口（`main.py`、脚本）用 `asyncio.run`。
6. 路由类：`APIRouter(prefix=..., tags=[...])`，在 `__init__` 里调 `self._route()` 注册。
7. 数据库访问只走 `DatabaseManager` / `DatabasePlugin` 的封装方法，SQL 必须参数化（`%s`）。
8. 任何模块不得让整个服务因 Redis / 数据库不可用而无法启动；降级 + 明确日志。

## 六、运行

```bash
.venv\Scripts\python.exe main.py            # 默认 config 组 A
$env:TWSC_CONFIG="B"; .venv\Scripts\python.exe main.py   # 选 B 组
.venv\Scripts\python.exe -m basic.test      # 数据库自检
```

依赖：FastAPI + uvicorn + asyncmy + loguru + python-dotenv + psutil + python-socketio。

## 七、接口调用示例

登录（饼干 + CSRF，注意 `-c` 保存 cookie）：

```bash
curl -i -c cookies.txt -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"root","password":"你的密码"}'
# 响应: {"success":true,"data":{"user_id":"1","username":"root","level":-128,"csrf_token":"..."}}
# 同时下发 twsc_session（HttpOnly）与 twsc_csrf（可被前端读取）两个饼干
```

所有 `POST/PUT/PATCH/DELETE` 都必须把 `twsc_csrf` 的值放进 `X-CSRF-Token` 请求头：

```bash
CSRF=$(grep twsc_csrf cookies.txt | awk '{print $7}')
curl -b cookies.txt -X POST http://127.0.0.1:8000/auth/logout -H "X-CSRF-Token: $CSRF"
curl -b cookies.txt http://127.0.0.1:8000/auth/me
curl -b cookies.txt http://127.0.0.1:8000/system/users -H "X-CSRF-Token: $CSRF"
```

后台新增用户（仅超级管理员）：

```bash
curl -b cookies.txt -X POST http://127.0.0.1:8000/system/users \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"username":"operator","password":"Operator_123","level":-100}'
```

在线文档：`http://127.0.0.1:8000/docs`（FastAPI 自动生成，无需额外页面）。

## 八、已实现与验证

| 模块 | 文件 | 验证结果 |
| --- | --- | --- |
| 启动链路 | `main.py` / `basic/web.py` | MySQL 8.0.46 实机启动：lifespan 连接、自动建库建表、加载 1 个业务插件、`/health`=200、CTRL_BREAK 优雅关闭 |
| 认证（饼干） | `transaction/auth.py` | 登录/登出/`/auth/me`/CSRF 403/权限 403/改密后旧会话失效，35 项端到端断言全通过 |
| 中间件 | `transaction/middleware.py` | 请求 ID、访问日志、安全响应头、统一 JSON 错误（401/403/404/422/503）、CSRF 双提交 |
| 路由 | `transaction/routes/*` | `/`、`/health`、`/auth/*`（6 个 Cookie 认证）、`/system/*`（6 个框架后台）；`/admin/*` 归管理台业务插件 |
| 数据库 | `databases/*` | users 表 14 字段自动创建、超级管理员 `role_level=-128`、pbkdf2 哈希、登录 IP 落库；驱动/建表/补字段/用户操作 86 项断言 |
| 插件体系 | `databases/plugin.py` / `plugin/` | 目录扫描、`example_business` 加载并注入数据库、`register_routes` 预留 |
| 缓存 | `cache/*` | 本机无 Redis → 自动降级内存缓存；自研异步 RESP2 客户端用假 Redis 验证 GET/SET/数组/错误回复/断线重连/连接池并发，90 项断言 |
| 降级保护 | 全链路 | 无 Redis：服务正常（memory 模式）；无数据库：服务正常、`/health` 报 `database=down`、登录返回 503 |
| 调试模式 | `basic/web.py` | `SERVER_DEBUG=True` 时 uvicorn 热重载（`main:app`）可正常启动并响应 |

验证覆盖 35（HTTP 端到端）+ 12（边界场景）+ 90（缓存）+ 86（数据库）+ 142（事务层）项断言，全部通过。

## 九、管理台后端（`/admin/*`，61 条接口）

对接前端 Vue 3 管理台（14 个页面）。接口契约见
[`docs/后端实现约定.md`](docs/后端实现约定.md)（内部实现约定）与前端仓库的
`API-接口清单.md`（字段/错误码权威来源）。

### 9.1 与框架内 `/auth/*` 的区别

| 项 | `/auth/*`（框架自带） | `/admin/*`（管理台） |
| --- | --- | --- |
| 认证 | Cookie 会话 + CSRF 双提交 | `Authorization: Bearer <token>`（12h） |
| 角色 | `role_level`（-128/-100/0） | `super` / `op` / `developer`（developer 只读） |
| 响应包 | `{success,message,data}` | `{code,message,data,traceId}` |
| 账号表 | `users`（框架用户） | `console_admins`（管理台账号，独立） |

两套互不影响：中间件按路径前缀分流错误外壳，管理台不走 CSRF。

### 9.2 业务插件

| 插件 | 负责接口 | 表 |
| --- | --- | --- |
| `console_auth` | 登录/注册/登出/me、白名单、账号、操作日志（15） | console_admins / console_whitelist / console_audit_logs |
| `console_users` | 用户列表/详情/审核/封禁、认证队列（5） | platform_users / pilot_profiles / client_profiles / verify_records / demands |
| `console_orders` | 订单列表/详情 + 7 个状态机操作（9） | orders / order_timeline / order_reports / order_payments / order_deliverables … |
| `console_content` | 作品/评论/举报/频道（9） | works / comments / content_reports / console_channels |
| `console_activities` | 活动/系统消息（12） | activities / notices |
| `console_report` | 数据报备/CSV/指标快照（4+1） | console_report_snapshots |
| `console_ops` | 看板/参数/对接配置（10） | console_params / console_param_history / console_integrations |

插件通过 `DatabasePlugin.prefix = "/admin"` 挂载；`main.py` 在 lifespan 里数据库就绪后统一挂载。

### 9.3 演示数据与联调

```powershell
# 首次启动自动建表 + 灌演示数据（3 个管理员、35 用户、6 订单、7 作品、活动/消息/参数/对接/日志）
$env:TWSC_CONFIG="B"; $env:DB_DB="twsc_ng_test"; $env:SERVER_DEBUG="False"; $env:SERVER_CONSOLE_SEED="True"
.\.venv\Scripts\python.exe main.py

# 管理台账号
#   方式一（推荐，有全量演示数据）：SERVER_CONSOLE_SEED=True，账号见 databases/seed.py
#     admin_super / Admin@12345   （super）
#     op_demo     / Op@12345      （op）
#     dev_demo    / Dev@12345     （developer，只读）
#   方式二：不开 seed 也能登录——首次启动若 console_admins 为空，会自动创建超级管理员：
#     调试模式（SERVER_DEBUG=True）密码固定为 Admin@12345；
#     生产模式随机生成并打印在启动日志里（可用 SERVER_CONSOLE_ADMIN_USER/PASSWORD 指定）

# 前端：VITE_API_BASE= 留空（同源代理），VITE_PROXY_TARGET=http://127.0.0.1:8000
# 冒烟测试（61 条接口逐条打）：
.\.venv\Scripts\python.exe tests\console_smoke.py http://127.0.0.1:8000
```

- `/docs` 可直接看到全部 `/admin/*` 接口的 Swagger；
- `/admin/reports` 做了**双实现**：带 `period/from/to` 走数据报备，否则返回举报列表
  （前端现状零改动即可；规范路径同时提供 `/admin/report-data`）；
- CSV 导出带 UTF-8 BOM 与 `Content-Disposition`，前端原生 fetch + Bearer 下载即可。
