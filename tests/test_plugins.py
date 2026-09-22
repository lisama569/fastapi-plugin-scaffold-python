"""插件注册/发现、重复、循环依赖与版本兼容的启动期拒绝。"""

from __future__ import annotations

import pytest

from app.plugins.contract import InterfaceRequirement, PluginSpec
from app.plugins.discovery import discover_plugins
from app.plugins.errors import (
    CircularDependencyError,
    DuplicatePluginError,
    PluginError,
    PluginNotFoundError,
    VersionIncompatibleError,
)
from app.plugins.registry import PluginRegistry

from .conftest import make_spec


def _registry(*specs: PluginSpec) -> PluginRegistry:
    reg = PluginRegistry()
    for spec in specs:
        reg.register(spec)
    return reg


def test_duplicate_plugin_name_rejected() -> None:
    reg = PluginRegistry()
    reg.register(make_spec("svc"))
    with pytest.raises(DuplicatePluginError) as exc:
        reg.register(make_spec("svc"))
    assert "svc" in str(exc.value)


def test_duplicate_interface_provider_rejected() -> None:
    reg = PluginRegistry()
    reg.register(make_spec("a", provides=("api",)))
    # 同一接口被第二个插件提供时，注册当下即拒绝，不进入装配阶段
    with pytest.raises(DuplicatePluginError) as exc:
        reg.register(make_spec("b", provides=("api",)))
    assert "api" in str(exc.value)


def test_missing_dependency_rejected() -> None:
    reg = _registry(make_spec("a", requires=(InterfaceRequirement("ghost", "^1.0.0"),)))
    with pytest.raises(PluginNotFoundError) as exc:
        reg.validate_graph()
    assert exc.value.plugin == "ghost"


def test_circular_dependency_rejected_with_chain() -> None:
    reg = _registry(
        make_spec("a", requires=(InterfaceRequirement("b", "^1.0.0"),)),
        make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),)),
    )
    with pytest.raises(CircularDependencyError) as exc:
        reg.validate_graph()
    # 环中起点 == 终点，且包含两个节点
    assert exc.value.cycle[0] == exc.value.cycle[-1]
    assert set(exc.value.cycle) == {"a", "b"}


def test_self_dependency_rejected() -> None:
    reg = _registry(
        make_spec("loop", provides=("loop",), requires=(InterfaceRequirement("loop", "^1.0.0"),))
    )
    with pytest.raises(CircularDependencyError):
        reg.validate_graph()


def test_version_incompatible_rejected() -> None:
    reg = _registry(
        make_spec("base", version="2.1.0"),
        make_spec("consumer", requires=(InterfaceRequirement("base", "^1.0.0"),)),
    )
    with pytest.raises(VersionIncompatibleError) as exc:
        reg.validate_graph()
    assert exc.value.consumer == "consumer"
    assert exc.value.provider_version == "2.1.0"


def test_version_compatible_range_accepted() -> None:
    reg = _registry(
        make_spec("base", version="1.9.4"),
        make_spec("consumer", requires=(InterfaceRequirement("base", "^1.2.0"),)),
    )
    reg.validate_graph()  # 不抛异常即可


def test_topological_order_is_deterministic() -> None:
    reg = PluginRegistry()
    # 故意打乱注册顺序
    for name in ("d", "b", "c", "a"):
        reg.register(make_spec(name))
    reg.register(
        make_spec(
            "e",
            requires=(
                InterfaceRequirement("a", "^1.0.0"),
                InterfaceRequirement("d", "^1.0.0"),
            ),
        )
    )
    order = [s.manifest.name for s in reg.resolve_order()]
    assert order.index("a") < order.index("e")
    assert order.index("d") < order.index("e")
    # 无依赖的节点严格按注册顺序
    assert order[:4] == ["d", "b", "c", "a"]


def test_discovery_from_module() -> None:
    specs = discover_plugins("app.sample_plugins")
    assert {s.manifest.name for s in specs} == {"clock", "greeter"}


def test_discovery_missing_module_rejected() -> None:
    with pytest.raises(PluginError):
        discover_plugins("app.sample_plugins.not_here")


def test_discovery_bad_plugins_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    module = types.ModuleType("tmp_bad_plugins")
    module.PLUGINS = "not-an-iterable"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tmp_bad_plugins", module)
    with pytest.raises(PluginError):
        discover_plugins("tmp_bad_plugins")
