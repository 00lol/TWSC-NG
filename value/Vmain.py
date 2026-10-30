# -*- coding:utf-8 -*-
"""全局变量文件：定义全局对象，所有模块共享同一个 value

约定：
1. 这里只声明"位置"，不初始化需要 I/O 的对象；
2. 谁负责创建，谁就赋值（main.py / basic / cache / databases / transaction）；
3. 新增底层变量请新建文件（见 value/规则），不要把所有逻辑塞进本文件。
"""


class Main:
    def __init__(self):
        # 日志（basic/init.py 注入）
        self.logger = None
        # 邮箱发信函数 send(receiver, html_content, html_title) -> (bool, str)
        self.email = None
        # 结构化配置（value/Vconfig.py 注入）
        self.config = {}
        # 数据库管理器（databases/mains.py，None 表示未初始化/不可用）
        self.database = None
        # 缓存服务（cache/mains.py）
        self.cache = None
        # 认证总控（transaction/auth.py）
        self.security = None
        # FastAPI 应用与模板
        self.app = None
        self.templates = None
        # SocketIO
        self.socketio = None
        self.sio_app = None
        # 已挂载的路由（name -> APIRouter）
        self.routes = {}
        # 启动时间与系统信息
        self.start_time = None
        self.system = {}
        # 插件（{"插件名": 插件实例}）
        self.plugin = self.Plugin()
        # lifespan 钩子，由 main.py 注入，basic/web.py 调用
        self.startup = None
        self.shutdown = None
        # 配置重载函数（同步）
        self.reload_config = None

    # 插件
    class Plugin:
        """插件容器，与 TWSC 的 value.plugin 结构保持一致"""

        def __init__(self):
            self.database_business = {}  # 数据库业务插件
            self.route = {}              # 路由插件（预留）
