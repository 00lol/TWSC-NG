# -*- coding:utf-8 -*-
"""路由聚合入口

参考旧版 TWSC `business/routing/routingmain.py` 的容错写法：
逐个实例化路由类，单个路由初始化 / 挂载失败只记录错误日志，不影响其它路由。

使用方式（main.py，在 app 创建之后、uvicorn 启动之前）::

    transaction.Routing(Value)
    # 或
    transaction.Routing(Value).main()
"""
from typing import Dict

from fastapi import APIRouter

from .routes import Admin, Auth, Index, get_base_url


class Routing:
    """路由聚合类"""

    # 路由类清单（类名, 类对象）
    ROUTERS = (
        ("Index", Index),
        ("Auth", Auth),
        ("Admin", Admin),
    )

    def __init__(self, value):
        self.value = value
        self.logger = getattr(value, "logger", None)
        if self.logger is None:
            raise ValueError("Routing 初始化失败：value.logger 未初始化")
        self.base_url = get_base_url(value)
        self.routes: Dict[str, APIRouter] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        loaded = []
        for name, router_class in self.ROUTERS:
            self.logger.debug(f"{name} 路由初始化...")
            try:
                instance = router_class(self.value)
                router = getattr(instance, "router", None)
                if router is None:
                    raise ValueError("路由实例未提供 router 属性")
                loaded.append({"name": name, "router": router})
            except Exception as e:
                self.logger.error(f"{name} 路由初始化失败: {e}")

        app = getattr(self.value, "app", None)
        if app is None:
            self.logger.error("value.app 未就绪，路由未挂载")
            return

        self.logger.info("路由初始化完成，正在加载路由")
        for item in loaded:
            try:
                app.include_router(item["router"])
                self.routes[item["name"]] = item["router"]
                self.logger.debug(f"{item['name']} 路由加载成功")
            except Exception as e:
                self.logger.error(f"{item['name']} 路由加载失败: {e}")

        # 记录到 value.routes（可选，供插件/调试查看）
        try:
            existing = getattr(self.value, "routes", None)
            if isinstance(existing, dict):
                existing.update(self.routes)
            else:
                self.value.routes = dict(self.routes)
        except Exception as e:
            self.logger.warning(f"记录 value.routes 失败: {e}")

        self.logger.info(
            f"全部路由加载完成：成功 {len(self.routes)} 个"
            f"（Index/Auth/Admin），前缀 base_url={self.base_url or '(空)'}"
        )

    # ------------------------------------------------------------------ #
    def main(self) -> "Routing":
        """返回自身的可用实例（项目风格：main() 返回可用对象）"""
        return self
