# -*- coding:utf-8 -*-
"""基础自检：数据库连通性

独立于服务运行，可以单独执行：
    .venv\\Scripts\\python.exe -m basic.test
读取的环境变量与 basic/files.py 加载的一致（需先有 __main__ 里的 dotenv 加载）。
"""
import asyncio
import os

import asyncmy
from asyncmy import Pool


def _env_int(name, default):
    try:
        return int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default


# 数据库检查
class DB:
    """数据库检查"""

    def __init__(self, logger):
        """分为2部分"""
        self.logger = logger

    async def main(self):
        """连接数据库并返回 (是否成功, 版本信息/错误信息)"""
        pool: Pool = None
        try:
            pool = await asyncmy.create_pool(
                host=os.getenv("DB_HOST", "127.0.0.1"),
                port=_env_int("DB_PORT", 3306),
                user=os.getenv("DB_USER", "root"),
                password=os.getenv("DB_PASSWORD", ""),
                database=os.getenv("DB_DB", "twsc"),
                charset=os.getenv("DB_CHARSET", "utf8mb4"),
                maxsize=_env_int("DB_MAXSIZE", 10),
                minsize=_env_int("DB_MININSIZE", 2),
                connect_timeout=5,
            )
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT VERSION()")
                    version = await cur.fetchone()
            return True, version
        except Exception as e:
            return False, str(e)
        finally:
            if pool is not None:
                try:
                    pool.close()
                    await pool.wait_closed()
                except Exception:
                    pass


if __name__ == "__main__":
    from dotenv import load_dotenv
    from loguru import logger

    # 独立执行时手动加载配置组
    group = os.getenv("TWSC_CONFIG", "A")
    if os.path.isdir("config"):
        path = f"config/{group}/.env.db"
        if os.path.exists(path):
            load_dotenv(path, override=False)
            logger.info(f"已加载数据库配置: {path}")
        else:
            logger.warning(f"未找到数据库配置: {path}")
    ok, msg = asyncio.run(DB(logger).main())
    if ok:
        logger.info(f"数据库连接成功，版本: {msg[0] if msg else '未知'}")
    else:
        logger.error(f"数据库连接失败: {msg}")
