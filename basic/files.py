# 此文件为主要检查并补全文件
import os
from dotenv import load_dotenv
from .test import DB as testDB
import asyncio
## 检测
class Test:
    def __init__(self,logger):
        """
        初始化测试类
        """
        self.files = [
            {"cache": ""},
            {"log": ""},
            {"config":
                [
                    {"A": [".env.db",".env.redis", ".env.email", ".env.server"]},
                    {"B": [".env.db",".env.redis", ".env.email", ".env.server"]}
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
    def __init__(self, structure, logger,base_path="."):
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
    def __init__(self,logger):
        """主要读取配置文件"""
        self.logger = logger
        self.configList = os.listdir("config")

    # 数据库文件读取至内存中
    def db(self):
        """读取数据库配置"""
        for i in self.configList:
            self.logger.info(f"开始检测数据库 {i} 组配置")
            name = f"config/{i}/.env.db"
            if not os.path.exists(name) or os.path.getsize(name) == 0:
                self.logger.debug(f"[空文件] {name} 将写入默认配置...")
                # 写入默认配置
                with open(name, "w", encoding="utf-8") as f:
                    f.write("DB_HOST=127.0.0.1\n"
                            "DB_PORT=3306\n"
                            "DB_USER=root\n"
                            "DB_PASSWORD=''\n"
                            "DB_DB=twac\n"
                            "DB_CHARSET=utf8mb4\n"
                            "DB_MAXSIZE=10\n"
                            "DB_MININSIZE=2\n")
            # 加载
            load_dotenv(name, override=True)
            # 检测数据库连接
            success, msg = asyncio.run(testDB(self.logger).main())
            if success:
                self.logger.info(f"数据库 {i} 组连接成功! 数据库版本: {msg[0]}")
                break
            else:
                self.logger.error(f"数据库 {i} 组连接失败: {msg}")

    # 缓存读取
    def Redis(self):
        for i in self.configList:
            self.logger.info(f"开始检测缓存 {i} 组配置")
            name = f"config/{i}/.env.redis"
            if not os.path.exists(name) or os.path.getsize(name) == 0:
                self.logger.debug(f"[空文件] {name} 将写入默认配置...")
                # 写入默认配置
                with open(name, "w", encoding="utf-8") as f:
                    f.write("REDIS_HOST=127.0.0.1\n"  # IP
                            "REDIS_PORT=6379\n"     # 端口
                            "REDIS_PASSWORD=''\n"  # 密码
                            "REDIS_DB=0\n"          # 数据库
                            )
            # 加载
            load_dotenv(name, override=True)

    # 邮箱
    def email(self,config):
        """读取邮箱配置"""
        if os.getenv("SERVER_EMAIL") == "False":
            self.logger.warning("[跳过邮箱配置] SERVER_EMAIL 未启用")
            return
        name = f"config/{config}/.env.email"
        if not os.path.exists(name) or os.path.getsize(name) == 0:
            self.logger.debug(f"[空文件] {name} 将写入默认配置...")
            # 写入默认配置
            with open(name, "w", encoding="utf-8") as f:
                f.write("EMAIL_USER=root\n"  # 邮箱用户名
                        "EMAIL_PASSWORD=123456\n") # 邮箱密码

        # 加载
        load_dotenv(name, override=True)
        self.logger.info(f"[加载邮箱配置] {name}")

    # 服务器配置
    def server(self,config):
        """读取服务器配置"""
        name = f"config/{config}/.env.server"
        if not os.path.exists(name) or os.path.getsize(name) == 0:
            self.logger.debug(f"[空文件] {name} 将写入默认配置...")
            # 写入默认配置
            with open(name, "w", encoding="utf-8") as f:
                f.write("SERVER_IP=127.0.0.1\n"  # IP
                        "SERVER_PORT=8000\n"     # 端口
                        "SERVER_DEBUG=True\n"    # debug模式 
                        "SERVER_EMAIL=False\n")  # 是否使用邮箱
        # 加载
        load_dotenv(name, override=True)
        self.logger.info(f"[加载服务器配置] {name}")

    def main(self):
        self.logger.info(f"开始读取配置文件,配置组为：{self.configList[0]}")
        self.db()
        self.server(self.configList[0])  # 先加载服务器配置
        self.email(self.configList[0])  # 再加载邮箱配置（依赖服务器配置）

class Main:
    def __init__(self,logger):
        t = Test(logger)
        Create(t.files,logger).run()
        Read(logger).main()

if __name__ == "__main__":
    # 使用示例
    Main(None)