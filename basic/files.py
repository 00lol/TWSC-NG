# 此文件为主要检查并补全文件
import os
import secrets

from dotenv import load_dotenv


## 检测
class Test:
    def __init__(self, logger):
        """
        初始化测试类
        """
        self.files = [
            {"cache": ""},
            {"log": ""},
            {"config":
                [
                    {"A": [".env.db", ".env.redis", ".env.email", ".env.server"]},
                    {"B": [".env.db", ".env.redis", ".env.email", ".env.server"]}
                ]
            }
        ]
        self.logger = logger

    def main(self):
        """递归检查所有文件/文件夹是否存在"""
        self._check(self.files, ".")

    def _check(self, structure, base_path):
        """递归检查 structure 描述的文件/文件夹是否存在

        Args:
            structure: 可以是 list / dict / str
            base_path: 当前所在的目录路径
        """
        try:
            if isinstance(structure, list):
                # 文件夹下的多个子项，逐个检查
                for item in structure:
                    self._check(item, base_path)

            elif isinstance(structure, dict):
                for name, children in structure.items():
                    dir_path = os.path.join(base_path, name)
                    if not os.path.isdir(dir_path):
                        self.logger.debug(f"[缺失文件夹] {dir_path}")
                    # children 可能是 ""（空文件夹）或 list（有子项）
                    if children:  # 非空才递归
                        self._check(children, dir_path)

            elif isinstance(structure, str):
                # 叶子节点：文件名
                file_path = os.path.join(base_path, structure)
                if not os.path.isfile(file_path):
                    self.logger.debug(f"[缺失文件] {file_path}")

        except Exception as e:
            self.logger.error(f"检查文件/文件夹时出错: {e}")


# 创建
class Create:
    def __init__(self, structure, logger, base_path="."):
        """
        创建文件/文件夹

        Args:
            structure: 文件结构描述，格式参考 Test.files
            base_path: 从哪个目录开始创建，默认为当前目录
        """
        self.structure = structure
        self.base_path = base_path

        self.logger = logger

    def run(self):
        """执行递归创建"""
        self._create(self.structure, self.base_path)

    def _create(self, structure, base_path):
        """递归创建文件/文件夹

        structure 有三种类型：
        - list:     文件夹下的多个子项，遍历递归
        - dict:     {name: children}  → name 是文件夹，children 决定是否继续递归
        - str:      叶子节点，是一个文件名，直接创建空文件
        """
        try:
            if isinstance(structure, list):
                for item in structure:
                    self._create(item, base_path)

            elif isinstance(structure, dict):
                for name, children in structure.items():
                    dir_path = os.path.join(base_path, name)
                    # 先确保父目录存在
                    os.makedirs(dir_path, exist_ok=True)
                    # children 为 "" 表示空文件夹；为 list 表示有子项需要继续递归
                    if children:
                        self._create(children, dir_path)

            elif isinstance(structure, str):
                # 文件：确保其父目录存在，然后以 utf-8 创建空文件
                file_path = os.path.join(base_path, structure)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                if not os.path.isfile(file_path):
                    with open(file_path, "w", encoding="utf-8") as f:
                        pass
                    self.logger.debug(f"[创建文件] {file_path}")
                else:
                    self.logger.debug(f"[已存在]   {file_path}")
        except Exception as e:
            self.logger.error(f"创建文件/文件夹时出错: {e}")


# 读取
class Read:
    """读取配置文件：文件缺失时创建，变量缺失时补默认值，再加载进环境变量"""

    def __init__(self, logger):
        """主要读取配置文件"""
        self.logger = logger
        # 配置组目录（A/B/...）
        self.configList = sorted(
            i for i in os.listdir("config")
            if os.path.isdir(os.path.join("config", i))
        ) if os.path.isdir("config") else []

    # 选择配置组：环境变量 TWSC_CONFIG / SERVER_CONFIG 优先，否则用第一个目录
    def _group(self):
        want = (os.getenv("TWSC_CONFIG") or os.getenv("SERVER_CONFIG") or "").strip()
        if want and want in self.configList:
            return want
        if want:
            self.logger.warning(f"[配置组] {want} 不存在，回退到 {self.configList[0] if self.configList else 'A'}")
        if not self.configList:
            self.logger.error("未找到任何配置组目录 config/*")
            return "A"
        return self.configList[0]

    # 补全缺失的环境变量（不覆盖已有值）
    def _ensure(self, path, defaults: dict):
        """补齐缺失的环境变量项

        Args:
            path: 配置文件路径
            defaults: {变量名: 默认值}，只追加文件里没有的键
        """
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        exists = {
            line.split("=", 1)[0].strip()
            for line in content.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        missing = [(k, v) for k, v in defaults.items() if k not in exists]
        if not missing:
            return
        with open(path, "a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            for key, default in missing:
                f.write(f"{key}={default}\n")
        self.logger.debug(f"[补全配置] {path}: {', '.join(k for k, _ in missing)}")

    def _load(self, path):
        """加载环境文件；系统环境变量优先，便于容器化部署覆盖"""
        load_dotenv(path, override=False)
        self.logger.info(f"[加载配置] {path}")

    # 数据库配置默认值
    def _defaults_db(self):
        return {
            "DB_ENGINE": "mysql",
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "3306",
            "DB_USER": "root",
            "DB_PASSWORD": "''",
            "DB_DB": "twsc",
            "DB_CHARSET": "utf8mb4",
            "DB_MAXSIZE": "10",
            "DB_MININSIZE": "2",
        }

    # 数据库文件读取
    def db(self, group):
        """读取数据库配置"""
        name = f"config/{group}/.env.db"
        self.logger.info(f"开始检测数据库配置组 {group}")
        self._ensure(name, self._defaults_db())
        self._load(name)

    # 缓存配置默认值
    def _defaults_redis(self):
        return {
            "REDIS_ENABLED": "True",
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": "6379",
            "REDIS_PASSWORD": "''",
            "REDIS_DB": "0",
            "REDIS_MAXSIZE": "5",
            "REDIS_TIMEOUT": "5",
        }

    # 缓存读取
    def Redis(self, group):
        """读取缓存配置（连接在服务启动时进行，失败自动降级内存缓存）"""
        name = f"config/{group}/.env.redis"
        self.logger.info(f"开始检测缓存配置组 {group}")
        self._ensure(name, self._defaults_redis())
        self._load(name)

    # 邮箱配置默认值
    def _defaults_email(self):
        return {
            "EMAIL_USER": "''",
            "EMAIL_PASSWORD": "''",
        }

    # 邮箱
    def email(self, group):
        """读取邮箱配置"""
        if os.getenv("SERVER_EMAIL", "False").strip().lower() not in ("1", "true", "yes", "y", "on"):
            self.logger.warning("[跳过邮箱配置] SERVER_EMAIL 未启用")
            return
        name = f"config/{group}/.env.email"
        self._ensure(name, self._defaults_email())
        self._load(name)

    # 服务器配置默认值
    def _defaults_server(self, group):
        return {
            "SERVER_IP": "127.0.0.1",
            "SERVER_PORT": "8000",
            "SERVER_DEBUG": "True",
            "SERVER_EMAIL": "False",
            "SERVER_CONFIG": group,
            "SERVER_SECRET": "",
            "SERVER_LOGIN_VALIDITY": "1440",
            "SERVER_REGISTER": "False",
            "SERVER_ADMIN_USER": "root",
            "SERVER_ADMIN_PASSWORD": "",
            "SERVER_CONSOLE_TOKEN_HOURS": "12",
            "SERVER_CONSOLE_SEED": "False",
            "SERVER_CONSOLE_ADMIN_USER": "admin_super",
            "SERVER_CONSOLE_ADMIN_PASSWORD": "",
            "SERVER_CORS": "",
            "SERVER_TRUSTED_PROXIES": "",
            "SERVER_BASE_URL": "",
            "SERVER_COOKIE_SECURE": "",
            "SERVER_SSL": "False",
            "SERVER_SSL_CERT": "./key/cert.pem",
            "SERVER_SSL_KEY": "./key/key.pem",
        }

    # 服务端配置
    def server(self, group):
        """读取服务器配置，并补全饼干签名密钥"""
        name = f"config/{group}/.env.server"
        self._ensure(name, self._defaults_server(group))
        self._load(name)
        # 饼干签名密钥：为空时生成并写回配置文件，保证重启后登录态不失效
        if not os.getenv("SERVER_SECRET", "").strip():
            secret = secrets.token_hex(32)
            with open(name, "r", encoding="utf-8") as f:
                content = f.read()
            with open(name, "a", encoding="utf-8") as f:
                if content and not content.endswith("\n"):
                    f.write("\n")
                f.write(f"SERVER_SECRET={secret}\n")
            os.environ["SERVER_SECRET"] = secret
            self.logger.info("[补全配置] 已生成新的饼干签名密钥 SERVER_SECRET")
        os.environ["SERVER_CONFIG"] = group

    def main(self):
        group = self._group()
        self.logger.info(f"开始读取配置文件，配置组为：{group}")
        self.server(group)  # 先加载服务器配置（下面的开关依赖它）
        self.db(group)
        self.Redis(group)
        self.email(group)   # 最后加载邮箱配置


class Main:
    def __init__(self, logger):
        t = Test(logger)
        Create(t.files, logger).run()
        Read(logger).main()


if __name__ == "__main__":
    # 使用示例
    Main(None)
