# -*- coding:utf-8 -*-
"""配置解析：把 config/<组>/.env.* 里的环境变量整理成结构化字典

设计原则：
1. 只有本文件直接读 os.getenv，其他模块一律使用 value.config；
2. 环境变量优先于配置文件（basic/files.py 用 load_dotenv(override=False) 加载）；
3. 配置缺失时给出安全默认值，不让服务因为少配一个变量就起不来。
"""
import os


def _str(name, default=""):
    """读取字符串型环境变量（不去空格，密码里可能有空格）"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw)


def _text(name, default=""):
    """读取需要去空格的字符串"""
    return _str(name, default).strip()


def _int(name, default=0):
    """读取整数型环境变量，非法值退回默认值"""
    try:
        return int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _bool(name, default=False):
    """读取布尔型环境变量（1/true/yes/on 视为真）"""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


class Config:
    """配置解析器：main() 返回可直接使用的配置字典"""

    def __init__(self, logger):
        self.logger = logger
        self.config = {}

    # 服务器配置
    def server(self):
        """服务器配置：监听地址、端口、调试、饼干有效期、跨域等"""
        ip = _text("SERVER_IP", "127.0.0.1")
        port = _int("SERVER_PORT", 8000)
        debug = _bool("SERVER_DEBUG", True)
        ssl = _bool("SERVER_SSL", False)
        scheme = "https" if ssl else "http"
        # 跨域来源：逗号分隔；未配置时只允许本机同源
        cors = [i.strip() for i in _text("SERVER_CORS").split(",") if i.strip()]
        if not cors:
            cors = [f"{scheme}://127.0.0.1:{port}", f"{scheme}://localhost:{port}"]
            if ip not in ("0.0.0.0", "127.0.0.1", "localhost"):
                cors.append(f"{scheme}://{ip}:{port}")
        return {
            "ip": ip,
            "port": port,
            "debug": debug,
            "email": _bool("SERVER_EMAIL", False),
            "config": _text("SERVER_CONFIG"),
            "secret": _text("SERVER_SECRET"),
            "login_validity": _int("SERVER_LOGIN_VALIDITY", 1440),  # 单位：分钟
            "register": _bool("SERVER_REGISTER", False),
            "admin_user": _text("SERVER_ADMIN_USER", "root"),
            "admin_password": _str("SERVER_ADMIN_PASSWORD", ""),
            # 管理台（/admin/*）：Bearer 令牌有效期、演示数据开关、首次启动管理员
            "console_token_hours": max(1, _int("SERVER_CONSOLE_TOKEN_HOURS", 12)),
            "console_seed": _bool("SERVER_CONSOLE_SEED", False),
            "console_admin_user": _text("SERVER_CONSOLE_ADMIN_USER", "admin_super"),
            "console_admin_password": _str("SERVER_CONSOLE_ADMIN_PASSWORD", ""),
            "cors": cors,
            "trusted_proxies": [
                i.strip() for i in _text("SERVER_TRUSTED_PROXIES").split(",") if i.strip()
            ],
            "base_url": _text("SERVER_BASE_URL", ""),
            "ssl": ssl,
            # 饼干是否只走 HTTPS：默认跟随 SSL 开关，可单独覆盖
            "cookie_secure": _bool("SERVER_COOKIE_SECURE", ssl),
            "ssl_cert": _text("SERVER_SSL_CERT", "./key/cert.pem"),
            "ssl_key": _text("SERVER_SSL_KEY", "./key/key.pem"),
        }

    # 数据库配置
    def db(self):
        """数据库配置：当前只实现 mysql，engine 留给插件形态扩展"""
        return {
            "engine": _text("DB_ENGINE", "mysql").lower(),
            "host": _text("DB_HOST", "127.0.0.1"),
            "port": _int("DB_PORT", 3306),
            "user": _text("DB_USER", "root"),
            "password": _str("DB_PASSWORD", ""),
            "database": _text("DB_DB", "twsc"),
            "charset": _text("DB_CHARSET", "utf8mb4"),
            "maxsize": max(1, _int("DB_MAXSIZE", 10)),
            "minsize": max(0, _int("DB_MININSIZE", 2)),
        }

    # 缓存配置
    def redis(self):
        """Redis 配置：连接失败会自动降级为内存缓存"""
        return {
            "enabled": _bool("REDIS_ENABLED", True),
            "host": _text("REDIS_HOST", "127.0.0.1"),
            "port": _int("REDIS_PORT", 6379),
            "password": _str("REDIS_PASSWORD", ""),
            "db": _int("REDIS_DB", 0),
            "maxsize": max(1, _int("REDIS_MAXSIZE", 5)),
            "timeout": max(1, _int("REDIS_TIMEOUT", 5)),
        }

    # 邮箱配置
    def email(self):
        """邮箱配置：只保留发信账号，具体 SMTP 地址由 basic/mail.py 自动识别"""
        return {
            "user": _text("EMAIL_USER"),
            "password": _str("EMAIL_PASSWORD", ""),
        }

    def main(self):
        """解析全部配置并返回"""
        self.config = {
            "server": self.server(),
            "db": self.db(),
            "redis": self.redis(),
            "email": self.email(),
        }
        server = self.config["server"]
        if self.logger:
            self.logger.info(f"配置组: {server['config'] or '未设置'} | 监听 {server['ip']}:{server['port']} | 调试模式: {server['debug']}")
            self.logger.info(f"数据库: {self.config['db']['engine']}://{self.config['db']['user']}@{self.config['db']['host']}:{self.config['db']['port']}/{self.config['db']['database']}")
            self.logger.info(f"缓存: {'redis' if self.config['redis']['enabled'] else '内存降级'} {self.config['redis']['host']}:{self.config['redis']['port']}/{self.config['redis']['db']}")
            self.logger.info(f"邮箱: {'已启用' if server['email'] else '未启用'}")
        return self.config
