import os
import asyncmy
from asyncmy import Pool

# 数据库检查
class DB:
    """数据库检查"""
    def __init__(self, logger):
        """分为2部分"""
        self.logger = logger

    async def main(self):
        try:
            pool: Pool = await asyncmy.create_pool(
                host=os.getenv("DB_HOST"),
                port=os.getenv("DB_PORT") if type(os.getenv("DB_HOST")) == int else int(os.getenv("DB_PORT")),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                database=os.getenv("DB_DB"),
                charset=os.getenv("DB_CHARSET"),
                maxsize=10,
                minsize=2
            )
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT VERSION()")
                    version = await cur.fetchone()
            pool.close()
            await pool.wait_closed()
            return True, version
        except Exception as e:
            return False, str(e)

# 邮箱检查