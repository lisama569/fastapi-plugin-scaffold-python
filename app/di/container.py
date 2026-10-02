"""依赖注入容器：区分应用级与请求级作用域。

* 应用级插件在装配时创建一次，FastAPI lifespan 内全局共享，受锁保护，
  并发场景下绝不会被重复初始化；
* 请求级插件只在请求作用域内惰性创建，实例仅存在于该请求作用域，
  请求结束即逆序释放，不跨请求泄漏；
* 已释放容器/作用域上的任何取用都抛 ``ContainerDisposedError``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..plugins.contract import PluginContext, PluginSpec


class ContainerDisposedError(RuntimeError):
    """容器或请求作用域已释放后仍被访问。"""


class Container:
    """应用级容器：持有应用级单例，并登记请求级插件的创建方式。"""

    def __init__(self) -> None:
        self._instances: dict[str, Any] = {}
        self._by_name: dict[str, Any] = {}
        self._request_specs: dict[str, PluginSpec] = {}
        self._request_options: dict[str, dict[str, Any]] = {}
        self._disposed = False
        self._lock = asyncio.Lock()
        # 热更新退役/排空：退役后不再派生新请求作用域，
        # 在途作用域归零时触发回调（由监督器负责最终释放）。
        self._active_scopes = 0
        self._retired = False
        self._drain_callbacks: list[Callable[[], None]] = []

    def register_instance(self, name: str, interface: str | None, instance: Any) -> None:
        """登记一个已装配完成的应用级实例。

        interface=None 时只按插件名登记；否则同时按接口名登记，by_name 不覆盖。
        """
        if self._disposed:
            raise ContainerDisposedError(f"容器已释放，无法登记 {name!r}")
        if interface is None:
            self._by_name[name] = instance
        else:
            self._instances[interface] = instance
            self._by_name.setdefault(name, instance)

    def register_request_spec(
        self, spec: PluginSpec, options: dict[str, Any] | None = None
    ) -> None:
        """登记请求级插件规格与其配置，供请求作用域惰性创建。"""
        if self._disposed:
            raise ContainerDisposedError(f"容器已释放，无法登记请求级规格 {spec.manifest.name!r}")
        opts = options or {}
        for interface in spec.manifest.provides:
            self._request_specs[interface] = spec
            self._request_options[interface] = opts
        self._request_specs[spec.manifest.name] = spec
        self._request_options[spec.manifest.name] = opts

    def request_options(self, interface: str) -> dict[str, Any]:
        return self._request_options.get(interface, {})

    def is_request_interface(self, interface: str) -> bool:
        return interface in self._request_specs

    def request_spec(self, interface: str) -> PluginSpec:
        return self._request_specs[interface]

    def get(self, interface: str) -> Any:
        """按接口名取应用级单例。"""
        if self._disposed:
            raise ContainerDisposedError(f"容器已释放，无法获取 {interface!r}")
        try:
            return self._instances[interface]
        except KeyError:
            raise KeyError(f"应用级容器中不存在接口 {interface!r}") from None

    def get_by_name(self, name: str) -> Any:
        if self._disposed:
            raise ContainerDisposedError(f"容器已释放，无法获取 {name!r}")
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"应用级容器中不存在插件 {name!r}") from None

    def has(self, interface: str) -> bool:
        return interface in self._instances

    def lock(self) -> asyncio.Lock:
        """装配期使用的初始化锁（并发装配保护）。"""
        return self._lock

    def new_request_scope(self) -> RequestScope:
        if self._disposed:
            raise ContainerDisposedError("容器已释放，无法创建请求作用域")
        if self._retired:
            raise ContainerDisposedError("容器已退役（热更新切换中），无法创建请求作用域")
        self._active_scopes += 1
        return RequestScope(self)

    def retire(self, on_drained: Callable[[], None] | None = None) -> None:
        """标记退役：不再接受新请求作用域；在途作用域归零后触发回调。

        回调幂等（只触发一次）；若当前无在途作用域则立即触发。
        """
        if self._retired:
            return
        self._retired = True
        if on_drained is not None:
            self._drain_callbacks.append(on_drained)
        if self._active_scopes == 0:
            self._fire_drained()

    def _scope_closed(self) -> None:
        """请求作用域释放时回调；退役且归零则触发排空回调。"""
        self._active_scopes -= 1
        if self._retired and self._active_scopes == 0:
            self._fire_drained()

    def _fire_drained(self) -> None:
        callbacks, self._drain_callbacks = self._drain_callbacks, []
        for cb in callbacks:
            cb()

    @property
    def retired(self) -> bool:
        return self._retired

    @property
    def active_scopes(self) -> int:
        return self._active_scopes

    async def dispose(self) -> None:
        """释放容器（幂等）。"""
        if self._disposed:
            return
        self._disposed = True
        self._instances.clear()
        self._by_name.clear()
        self._request_specs.clear()
        self._request_options.clear()

    @property
    def disposed(self) -> bool:
        return self._disposed


class RequestScope:
    """单次请求作用域：请求级实例惰性创建、互相隔离、逆序释放。"""

    def __init__(self, container: Container) -> None:
        self._container = container
        self._instances: dict[str, Any] = {}
        self._creation_order: list[str] = []
        self._disposed = False

    def _resolve_dependency(self, interface: str) -> Any:
        """应用级依赖取容器单例；请求级依赖在本作用域内惰性创建。"""
        if self._container.has(interface):
            return self._container.get(interface)
        if self._container.is_request_interface(interface):
            return self._build(self._container.request_spec(interface))
        raise KeyError(f"无法解析接口 {interface!r}: 无提供者")

    def _build(self, spec: PluginSpec) -> Any:
        """在当前作用域创建一个请求级实例（同作用域内只创建一次）。"""
        canonical = spec.manifest.provides[0] if spec.manifest.provides else spec.manifest.name
        if canonical in self._instances:
            return self._instances[canonical]
        deps = {
            req.interface: self._resolve_dependency(req.interface) for req in spec.manifest.requires
        }
        options = self._container.request_options(canonical)
        instance = spec.factory(PluginContext(options=options, dependencies=deps))
        self._instances[canonical] = instance
        for interface in spec.manifest.provides:
            self._instances[interface] = instance
        self._creation_order.append(canonical)
        return instance

    def get(self, interface: str) -> Any:
        """取实例；应用级接口回退到容器单例，请求级接口在本作用域创建。"""
        if self._disposed:
            raise ContainerDisposedError(f"请求作用域已释放，无法获取 {interface!r}")
        if self._container.has(interface):
            return self._container.get(interface)
        if self._container.is_request_interface(interface):
            return self._build(self._container.request_spec(interface))
        raise KeyError(f"无法解析接口 {interface!r}")

    async def dispose(self) -> None:
        """释放本请求创建的全部实例（逆序），幂等。"""
        if self._disposed:
            return
        self._disposed = True
        try:
            for canonical in reversed(self._creation_order):
                instance = self._instances.get(canonical)
                on_stop = getattr(instance, "on_request_stop", None)
                if callable(on_stop):
                    result = on_stop()
                    if asyncio.iscoroutine(result):
                        await result
        finally:
            self._instances.clear()
            self._creation_order.clear()
            self._container._scope_closed()

    @property
    def disposed(self) -> bool:
        return self._disposed
