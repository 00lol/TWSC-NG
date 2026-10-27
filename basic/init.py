# 服务器日志、系统检测初始化
import os
import platform
import socket
import sys
import psutil
from loguru import logger
from .files import Main as Files


class SystemTest:
    def __init__(self,logger):
        # 获取操作系统可执行程序的结构
        structure = platform.architecture()
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)

        logger.info(f"系统内核: {structure[1]}x{structure[0]}")
        jiago = ["Windows", "Linux"]
        for i in jiago:
            if structure[1].find(i) == -1:
                break
        else:
            raise ValueError("系统不兼容！请使用Windows或者Linux系统")
        if structure[0] != "64bit":
            logger.warning("使用x32架构可能会出现兼容性问题，建议使用x64架构")

        # 系统版本
        logger.info(f"系统操作版本: {platform.platform()}")

        # 计算机的网络名称
        logger.info(f"网络名称: {platform.node()}")

        # 计算机处理器信息
        logger.info(f"CPU: {platform.processor()}")

        # 计算机ip地址
        logger.info(f"服务器所在IP: {ip}")
        try:
            mem = psutil.virtual_memory()
            logger.info(f"内存大小: {round(mem.total / 1024 ** 3, 2)} GB")
        except Exception as e:
            logger.error(f"获取内存信息失败: {str(e)}")

        # 检测python版本编译
        ver = sys.version
        logger.info(f"Python 版本: {ver}")
        logger.info(f"Python 已向下兼容到3.6.8! 但最好是使用3.11.9及以上版本")

        if int(ver[0]) < 3:
            raise ValueError("无法使用旧版Python，请Python版本升级到3.7.4!")
        elif "3.11.9" != ver.split(" ")[0]:
            logger.warning("尽量使用Python 3.11.9 及以上版本 ，防止出现兼容问题")


class LoggerInit:
    def __init__(self):
        """日志初始化"""
        # 移除默认的 handler
        logger.remove()

        # 输出到控制台
        logger.add(
            sink=sys.stdout,
            level="DEBUG",
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> |<level>{level: <8}</level> |<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        )

        # 输出到文件（按时间切分）
        os.makedirs("log", exist_ok=True)  # 确保日志目录存在
        logger.add(
            sink="log/app_{time:YYYY-MM-DD}.log",
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {process} | {level} | {name}-{module}:{message} \n| Lo:[{function}-{line}] [{process.name}:{process.id} | {thread.name}:{thread.id}]",
            rotation="00:00",  # 每天午夜切分
            retention="60 days",  # 保留最近 60 天的日志
            encoding="utf-8",
            compression="zip"  # 可选：压缩旧日志文件
        )
        logger.info(self.logo())

        self.logger = logger

    def logo(self):
        return "TWSC-NG"

    def main(self):
        return self.logger

class Go:
    def __init__(self):
        self.logger = LoggerInit().main()
        SystemTest(self.logger)
        Files(self.logger)

    def main(self):
        return self.logger
