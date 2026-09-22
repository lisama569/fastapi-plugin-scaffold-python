"""依赖注入容器：应用级单例与请求级实例的隔离。

- :class:`Container` 为 *应用级* 容器，装配成功后保存每个 APP 作用域
  插件的唯一实例；受 asyncio.Lock 保护，并发请求读取不会触发重复创建。
- :class:`RequestContainer` 为 *请求级* 容器，持有请求级插件的惰性
  实例以及对应用级容器的只读访问；请求结束调用 :meth:`aclose` 释放，
  请求级实例因此不会跨请求泄漏。
"""

from __future__ import annotations

import asyncio
from enum import Enum


class Scope(str, Enum):
    APP = "app"
    REQUEST = "request"


class Container:
    """应用级容器。"""

    def __init__(self) -> None:
        self._instances: dict[str, object] = {}
        self._request_factories: dict[str, object] = {}
        self._lock = asyncio.Lock()

    def register_request_factory(self, name: str, factory: object) -> None:
        self._request_factories[name] = factory

    async def set_app_instance(self, name: str, instance: object) -> None:
        async with self._lock:
            self._instances[name] = instance

    def get_app_instance(self, name: str) -> object:
        if name not in self._instances:
            raise KeyError(f"应用级实例尚未装配: {name}")
        return self._instances[name]

    def has(self, name: str) -> bool:
        return name in self._instances

    async def remove_app_instance(self, name: str) -> None:
        """装配失败回滚时撤销已放入的应用级实例。"""
        async with self._lock:
            self._instances.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._instances)

    def build_request_container(self) -> RequestContainer:
        return RequestContainer(self, dict(self._request_factories))


class RequestContainer:
    """请求级容器：请求级实例惰性创建，随请求结束释放。"""

    def __init__(
        self, app_container: Container, factories: dict[str, object] | None = None
    ) -> None:
        self._app = app_container
        self._request_instances: dict[str, object] = {}
        self._factories: dict[str, object] = dict(factories or {})
        self._lock = asyncio.Lock()
        self._closed = False

    def get_app_instance(self, name: str) -> object:
        """只读访问应用级单例。"""
        return self._app.get_app_instance(name)

    async def get_request_instance(self, name: str) -> object:
        if name in self._request_instances:
            return self._request_instances[name]
        async with self._lock:
            # 双检：并发协程只创建一次
            if name in self._request_instances:
                return self._request_instances[name]
            if self._closed:
                raise RuntimeError("请求容器已关闭，不能再创建实例")
            factory = self._factories.get(name)
            if factory is None:
                raise KeyError(f"未注册的请求级插件: {name}")
            instance = await factory(self)
            self._request_instances[name] = instance
            return instance

    def has_request_instance(self, name: str) -> bool:
        return name in self._request_instances

    async def aclose(self) -> None:
        """释放本请求内创建的实例（具备 aclose/close 时调用）。"""
        async with self._lock:
            for instance in self._request_instances.values():
                aclose = getattr(instance, "aclose", None)
                if callable(aclose):
                    await aclose()
                else:
                    close = getattr(instance, "close", None)
                    if callable(close):
                        res = close()
                        if asyncio.iscoroutine(res):
                            await res
            self._request_instances.clear()
            self._closed = True
