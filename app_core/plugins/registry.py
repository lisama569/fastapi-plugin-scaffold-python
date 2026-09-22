"""插件注册表：支持手动注册与包内自动发现。

- :meth:`PluginRegistry.register` 手动注册，重复名立即抛
  :class:`DuplicatePluginError`，绝不静默覆盖。
- :meth:`PluginRegistry.discover` 遍历给定包（默认 :mod:`plugins_example`）
  下所有模块，找出其中的所有非抽象 :class:`~app_core.plugins.contracts.Plugin`
  实例（包含 ``PLUGINS`` 序列与模块级单例属性两种导出形式）。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Iterable

from app_core.errors import DuplicatePluginError, PluginNotFoundError
from app_core.plugins.contracts import Plugin

_DEFAULT_DISCOVER_PACKAGE = "plugins_example"


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._sources: dict[str, str] = {}

    # ------------------------------------------------------------------
    def register(self, plugin: Plugin, *, source: str = "manual") -> None:
        name = plugin.metadata.name
        if name in self._plugins:
            raise DuplicatePluginError(name, source)
        self._plugins[name] = plugin
        self._sources[name] = source

    def register_many(self, plugins: Iterable[Plugin]) -> None:
        for plugin in plugins:
            self.register(plugin)

    # ------------------------------------------------------------------
    def discover(self, package_name: str = _DEFAULT_DISCOVER_PACKAGE) -> int:
        """导入包及所有子模块并注册其中的插件实例。

        模块可以通过两种方式导出：

        1. 定义模块级列表/元组 ``PLUGINS = [PluginA(), PluginB()]``；
        2. 直接把插件实例赋给模块级变量（实例需有 ``metadata``）。

        :returns: 本次新注册的插件数量。
        """
        package = importlib.import_module(package_name)
        modules: list[object] = []
        if getattr(package, "__path__", None) is not None:
            for module_info in pkgutil.walk_packages(
                package.__path__, prefix=package_name + "."
            ):
                modules.append(importlib.import_module(module_info.name))
        else:
            modules.append(package)

        before = len(self._plugins)
        seen_ids: set[int] = set()
        for module in modules:
            # 约定：模块显式导出 PLUGINS 序列；否则回退为扫描本模块定义的
            # 插件实例成员。两种方式互斥，避免同一插件被重复登记。
            exported = getattr(module, "PLUGINS", None)
            if isinstance(exported, (list, tuple)):
                candidates = list(exported)
            else:
                candidates = [
                    member
                    for _, member in inspect.getmembers(module)
                    if getattr(member, "__module__", None) == module.__name__
                    and hasattr(member, "metadata")
                    and hasattr(member, "startup")
                    and hasattr(member, "configure")
                ]
            for candidate in candidates:
                if id(candidate) in seen_ids:
                    continue
                seen_ids.add(id(candidate))
                self.register(candidate, source=f"discover:{module.__name__}")
        return len(self._plugins) - before

    # ------------------------------------------------------------------
    def get(self, name: str) -> Plugin:
        if name not in self._plugins:
            raise PluginNotFoundError(name)
        return self._plugins[name]

    def all(self) -> list[Plugin]:
        return [self._plugins[name] for name in sorted(self._plugins)]

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def source_of(self, name: str) -> str:
        return self._sources[name]

    def __contains__(self, name: object) -> bool:
        return name in self._plugins
