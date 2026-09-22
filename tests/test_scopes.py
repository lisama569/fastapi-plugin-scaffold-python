"""生命周期与作用域：应用级单例共享、请求级隔离、并发安全、释放保护。"""

from __future__ import annotations

import asyncio

import pytest

from app.assembly.assembler import Assembler
from app.di.container import ContainerDisposedError
from app.plugins.contract import InterfaceRequirement
from app.plugins.registry import PluginRegistry

from .conftest import make_config, make_spec


async def _assemble(specs, config):
    reg = PluginRegistry()
    for spec in specs:
        reg.register(spec)
    return await Assembler(reg, config).assemble()


async def test_app_singleton_shared_across_concurrent_requests() -> None:
    """并发 N 个请求，拿到的应用级实例必须是同一个，且只初始化一次。"""
    seen: list[object] = []

    def hold(ctx):
        obj = object()
        seen.append(obj)
        return obj

    clock = make_spec("clock", instance_factory=hold)
    config = make_config({"clock": (True, "app")})
    container, _ = await _assemble([clock], config)

    assert len(seen) == 1  # 装配期只创建一次

    async def one_request() -> object:
        scope = container.new_request_scope()
        try:
            return scope.get("clock")
        finally:
            await scope.dispose()

    results = await asyncio.gather(*(one_request() for _ in range(50)))
    assert len({id(x) for x in results}) == 1
    assert results[0] is seen[0]
    assert len(seen) == 1  # 并发请求不重复初始化
    await container.dispose()


async def test_request_instances_isolated_per_request() -> None:
    """请求级插件：不同请求拿到不同实例，同请求内多次取用是同一实例。"""
    reqsvc = make_spec("reqsvc", scope="request")
    config = make_config({"reqsvc": (True, "request")})
    container, _ = await _assemble([reqsvc], config)

    s1 = container.new_request_scope()
    s2 = container.new_request_scope()
    i1a = s1.get("reqsvc")
    i1b = s1.get("reqsvc")
    i2 = s2.get("reqsvc")
    assert i1a is i1b  # 同请求惰性单例
    assert i1a is not i2  # 跨请求隔离
    await s1.dispose()
    await s2.dispose()


async def test_request_plugin_can_consume_app_singleton() -> None:
    clock = make_spec("clock", scope="app")
    greeter = make_spec(
        "greeter",
        scope="request",
        requires=(InterfaceRequirement("clock", "^1.0.0"),),
    )
    config = make_config({"clock": (True, "app"), "greeter": (True, "request")})
    container, report = await _assemble([clock, greeter], config)

    assert report.assembled == ["clock"]
    assert report.request_registered == ["greeter"]

    s1 = container.new_request_scope()
    s2 = container.new_request_scope()
    g1 = s1.get("greeter")
    g2 = s2.get("greeter")
    assert g1 is not g2  # greeter 请求隔离
    assert g1["deps"]["clock"] is g2["deps"]["clock"]  # clock 共享单例
    await s1.dispose()
    await s2.dispose()
    await container.dispose()


async def test_concurrent_requests_get_independent_instances() -> None:
    reqsvc = make_spec("reqsvc", scope="request")
    config = make_config({"reqsvc": (True, "request")})
    container, _ = await _assemble([reqsvc], config)

    instances: list[object] = []

    async def one_request() -> None:
        scope = container.new_request_scope()
        try:
            await asyncio.sleep(0)  # 制造交错
            instances.append(scope.get("reqsvc"))
        finally:
            await scope.dispose()

    await asyncio.gather(*(one_request() for _ in range(30)))
    # 全部实例仍被列表持有，身份必须两两不同
    assert len({id(x) for x in instances}) == 30  # 无跨请求泄漏
    await container.dispose()


async def test_disposed_scope_and_container_reject_access() -> None:
    clock = make_spec("clock", scope="app")
    config = make_config({"clock": (True, "app")})
    container, _ = await _assemble([clock], config)

    scope = container.new_request_scope()
    assert scope.get("clock") is container.get("clock")
    await scope.dispose()
    with pytest.raises(ContainerDisposedError):
        scope.get("clock")

    await container.dispose()
    with pytest.raises(ContainerDisposedError):
        container.get("clock")
    with pytest.raises(ContainerDisposedError):
        container.new_request_scope()


async def test_dispose_is_idempotent() -> None:
    clock = make_spec("clock", scope="app")
    config = make_config({"clock": (True, "app")})
    container, _ = await _assemble([clock], config)
    await container.dispose()
    await container.dispose()  # 不抛异常
    assert container.disposed is True
