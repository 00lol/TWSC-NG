# -*- coding:utf-8 -*-
"""TWSC-NG 启动入口

同步阶段（本模块导入时执行）：
    日志 -> 配置 -> 邮箱 -> 应用外壳（中间件/CORS） -> 路由
异步阶段（web.py 的 lifespan 调用 startup/shutdown）：
    缓存连接 -> 数据库连接/建表/插件 -> 认证初始化 -> 超级管理员引导

这样拆分的原因：asyncmy 连接池绑定事件循环，必须建在 uvicorn 的事件循环里。
"""
import os
import sys
import time

import basic
import cache
import databases
import transaction
import value

# 全局对象（模块级：uvicorn 热重载会用 "main:app" 重新导入本模块）
Value = value.Main()
# Web 应用外壳
WebApp = None


def _init_email(server):
    """初始化邮箱；未启用或配置不全时返回 None（不影响服务启动）"""
    logger = Value.logger
    if not server.get("email"):
        logger.warning("邮件未启用")
        return None
    email_cfg = Value.config.get("email", {}) or {}
    if not email_cfg.get("user") or not email_cfg.get("password"):
        logger.warning("邮箱已启用但账号/授权码未配置，跳过邮箱初始化")
        return None
    try:
        mail = basic.AutoEmail(
            logger,
            sender=email_cfg["user"],
            password=email_cfg["password"],
        )
        logger.info("邮件已启用")
        return mail.main()
    except Exception as e:
        logger.error(f"邮箱初始化失败: {e}")
        return None


def _assemble():
    """同步组装整个应用（不做任何网络 I/O）"""
    global WebApp
    # 1. 日志 + 系统兼容检测 + 文件/配置检查
    Value.logger = basic.Go().main()
    Value.start_time = time.time()

    # 2. 结构化配置
    Value.config = value.Config(Value.logger).main()
    server = Value.config.get("server", {}) or {}

    # 3. 邮箱（发信函数注入 value，供路由发验证码使用）
    Value.email = _init_email(server)

    # 4. 缓存服务（连接在 lifespan 里做，失败会降级内存缓存）
    Value.cache = cache.Cache(Value.logger, Value.config.get("redis", {}) or {})

    # 5. 数据库管理器（连接/建表/插件在 lifespan 里做）
    Value.database = databases.DatabaseManager(Value)

    # 6. 认证总控（密钥、会话存储在 lifespan 里初始化）
    Value.security = transaction.Security(Value)

    # 7. Web 外壳：FastAPI + 中间件 + CORS + 静态/模板
    WebApp = basic.FastAPIWebApp(Value)
    Value.app = WebApp.Init()

    # 8. 路由挂载
    transaction.Routing(Value)

    # 9. 生命周期钩子与配置重载
    Value.startup = startup
    Value.shutdown = shutdown
    Value.reload_config = reload_config
    return WebApp


async def startup():
    """lifespan 启动钩子：连接外部依赖并完成引导"""
    logger = Value.logger
    logger.info(f"{'=' * 12} 服务启动 {'=' * 12}")

    # 缓存：失败自动降级内存缓存
    if Value.cache is not None:
        await Value.cache.init()

    # 数据库：失败不阻断启动，相关接口返回 503
    db_ok = False
    if Value.database is not None:
        db_ok = await Value.database.init()
        if not db_ok:
            logger.warning("数据库不可用：用户认证与后台接口将返回 503")

    # 认证：密钥、饼干签名器、会话存储
    if Value.security is not None:
        await Value.security.init()

    # 无超级管理员时引导创建（参考 TWSC 的交互式初始化）
    if db_ok:
        await _bootstrap_admin()

    logger.info(f"{'=' * 12} 服务就绪 {'=' * 12}")


async def _bootstrap_admin():
    """初始化超级管理员：交互式确认，非交互环境自动生成随机密码"""
    logger = Value.logger
    user = Value.database.user
    try:
        if await user.transfer():
            logger.info("超级管理员已存在，跳过引导")
            return
    except Exception as e:
        logger.error(f"检查超级管理员失败: {e}")
        return

    server = Value.config.get("server", {}) or {}
    username = server.get("admin_user") or "root"
    password = server.get("admin_password") or ""
    interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    if not password:
        if interactive:
            logger.info("数据库中未找到初始用户，即将设置....")
            name = input(f"请设置超级管理员账号：(默认{username})>>>").strip()
            if name:
                username = name
            rand = os.urandom(10).hex()
            answer = input(f"随机密码为：[{rand}],是否同意？(同意：回车/不同意：输入新的密码)>>>").strip()
            password = answer or rand
        else:
            # 非交互式环境（容器/后台运行）不能卡在 input 上
            password = os.urandom(8).hex()
            logger.warning(f"非交互式环境，已生成超级管理员：{username} / {password} （请登录后立即修改密码）")

    ok, msg = await user.add_user(
        username=username,
        password_hash=transaction.Password.hash(password),
        level=-128,
        belong=0,
    )
    if ok:
        logger.info(f"超级管理员 {username} 创建成功")
    else:
        logger.error(f"超级管理员创建失败: {msg}")


async def shutdown():
    """lifespan 关闭钩子：释放连接"""
    logger = Value.logger
    logger.info("开始关闭服务...")
    if Value.database is not None:
        try:
            await Value.database.close()
        except Exception as e:
            logger.error(f"关闭数据库失败: {e}")
    if Value.cache is not None:
        try:
            await Value.cache.close()
        except Exception as e:
            logger.error(f"关闭缓存失败: {e}")
    logger.info("服务已关闭")


def reload_config():
    """重载配置（同步）：重读配置文件；数据库/缓存连接需重启服务才生效"""
    logger = Value.logger
    logger.info("重载配置文件")
    try:
        basic.Files(logger)  # 重新检查/补全配置文件
        Value.config = value.Config(logger).main()
        logger.info("配置已重载（数据库/缓存连接需重启服务生效）")
    except Exception as e:
        logger.error(f"重载配置失败: {e}")


def create_app():
    """创建应用：供外部 ASGI 服务器（uvicorn ./gunicorn）导入使用"""
    webapp = _assemble()
    return webapp.Init()


# 模块级应用实例：uvicorn 热重载使用 "main:app" 导入字符串
WebApp = _assemble()
app = Value.app


if __name__ == "__main__":
    Value.logger.info("TWSC-NG 启动中...")
    server_cfg = Value.config.get("server", {})
    url = server_cfg.get("ip", "127.0.0.1")
    if url == "0.0.0.0":
        url = "127.0.0.1"
        msg = f"在本机所有IP上监听，端口为：{server_cfg.get('port')}"
    else:
        msg = f"已绑定IP:[ {url} ] ，端口为：{server_cfg.get('port')}"
    Value.logger.info(f"服务器 {msg} 启动，等待连接")
    scheme = "https" if server_cfg.get("ssl", False) else "http"
    Value.logger.info(f"使用 {scheme}://{url}:{server_cfg.get('port')} 访问")
    WebApp.run(times=0, server_type="uvicorn")
