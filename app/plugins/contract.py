"""插件契约：元数据、装配上下文与插件规格。

兼容规则（在启动阶段判定，任一不满足即拒绝装配）：

* 每个接口名全应用内只能有一个提供者，重复提供按 DuplicatePluginError 拒绝；
* 消费者声明的版本要求采用简单 semver 区间，"^x.y" 表示同一主版本且不低于，
  不满足按 VersionIncompatibleError 拒绝；
* 依赖缺失按 PluginNotFoundError 拒绝；
* 依赖图成环按 CircularDependencyError 拒绝。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Scope = Literal["app", "request"]


@dataclass(frozen=True)
class InterfaceRequirement:
    """对另一个插件提供接口的依赖声明。"""

    interface: str
    version: str


@dataclass(frozen=True)
class PluginManifest:
    """插件静态元数据。"""

    name: str
    version: str
    provides: tuple[str, ...]
    requires: tuple[InterfaceRequirement, ...] = ()
    scope: Scope = "app"
    description: str = ""


@dataclass(frozen=True)
class PluginContext:
    """传给插件工厂的装配上下文：依赖实例与该插件的配置选项。"""

    options: dict[str, Any]
    dependencies: dict[str, Any] = field(default_factory=dict)


PluginFactory = Callable[[PluginContext], Any]
PluginLifecycleHook = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class PluginSpec:
    """一个可装配插件的完整规格：元数据 + 工厂 + 生命周期钩子。"""

    manifest: PluginManifest
    factory: PluginFactory
    on_start: PluginLifecycleHook | None = None
    on_stop: PluginLifecycleHook | None = None
    validate_options: Callable[[dict[str, Any]], None] | None = None
