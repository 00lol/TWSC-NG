# -*- coding:utf-8 -*-
"""请求事务层 —— 认证与会话

本模块全部基于标准库实现（项目未安装 bcrypt / itsdangerous）：
- 密码哈希：hashlib.pbkdf2_hmac（PBKDF2-HMAC-SHA256，60 万次迭代），格式
  ``pbkdf2_sha256$<迭代数>$<salt_b64>$<hash_b64>``；
- 饼干签名：hmac + hashlib + base64 + json，格式 ``base64url(json).时间戳.HMAC-SHA256``；
- 会话存储：由 ``value.cache.session`` 注入（SessionStore 契约），缓存不可用时
  自动降级为进程内字典存储，保证登录功能可用（重启后登录态丢失）。

对外入口：Password / CookieSigner / Security / current_user / require_level / get_client_ip
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, status

# 权限等级（数值越小权限越高，与 databases/user.py 保持一致）
LEVEL_SUPER = -128   # 超级管理员
LEVEL_ADMIN = -100   # 管理员
LEVEL_USER = 0       # 普通用户

# 可信代理列表：模块级缓存，由 Security.init / Middleware.main 从配置装载
_TRUSTED_PROXIES: set = set()


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _to_bool(value: Any, default: bool = False) -> bool:
    """把配置里的各种写法统一成 bool"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _to_int(value: Any, default: int = 0) -> int:
    """把配置里的各种写法统一成 int（失败时返回默认值）"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_trusted_proxies(config: Any) -> set:
    """从配置中解析可信代理列表

    兼容三种写法（README 暂未把 trusted_proxies 写进 server 字段清单，先宽松读取）：
    1. config["server"]["trusted_proxies"]      —— 按 README 结构；
    2. config["server"]["security"]["trusted_proxies"] —— 分组写法；
    3. config["trusted_proxies"]                —— 扁平写法。
    """
    proxies: set = set()
    try:
        if not isinstance(config, dict):
            return proxies
        server = config.get("server") if isinstance(config.get("server"), dict) else None
        if server is None and isinstance(config.get("Sever"), dict):  # 兼容旧 TWSC 结构
            server = config.get("Sever")
        if isinstance(server, dict):
            candidates = []
            candidates.append(server.get("trusted_proxies"))
            security = server.get("security") or server.get("Security")
            if isinstance(security, dict):
                candidates.append(security.get("trusted_proxies"))
            for item in candidates:
                if isinstance(item, str):
                    proxies.update(x.strip() for x in item.split(",") if x.strip())
                elif isinstance(item, (list, tuple, set)):
                    proxies.update(str(x).strip() for x in item if str(x).strip())
        flat = config.get("trusted_proxies")
        if isinstance(flat, str):
            proxies.update(x.strip() for x in flat.split(",") if x.strip())
        elif isinstance(flat, (list, tuple, set)):
            proxies.update(str(x).strip() for x in flat if str(x).strip())
    except Exception:
        pass
    return proxies


def set_trusted_proxies(proxies: Any) -> None:
    """装载可信代理列表（中间件 / Security 初始化时调用）"""
    global _TRUSTED_PROXIES
    try:
        if isinstance(proxies, str):
            _TRUSTED_PROXIES = {x.strip() for x in proxies.split(",") if x.strip()}
        elif isinstance(proxies, (list, tuple, set)):
            _TRUSTED_PROXIES = {str(x).strip() for x in proxies if str(x).strip()}
        else:
            _TRUSTED_PROXIES = set()
    except Exception:
        _TRUSTED_PROXIES = set()


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
class Password:
    """密码哈希（PBKDF2-HMAC-SHA256，标准库实现，不依赖 bcrypt）"""

    ALGORITHM = "pbkdf2_sha256"
    ITERATIONS = 600000
    SALT_BYTES = 16
    HASH_BYTES = 32

    @staticmethod
    def _b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _unb64(text: str) -> bytes:
        return base64.b64decode(text.encode("ascii"))

    @staticmethod
    def hash(password: str) -> str:
        """生成密码哈希：pbkdf2_sha256$<迭代数>$<salt_b64>$<hash_b64>"""
        if not isinstance(password, str):
            password = str(password or "")
        salt = secrets.token_bytes(Password.SALT_BYTES)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            Password.ITERATIONS,
            dklen=Password.HASH_BYTES,
        )
        return (
            f"{Password.ALGORITHM}${Password.ITERATIONS}"
            f"${Password._b64(salt)}${Password._b64(digest)}"
        )

    @staticmethod
    def verify(password: str, hashed: str) -> bool:
        """校验密码；格式非法 / 算法不认识 一律返回 False（不抛异常）"""
        if not isinstance(password, str) or not password:
            return False
        if not isinstance(hashed, str) or not hashed:
            return False
        try:
            parts = hashed.split("$")
            if len(parts) != 4:
                return False
            algorithm, iterations_text, salt_b64, hash_b64 = parts
            if algorithm != Password.ALGORITHM:
                return False
            iterations = int(iterations_text)
            if iterations <= 0:
                return False
            salt = Password._unb64(salt_b64)
            expected = Password._unb64(hash_b64)
            if not salt or not expected:
                return False
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected)
            )
        except Exception:
            return False
        return hmac.compare_digest(digest, expected)

    @staticmethod
    def needs_update(hashed: str) -> bool:
        """哈希是否需要升级（算法不同 / 迭代次数变化 / 格式非法都返回 True）"""
        if not isinstance(hashed, str) or not hashed:
            return True
        try:
            parts = hashed.split("$")
            if len(parts) != 4:
                return True
            algorithm, iterations_text = parts[0], parts[1]
            if algorithm != Password.ALGORITHM:
                return True
            return int(iterations_text) != Password.ITERATIONS
        except Exception:
            return True


# --------------------------------------------------------------------------- #
# 饼干签名
# --------------------------------------------------------------------------- #
class CookieSigner:
    """HMAC-SHA256 饼干签名器

    令牌格式：``base64url(json).时间戳.HMAC-SHA256签名``
    - 签名密钥由 ``salt`` 与 ``secret`` 派生，避免同一 secret 跨用途复用；
    - verify 对签名不正确 / 过期 / 格式错误一律返回 None，不抛异常。
    """

    def __init__(self, secret: str, salt: str = "twsc.session"):
        self.secret = secret if isinstance(secret, str) else str(secret or "")
        self.salt = salt or "twsc.session"
        self._key = f"{self.salt}:{self.secret}".encode("utf-8")

    # ---- 编解码 ----
    @staticmethod
    def _b64encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(text: str) -> bytes:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode((text + padding).encode("ascii"))

    def _signature(self, payload: str, timestamp: str) -> str:
        message = f"{payload}.{timestamp}".encode("ascii")
        digest = hmac.new(self._key, message, hashlib.sha256).digest()
        return self._b64encode(digest)

    # ---- 签名 / 校验 ----
    def sign(self, data: dict) -> str:
        """对字典签名，生成可写入 Cookie 的令牌"""
        payload_dict = data if isinstance(data, dict) else {}
        payload = self._b64encode(
            json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        timestamp = str(int(time.time()))
        return f"{payload}.{timestamp}.{self._signature(payload, timestamp)}"

    def verify(self, token: str, max_age: int = 0) -> Optional[dict]:
        """校验令牌；成功返回原始字典，失败返回 None（不抛异常）"""
        try:
            if not isinstance(token, str) or not token:
                return None
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload, timestamp, signature = parts
            expected = self._signature(payload, timestamp)
            if not hmac.compare_digest(expected, signature):
                return None
            issued_at = int(timestamp)
            now = int(time.time())
            if max_age and max_age > 0 and now - issued_at > max_age:
                return None
            # 时间戳明显在未来视为异常（容忍 60 秒时钟漂移）
            if issued_at > now + 60:
                return None
            data = json.loads(self._b64decode(payload).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# 进程内会话存储（缓存不可用时的降级实现）
# --------------------------------------------------------------------------- #
class _MemorySessionStore:
    """进程内会话存储，接口与 cache/session.py 的 SessionStore 保持一致

    仅在 ``value.cache.session`` 不可用时启用：单进程有效，重启后登录态丢失。
    """

    def __init__(self, prefix: str = "twsc:session:", ttl: int = 86400):
        self.prefix = prefix or "twsc:session:"
        self.ttl = int(ttl) if ttl else 86400
        self._sessions: Dict[str, dict] = {}

    async def create(self, data: dict, ttl: Optional[int] = None) -> str:
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = {
            "data": dict(data or {}),
            "expire": time.time() + int(ttl or self.ttl),
        }
        self._purge()
        return sid

    async def load(self, sid: str) -> Optional[dict]:
        key = str(sid)
        item = self._sessions.get(key)
        if not item:
            return None
        if item["expire"] < time.time():
            self._sessions.pop(key, None)
            return None
        return dict(item["data"])

    async def refresh(self, sid: str, ttl: Optional[int] = None) -> bool:
        key = str(sid)
        item = self._sessions.get(key)
        if not item:
            return False
        if item["expire"] < time.time():
            self._sessions.pop(key, None)
            return False
        item["expire"] = time.time() + int(ttl or self.ttl)
        return True

    async def destroy(self, sid: str) -> bool:
        return self._sessions.pop(str(sid), None) is not None

    def _purge(self) -> None:
        """顺带清理过期会话，避免进程内字典无限增长"""
        if len(self._sessions) < 256:
            return
        now = time.time()
        for key in [k for k, v in self._sessions.items() if v.get("expire", 0) < now]:
            self._sessions.pop(key, None)


# --------------------------------------------------------------------------- #
# 用户字典规范化（跨包契约：level 还是 role_level 尚未统一，两种都认）
# --------------------------------------------------------------------------- #
def normalize_user(raw: Any) -> dict:
    """把数据库 / 会话里的用户记录规范成 {"user_id","username","level"}

    databases/user.py 的字段名可能为 user_id/username/level 或
    id/user/role_level，这里做一次宽松映射，避免跨包字段名不一致导致登录失败。
    """
    if not isinstance(raw, dict):
        return {}

    def pick(*keys: str) -> Any:
        for key in keys:
            if key in raw and raw[key] is not None:
                return raw[key]
        return None

    user_id = pick("user_id", "id", "uid")
    username = pick("username", "user", "name")
    level = pick("level", "role_level", "user_level")

    return {
        "user_id": None if user_id is None else str(user_id),
        "username": None if username is None else str(username),
        "level": _to_int(level, LEVEL_USER),
    }


# --------------------------------------------------------------------------- #
# 安全 / 会话服务（value.security）
# --------------------------------------------------------------------------- #
class Security:
    """饼干认证与会话服务

    - ``__init__`` 只做组装与参数读取（不连网）；
    - ``async init`` 在 uvicorn startup 阶段调用：创建签名器、接管会话存储；
    - 会话数据只存服务端（sid 进饼干），饼干只携带 sid，便于服务端主动销毁。
    """

    COOKIE_NAME = "twsc_session"
    CSRF_COOKIE_NAME = "twsc_csrf"
    CSRF_HEADER = "X-CSRF-Token"
    SESSION_PREFIX = "twsc:session:"

    def __init__(self, value):
        self.value = value
        self.logger = getattr(value, "logger", None)
        if self.logger is None:
            raise ValueError("Security 初始化失败：value.logger 未初始化")

        server = self._server_config()
        self.debug = _to_bool(server.get("debug", False), False)
        # 登录有效期（分钟），默认 60 分钟
        self.login_validity = _to_int(server.get("login_validity", 60), 60)
        if self.login_validity <= 0:
            self.login_validity = 60
        self.max_age = self.login_validity * 60

        # 饼干是否只走 HTTPS：默认跟随 server.ssl，可被 server.cookie_secure 覆盖
        # （plain HTTP 部署时若强制 Secure，浏览器根本不会回传登录饼干）
        self.cookie_secure = _to_bool(
            server.get("cookie_secure", server.get("ssl", False)), False
        )

        # 饼干签名密钥：配置为空时生成一次性随机密钥（重启后登录态失效）
        secret = str(server.get("secret") or "").strip()
        if not secret:
            secret = secrets.token_hex(32)
            self.logger.warning(
                "未配置 server.secret，已生成一次性随机密钥：重启后所有登录态失效，生产环境请配置 SERVER_SECRET"
            )
        else:
            self.logger.debug("安全模块：使用配置中的 server.secret 作为饼干签名密钥")
        self.secret = secret
        self.signer = CookieSigner(secret)

        # 会话存储：从 value.cache.session 取（此刻 cache 可能还没 init，只做引用）
        cache = getattr(value, "cache", None)
        try:
            self.session_store = getattr(cache, "session", None) if cache is not None else None
        except Exception as e:
            self.logger.warning(f"获取缓存会话存储失败，稍后 init 时降级: {e}")
            self.session_store = None
        self.persistent = self.session_store is not None
        self.ready = False

        # 可信代理（get_client_ip 使用）
        set_trusted_proxies(parse_trusted_proxies(getattr(value, "config", None)))

    # ---- 配置读取 ----
    def _server_config(self) -> dict:
        """读取 server 配置段（兼容旧 TWSC 的 Sever 结构）"""
        config = getattr(self.value, "config", None)
        if isinstance(config, dict):
            server = config.get("server")
            if isinstance(server, dict):
                return server
            legacy = config.get("Sever")
            if isinstance(legacy, dict):
                return legacy
        return {}

    # ---- 生命周期 ----
    async def init(self) -> bool:
        """初始化签名器与会话存储；返回是否使用持久化（缓存）会话存储"""
        try:
            self.signer = CookieSigner(self.secret)

            cache = getattr(self.value, "cache", None)
            try:
                store = getattr(cache, "session", None) if cache is not None else None
            except Exception as e:
                self.logger.warning(f"获取缓存会话存储失败，降级为进程内存储: {e}")
                store = None
            if store is None:
                # 缓存不可用：降级进程内会话存储，保证登录可用
                store = _MemorySessionStore(prefix=self.SESSION_PREFIX, ttl=self.max_age)
                self.persistent = False
                self.logger.warning(
                    "缓存会话存储不可用，已降级为进程内会话存储（仅单进程有效，重启后登录态丢失）"
                )
            else:
                self.persistent = True
                self.logger.info("安全模块初始化完成：会话使用缓存存储")

            self.session_store = store
            self.ready = True
            set_trusted_proxies(parse_trusted_proxies(getattr(self.value, "config", None)))
            self.logger.info(
                f"安全模块就绪：登录有效期 {self.login_validity} 分钟，"
                f"会话存储={'cache' if self.persistent else 'memory'}"
            )
            return self.persistent
        except Exception as e:  # 初始化失败也要保证服务能起来
            self.logger.error(f"安全模块初始化失败: {e}")
            if self.session_store is None:
                self.session_store = _MemorySessionStore(
                    prefix=self.SESSION_PREFIX, ttl=self.max_age
                )
            self.ready = True
            self.persistent = False
            return False

    # ---- 会话 ----
    async def login(self, user: dict) -> Optional[dict]:
        """创建登录会话，返回 {"cookie","csrf","sid"}；失败返回 None"""
        try:
            if self.session_store is None:
                self.logger.error("会话存储未就绪，无法创建登录会话")
                return None
            normalized = normalize_user(user)
            if not normalized.get("user_id"):
                self.logger.error("登录失败：用户记录缺少 user_id")
                return None

            sid = await self.session_store.create(
                {
                    "user_id": normalized["user_id"],
                    "username": normalized.get("username") or "",
                    "level": normalized.get("level", LEVEL_USER),
                    "login_at": int(time.time()),
                },
                ttl=self.max_age,
            )
            if not sid:
                self.logger.error("登录失败：会话创建返回空 sid")
                return None

            cookie = self.signer.sign({"sid": sid, "user_id": normalized["user_id"]})
            csrf = secrets.token_urlsafe(32)
            self.logger.debug(
                f"创建登录会话成功：用户 {normalized.get('username')}"
                f"({normalized['user_id']}) sid={str(sid)[:8]}..."
            )
            return {"cookie": cookie, "csrf": csrf, "sid": sid}
        except Exception as e:
            self.logger.error(f"创建登录会话失败: {e}")
            return None

    async def logout(self, request: Any) -> bool:
        """销毁请求携带的会话；任何异常返回 False（登出接口保持幂等）"""
        try:
            if request is None:
                return False
            token = request.cookies.get(self.COOKIE_NAME) if hasattr(request, "cookies") else None
            if not token:
                return False
            payload = self.signer.verify(token, max_age=self.max_age)
            if not payload:
                return False
            sid = payload.get("sid")
            if not sid or self.session_store is None:
                return False
            result = await self.session_store.destroy(str(sid))
            if result:
                self.logger.debug(f"会话已销毁：sid={str(sid)[:8]}...")
            return bool(result)
        except Exception as e:
            self.logger.warning(f"销毁会话失败: {e}")
            return False

    async def resolve(self, request: Any) -> Optional[dict]:
        """中间件调用：饼干 -> 会话 -> 用户；任何异常返回 None"""
        try:
            if request is None or self.session_store is None:
                return None
            token = request.cookies.get(self.COOKIE_NAME) if hasattr(request, "cookies") else None
            if not token:
                return None
            payload = self.signer.verify(token, max_age=self.max_age)
            if not payload:
                return None
            sid = payload.get("sid")
            if not sid:
                return None
            session = await self.session_store.load(str(sid))
            if not isinstance(session, dict) or not session:
                return None
            user = normalize_user(session)
            if not user.get("user_id"):
                return None
            # 滑动续期（尽力而为：失败不影响本次请求）
            try:
                await self.session_store.refresh(str(sid), ttl=self.max_age)
            except Exception:
                pass
            return user
        except Exception as e:
            self.logger.warning(f"解析登录态失败: {e}")
            return None

    # ---- 饼干 ----
    def set_cookie(self, response: Any, cookie: str, csrf: str) -> None:
        """写入会话饼干与 CSRF 饼干（CSRF 饼干必须允许前端读取）"""
        secure = self.cookie_secure
        response.set_cookie(
            key=self.COOKIE_NAME,
            value=cookie,
            max_age=self.max_age,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            key=self.CSRF_COOKIE_NAME,
            value=csrf,
            max_age=self.max_age,
            httponly=False,  # 前端需要读取它并放进 X-CSRF-Token 请求头
            secure=secure,
            samesite="lax",
            path="/",
        )

    def clear_cookie(self, response: Any) -> None:
        """删除会话饼干与 CSRF 饼干"""
        secure = self.cookie_secure
        response.delete_cookie(
            key=self.COOKIE_NAME,
            path="/",
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        response.delete_cookie(
            key=self.CSRF_COOKIE_NAME,
            path="/",
            httponly=False,
            secure=secure,
            samesite="lax",
        )


# --------------------------------------------------------------------------- #
# FastAPI 依赖
# --------------------------------------------------------------------------- #
async def current_user(request: Request) -> dict:
    """依赖：当前登录用户（未登录 401）

    用户由中间件在请求进入路由前装载到 request.state.user。
    """
    user = getattr(getattr(request, "state", None), "user", None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
        )
    return user


def require_level(level: int) -> Callable:
    """依赖工厂：要求用户等级不低于 level（等级越负数越高）

    - -128 超级管理员 / -100 管理员 / 0 普通用户；
    - 用户等级 > 要求等级（即权限更低）时拒绝，返回 403。
    """
    required = _to_int(level, LEVEL_USER)

    async def _checker(request: Request) -> dict:
        user = getattr(getattr(request, "state", None), "user", None)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未登录或登录已过期",
            )
        user_level = _to_int(user.get("level", LEVEL_USER), LEVEL_USER)
        if user_level > required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return user

    _checker.__name__ = f"require_level_{required}"
    return _checker


async def get_client_ip(request: Request) -> str:
    """获取客户端真实 IP

    仅当对端地址（request.client.host）属于可信代理时才信任 X-Forwarded-For，
    否则一律使用对端地址，避免伪造来源 IP。可信代理由配置 trusted_proxies 提供。
    """
    peer_ip = "未知"
    try:
        if request is not None and getattr(request, "client", None):
            peer_ip = request.client.host or "未知"
    except Exception:
        peer_ip = "未知"

    proxies = _TRUSTED_PROXIES
    # 应用级覆盖（中间件注册时写入 app.state.trusted_proxies）
    try:
        app = getattr(request, "app", None)
        state_proxies = getattr(getattr(app, "state", None), "trusted_proxies", None)
        if state_proxies:
            proxies = set(state_proxies)
    except Exception:
        pass

    if peer_ip and peer_ip in proxies:
        try:
            forwarded = request.headers.get("X-Forwarded-For")
        except Exception:
            forwarded = None
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                return candidate
    return peer_ip
