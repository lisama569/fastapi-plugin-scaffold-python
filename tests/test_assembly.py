"""插件装配：正常拓扑、重复/循环/缺失/不兼容、中途失败回滚、发现机制。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app_core.errors import (
    AssemblyError,
    CircularDependencyError,
    DuplicatePluginError,
    IncompatiblePluginError,
    PluginNotFoundError,
)
from app_core.plugins.assembler import Assembler
from app_core.plugins.container import Container
from app_core.plugins.contracts import Requirement
from app_core.plugins.registry import PluginRegistry
from plugins_example.basic import CounterPlugin, SessionPlugin

logger = logging.getLogger("test.assembly")


def test_normal_topological_assembly(make_registry, make_settings, assemble):
    registry = make_registry([
        {"name": "echo", "requires": [Requirement("counter", "1.0.0")]},
        {"name": "counter", "version": "1.1.0"},
    ])
    settings = make_settings(["echo", "counter"])

    container, _assembler, order = asyncio.run(assemble(registry, settings))

    # 依赖必须先于依赖方启动；装配顺序确定性、稳定
    assert order == ["counter", "echo"]
    assert container.get_app_instance("counter") == "counter-instance"
    assert container.get_app_instance("echo") == "echo-instance"
    logger.info("装配顺序: %s", order)


def test_discovery_registers_example_plugins():
    registry = PluginRegistry()
    count = registry.discover("plugins_example")
    assert count >= 3
    assert {"counter", "echo", "session"} <= set(registry.names())
    assert registry.source_of("counter").startswith("discover:")


def test_discovery_then_duplicate_manual_register_is_rejected():
    registry = PluginRegistry()
    registry.discover("plugins_example")
    with pytest.raises(DuplicatePluginError) as exc:
        registry.register(CounterPlugin())
    assert exc.value.name == "counter"


def test_duplicate_registration_with_same_name_is_rejected(make_registry):
    with pytest.raises(DuplicatePluginError) as exc:
        make_registry([{"name": "dup"}, {"name": "dup"}])
    assert exc.value.name == "dup"
    assert "重复注册" in str(exc.value)


def test_enabled_plugin_not_registered(make_registry, make_settings):
    registry = make_registry([{"name": "counter"}])
    settings = make_settings(["ghost"])
    with pytest.raises(PluginNotFoundError) as exc:
        Assembler(registry, settings).resolve_order()
    assert exc.value.name == "ghost"


def test_missing_dependency_reports_referencer(make_registry, make_settings):
    registry = make_registry([
        {"name": "echo", "requires": [Requirement("missing", "1.0.0")]},
    ])
    settings = make_settings(["echo"])
    with pytest.raises(PluginNotFoundError) as exc:
        Assembler(registry, settings).resolve_order()
    assert exc.value.name == "missing"
    assert exc.value.referenced_by == "echo"


def test_circular_dependency_reports_cycle_path(make_registry, make_settings):
    registry = make_registry([
        {"name": "a", "requires": [Requirement("b")]},
        {"name": "b", "requires": [Requirement("c")]},
        {"name": "c", "requires": [Requirement("a")]},
    ])
    settings = make_settings(["a"])
    with pytest.raises(CircularDependencyError) as exc:
        Assembler(registry, settings).resolve_order()
    cycle = exc.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"a", "b", "c"}
    logger.info("循环依赖失败链路: %s", cycle)


def test_unsupported_api_level_is_rejected(make_registry, make_settings):
    registry = make_registry([{"name": "future", "api_level": 99}])
    settings = make_settings(["future"])
    with pytest.raises(IncompatiblePluginError) as exc:
        Assembler(registry, settings).resolve_order()
    assert exc.value.plugin == "future"
    assert "api_level" in exc.value.reason


def test_dependency_version_below_required_is_rejected(make_registry, make_settings):
    registry = make_registry([
        {"name": "counter", "version": "0.9.0"},
        {"name": "echo", "requires": [Requirement("counter", "1.0.0")]},
    ])
    settings = make_settings(["echo"])
    with pytest.raises(IncompatiblePluginError) as exc:
        Assembler(registry, settings).resolve_order()
    assert exc.value.plugin == "echo"
    assert "counter" in exc.value.reason


def test_assembly_failure_rolls_back_started_plugins(make_registry, make_settings):
    registry = make_registry([
        {"name": "counter"},
        {"name": "failing",
         "requires": [Requirement("counter")],
         "startup_error": RuntimeError("boom")},
    ])
    settings = make_settings(["failing"])
    assembler = Assembler(registry, settings)
    container = Container()

    with pytest.raises(AssemblyError) as exc:
        asyncio.run(assembler.assemble(container))

    err = exc.value
    assert err.plugin == "failing"
    assert err.chain == ["counter", "failing"]   # 可定位的失败链路
    assert err.rolled_back == ["counter"]        # 逆序回滚已启动部分
    assert isinstance(err.cause, RuntimeError)
    assert not container.has("counter")          # 实例已随失败撤销
    assert registry.get("counter").shutdown_called is True
    logger.info("失败链路: %s; 已回滚: %s", err.chain, err.rolled_back)


def test_rollback_continues_when_shutdown_also_fails(make_registry, make_settings):
    class _ExplodingOnShutdown(make_registry([{"name": "x"}]).get("x").__class__):
        async def shutdown(self, ctx) -> None:
            raise RuntimeError("shutdown also failed")

    x = _ExplodingOnShutdown("x")
    y_plugin = make_registry([
        {"name": "failing", "startup_error": RuntimeError("boom"),
         "requires": [Requirement("x")]},
    ]).get("failing")
    registry = PluginRegistry()
    registry.register(x)
    registry.register(y_plugin)
    settings = make_settings(["failing"])

    with pytest.raises(AssemblyError) as exc:
        asyncio.run(Assembler(registry, settings).assemble(Container()))
    # 即使回滚出错，原始装配失败原因仍然保留、可区分
    assert exc.value.plugin == "failing"
    assert isinstance(exc.value.cause, RuntimeError)


def test_app_singleton_used_once_under_concurrent_requests(assemble, make_settings):
    registry = PluginRegistry()
    registry.register(CounterPlugin())
    settings = make_settings(["counter"], {"counter": {"start": 10}})
    container, _a, order = asyncio.run(assemble(registry, settings))
    assert order == ["counter"]

    seen: list[object] = []

    async def worker() -> None:
        seen.append(container.get_app_instance("counter"))

    async def scenario() -> None:
        await asyncio.gather(*(worker() for _ in range(50)))

    asyncio.run(scenario())
    assert len(seen) == 50
    assert len({id(obj) for obj in seen}) == 1       # 全部拿到同一个实例
    counter = container.get_app_instance("counter")
    assert counter.init_count == 1                   # 只初始化一次
    assert counter.value == 10


def test_request_scoped_instances_do_not_leak_across_requests(make_settings):
    registry = PluginRegistry()
    registry.register(CounterPlugin())
    registry.register(SessionPlugin())
    settings = make_settings(["counter", "session"])
    assembler = Assembler(registry, settings)
    container = Container()
    asyncio.run(assembler.assemble(container))

    async def one_request() -> tuple[int, object, bool]:
        rc = container.build_request_container()
        counter = rc.get_app_instance("counter")      # 请求可读应用级单例
        s1 = await rc.get_request_instance("session")
        s2 = await rc.get_request_instance("session")
        assert s1 is s2                               # 请求内一致
        await rc.aclose()
        return id(counter), s1, s1.closed             # type: ignore[attr-defined]

    async def scenario() -> list[tuple[int, object, bool]]:
        return await asyncio.gather(*(one_request() for _ in range(20)))

    results = asyncio.run(scenario())
    counter_ids = {row[0] for row in results}
    session_ids = {id(row[1]) for row in results}
    assert len(counter_ids) == 1                      # 共享同一个应用级单例
    assert len(session_ids) == 20                     # 每请求独立 session
    assert all(row[2] is True for row in results)     # 结束即释放
