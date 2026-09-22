"""装配编排：成功装配、禁用过滤、选项拒绝、中途失败回滚与失败链路。"""

from __future__ import annotations

import pytest

from app.assembly.assembler import Assembler
from app.plugins.contract import InterfaceRequirement
from app.plugins.errors import AssemblyError
from app.plugins.registry import PluginRegistry

from .conftest import make_config, make_spec


async def _assemble(specs, config):
    reg = PluginRegistry()
    for spec in specs:
        reg.register(spec)
    return await Assembler(reg, config).assemble()


async def test_assembly_order_and_dependency_injection() -> None:
    events: list[str] = []
    a = make_spec("a", track=events)
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),), track=events)
    c = make_spec("c", requires=(InterfaceRequirement("b", "^1.0.0"),), track=events)
    # 乱序注册
    config = make_config({"a": (True, "app"), "b": (True, "app"), "c": (True, "app")})
    container, report = await _assemble([c, a, b], config)

    assert report.ok is True
    assert report.assembled == ["a", "b", "c"]
    assert events == ["create:a", "start:a", "create:b", "start:b", "create:c", "start:c"]
    # 依赖被真实注入
    assert container.get("c")["deps"]["b"] is container.get("b")
    assert container.get("b")["deps"]["a"] is container.get("a")


async def test_disabled_plugin_skipped() -> None:
    a = make_spec("a")
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),))
    config = make_config({"a": (True, "app"), "b": (False, "app")})
    container, report = await _assemble([a, b], config)
    assert report.assembled == ["a"]
    assert not container.has("b")


async def test_factory_failure_rolls_back_in_reverse(caplog) -> None:
    events: list[str] = []
    a = make_spec("a", track=events)
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),), track=events)
    c = make_spec(
        "c",
        requires=(InterfaceRequirement("b", "^1.0.0"),),
        fail_factory=True,
        fail_message="c 工厂爆炸",
        track=events,
    )
    config = make_config({"a": (True, "app"), "b": (True, "app"), "c": (True, "app")})
    with pytest.raises(AssemblyError) as exc:
        await _assemble([c, b, a], config)

    assert exc.value.plugin == "c"
    assert exc.value.reason == "c 工厂爆炸"
    assert exc.value.chain == ["a", "b", "c"]
    assert exc.value.assembled == ["a", "b"]
    # a,b 已启动；c 工厂在记录 create 前即失败；按 b -> a 逆序回滚
    assert events == [
        "create:a",
        "start:a",
        "create:b",
        "start:b",
        "stop:b",
        "stop:a",
    ]
    # 失败链路与回滚信息进入日志
    log_text = "\n".join(r.message for r in caplog.records)
    assert "失败链路: a -> b -> c" in log_text
    assert "['b', 'a']" in log_text


async def test_start_hook_failure_rolls_back() -> None:
    events: list[str] = []
    a = make_spec("a", track=events)
    b = make_spec(
        "b",
        requires=(InterfaceRequirement("a", "^1.0.0"),),
        start_fail=True,
        fail_message="b 起不来",
        track=events,
    )
    config = make_config({"a": (True, "app"), "b": (True, "app")})
    with pytest.raises(AssemblyError) as exc:
        await _assemble([b, a], config)
    assert exc.value.plugin == "b"
    assert events == ["create:a", "start:a", "create:b", "stop:a"]
    assert exc.value.rolled_back  # AssemblyError 自身没有该字段，检查链路
    assert exc.value.chain == ["a", "b"]


async def test_invalid_options_rejected_before_instantiation() -> None:
    def reject(options):
        raise ValueError("greeting 不能为空")

    from app.plugins.contract import PluginSpec

    spec = make_spec("g")
    bad = PluginSpec(
        manifest=spec.manifest,
        factory=spec.factory,
        on_start=spec.on_start,
        on_stop=spec.on_stop,
        validate_options=reject,
    )
    config = make_config({"g": (True, "app")}, options={"g": {"greeting": ""}})
    with pytest.raises(AssemblyError) as exc:
        await _assemble([bad], config)
    assert "greeting 不能为空" in exc.value.reason
    # 一个都没装成
    assert exc.value.assembled == []


async def test_app_scoped_plugin_depending_on_request_scoped_rejected() -> None:
    app_spec = make_spec(
        "appsvc",
        requires=(InterfaceRequirement("reqsvc", "^1.0.0"),),
        scope="app",
    )
    req_spec = make_spec("reqsvc", scope="request")
    config = make_config({"appsvc": (True, "app"), "reqsvc": (True, "request")})
    with pytest.raises(AssemblyError) as exc:
        await _assemble([app_spec, req_spec], config)
    assert "应用级插件不能依赖请求级插件" in exc.value.reason


async def test_scope_mismatch_config_vs_manifest_rejected() -> None:
    a = make_spec("a", scope="app")
    # 清单是 app，配置却写成 request
    config = make_config({"a": (True, "request")})
    with pytest.raises(AssemblyError) as exc:
        await _assemble([a], config)
    assert "作用域" in exc.value.reason


async def test_failure_chain_points_to_deep_dependency() -> None:
    # 依赖方向 d -> c -> b -> a；a（最底层）工厂失败
    a = make_spec("a", fail_factory=True, fail_message="a boom")
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),))
    c = make_spec("c", requires=(InterfaceRequirement("b", "^1.0.0"),))
    d = make_spec("d", requires=(InterfaceRequirement("c", "^1.0.0"),))
    config = make_config({n: (True, "app") for n in ("a", "b", "c", "d")})
    with pytest.raises(AssemblyError) as exc:
        # 装配在拓扑序第一个节点 a 处即失败
        await _assemble([d, c, b, a], config)
    assert exc.value.plugin == "a"
    assert exc.value.chain == ["a"]
    assert exc.value.assembled == []  # a 在最底层，失败时无人完成启动


async def test_failure_chain_when_middle_plugin_fails() -> None:
    # 依赖方向 d -> c -> b -> a；c（中间层）工厂失败，链路含其全部依赖与自身
    a = make_spec("a")
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),))
    c = make_spec(
        "c",
        requires=(InterfaceRequirement("b", "^1.0.0"),),
        fail_factory=True,
        fail_message="c boom",
    )
    d = make_spec("d", requires=(InterfaceRequirement("c", "^1.0.0"),))
    config = make_config({n: (True, "app") for n in ("a", "b", "c", "d")})
    with pytest.raises(AssemblyError) as exc:
        await _assemble([d, c, b, a], config)
    assert exc.value.plugin == "c"
    assert exc.value.chain == ["a", "b", "c"]
    assert exc.value.assembled == ["a", "b"]
    assert exc.value.rolled_back == ["b", "a"]


async def test_enabled_plugin_depending_on_disabled_plugin_rejected() -> None:
    a = make_spec("a")
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),))
    # a 被禁用，b 仍启用 -> 明确拒绝，而不是装配期 KeyError
    config = make_config({"a": (False, "app"), "b": (True, "app")})
    with pytest.raises(AssemblyError) as exc:
        await _assemble([a, b], config)
    assert exc.value.plugin == "b"
    assert "a" in exc.value.reason
    assert exc.value.chain == ["a", "b"]
