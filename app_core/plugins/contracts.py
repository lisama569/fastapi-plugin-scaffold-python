"""插件契约：元数据、版本要求、作用域与结构协议。

兼容性规则（启动阶段由 assembler 强制执行）：

- **接口级别**：每个插件声明它遵循的 ``api_level``；宿主支持
  ``[MIN_SUPPORTED_API_LEVEL, CURRENT_API_LEVEL]`` 闭区间，超范围即拒绝。
- **依赖版本**：:class:`Requirement` 声明依赖插件的最低版本（含），
  被依赖方版本低于下限即拒绝。
- **作用域**：``Scope.APP`` 的实例在装配时创建一次、全局共享；
  ``Scope.REQUEST`` 的实例按请求惰性创建、请求结束即释放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app_core.plugins.container import RequestContainer, Scope

#: 当前宿主支持的插件契约接口级别。
CURRENT_API_LEVEL = 1
#: 宿主仍向后兼容的最低接口级别。
MIN_SUPPORTED_API_LEVEL = 1


def parse_version(text: str) -> tuple[int, ...]:
    """把 ``"1.2.3"`` 解析为可比较的整数元组；非法格式抛 ValueError。"""
    parts = text.strip().split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(f"非法版本号: {text!r}")
    return tuple(int(part) for part in parts)


def compare_versions(a: str, b: str) -> int:
    """语义化三段版本比较：a<b 返回 -1，相等 0，a>b 返回 1。"""
    ta, tb = parse_version(a), parse_version(b)
    width = max(len(ta), len(tb))
    ta += (0,) * (width - len(ta))
    tb += (0,) * (width - len(tb))
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


@dataclass(frozen=True)
class Requirement:
    """对另一个插件的依赖声明：名称 + 最低兼容版本（含）。"""

    name: str
    min_version: str = "0.0.0"


@dataclass(frozen=True)
class PluginMetadata:
    name: str
    version: str = "0.0.0"
    requires: tuple[Requirement, ...] = ()
    scope: Scope = Scope.APP
    api_level: int = CURRENT_API_LEVEL


@runtime_checkable
class Plugin(Protocol):
    """插件的结构化协议。

    生命周期方法由装配器按解析顺序调用：``configure``（同步，注入私有
    配置）→ ``startup``（异步，创建实例）→ ``shutdown``（异步，逆序）。
    """

    metadata: PluginMetadata

    def configure(self, config: dict[str, object]) -> None: ...

    async def startup(self, ctx: Any) -> object | None: ...

    async def shutdown(self, ctx: Any) -> None: ...


@runtime_checkable
class RequestScopedPlugin(Plugin, Protocol):
    """请求级插件需要额外提供按请求创建实例的方法。"""

    async def create_request_instance(self, rc: RequestContainer) -> object: ...


class BasePlugin:
    """便利基类：提供生命周期与路由注册的默认空实现。

    子类只需设置类属性 :attr:`metadata` 并按需覆写方法。
    """

    metadata: PluginMetadata

    def configure(self, config: dict[str, object]) -> None:
        self.config = dict(config)

    async def startup(self, ctx: Any) -> object | None:
        return None

    async def shutdown(self, ctx: Any) -> None:
        return None

    def register_routes(self, router: Any) -> None:  # pragma: no cover - 可选钩子
        return None
