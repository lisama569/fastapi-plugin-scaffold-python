"""运行期热更新：成功切换、失败回退、并发串行化、实例复用、在途请求不受影响。"""

from __future__ import annotations

import asyncio
from typing import Any

import yaml
from fastapi import Depends
from fastapi.testclient import TestClient

from app.ext.fastapi_lifespan import get_report, requires
from app.factory import build_registry, create_app
from app.plugins.contract import InterfaceRequirement
from app.runtime.reloader import Runtime, compute_reuse_plan
from app.sample_plugins.clock import CLOCK_SPEC, ClockService
from app.sample_plugins.greeter import GREETER_SPEC, GreeterService

from .conftest import make_config, make_spec


def _write_config(path, plugins: dict[str, Any], log_level: str = "info") -> None:
    path.write_text(
        yaml.safe_dump({"app_name": "demo", "log_level": log_level, "plugins": plugins}),
        encoding="utf-8",
    )


def _file_plugins(greeting: str = "hi") -> dict[str, Any]:
    return {
        "clock": {"enabled": True, "scope": "app", "options": {}},
        "greeter": {"enabled": True, "scope": "request", "options": {"greeting": greeting}},
    }


def _build_file_app(tmp_path, greeting: str = "hi", **kwargs):
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, _file_plugins(greeting))
    app = create_app(
        config_path=str(cfg),
        environ={},
        plugin_specs=[CLOCK_SPEC, GREETER_SPEC],
        **kwargs,
    )

    @app.get("/hello")
    async def hello(
        greeter: GreeterService = Depends(requires("greeter")),
        clock: ClockService = Depends(requires("clock")),
    ):
        return {"greeting": greeter.greet("api"), "tick": clock.tick()}

    return app, cfg


# ---------------------------------------------------------------- HTTP 端到端


def test_reload_success_switches_to_new_config(tmp_path, caplog) -> None:
    app, cfg = _build_file_app(tmp_path, greeting="hi")
    with TestClient(app) as client:
        assert client.get("/hello").json()["greeting"].startswith("hi, api")

        _write_config(cfg, _file_plugins("yo"))
        r = client.post("/admin/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["generation"] == 2
        assert body["reused"] == ["clock"]  # 配置未变的应用级插件被复用
        assert body["rebuilt"] == []

        # 新请求走新装配：greeting 生效为 yo
        assert client.get("/hello").json()["greeting"].startswith("yo, api")

        report = get_report(app)
        assert report.reused == ["clock"]

    log_text = "\n".join(r.message for r in caplog.records)
    assert "复用计划" in log_text
    assert "热更新完成" in log_text


def test_reload_reuses_unchanged_singleton_instance(tmp_path) -> None:
    app, cfg = _build_file_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/hello").json()["tick"] == 2
        _write_config(cfg, _file_plugins("yo"))
        assert client.post("/admin/reload").json()["ok"] is True
        # clock 实例被复用而非重建：tick 计数延续而不是清零重来
        assert client.get("/hello").json()["tick"] == 4


def test_reload_config_stage_failure_keeps_old_assembly(tmp_path, caplog) -> None:
    app, cfg = _build_file_app(tmp_path, greeting="hi")
    with TestClient(app) as client:
        # 非法配置：缺少必填 enabled
        _write_config(cfg, {"greeter": {"scope": "request", "options": {}}})
        r = client.post("/admin/reload")
        assert r.status_code == 500
        body = r.json()
        assert body["ok"] is False
        assert body["stage"] == "config"
        assert body["failure_reason"]
        assert body["failure_chain"]

        # 旧装配继续正常服务
        assert client.get("/hello").json()["greeting"].startswith("hi, api")
        assert get_report(app).ok is True

    log_text = "\n".join(r.message for r in caplog.records)
    assert "热更新失败(配置阶段)" in log_text


def test_reload_assemble_stage_failure_keeps_old_assembly(tmp_path, caplog) -> None:
    app, cfg = _build_file_app(tmp_path, greeting="hi")
    with TestClient(app) as client:
        # greeter 的 validate_options 拒绝空 greeting -> 装配阶段失败
        _write_config(cfg, _file_plugins(greeting=""))
        r = client.post("/admin/reload")
        assert r.status_code == 500
        body = r.json()
        assert body["ok"] is False
        assert body["stage"] == "assemble"
        assert body["failure_plugin"] == "greeter"
        assert body["failure_chain"] == ["clock", "greeter"]

        # 旧装配继续正常服务，且未进入半死不活状态
        assert client.get("/hello").json()["greeting"].startswith("hi, api")

        # 修复配置后可再次成功热更新
        _write_config(cfg, _file_plugins(greeting="fixed"))
        assert client.post("/admin/reload").json()["ok"] is True
        assert client.get("/hello").json()["greeting"].startswith("fixed, api")

    log_text = "\n".join(r.message for r in caplog.records)
    assert "热更新失败(装配阶段)" in log_text
    assert "失败链路" in log_text


def test_reload_dependency_disabled_is_rejected(tmp_path) -> None:
    app, cfg = _build_file_app(tmp_path)
    with TestClient(app) as client:
        plugins = _file_plugins()
        plugins["clock"]["enabled"] = False  # greeter 依赖的 clock 被禁用
        _write_config(cfg, plugins)
        r = client.post("/admin/reload")
        assert r.status_code == 500
        assert r.json()["stage"] == "assemble"
        assert client.get("/hello").status_code == 200


# ------------------------------------------------------------- 直接驱动 Runtime


def _make_runtime(track: list[str], configs: list, specs=None):
    registry = build_registry(specs if specs is not None else [CLOCK_SPEC, GREETER_SPEC])
    # configs[0] 是启动配置；provider 只在热更新时被调用，从后续配置依次取
    queue = list(configs[1:])

    def provider():
        return queue.pop(0) if queue else configs[-1]

    return Runtime(registry, provider, configs[0]), registry


def test_concurrent_reloads_are_serialized_and_deterministic(caplog) -> None:
    track: list[str] = []
    spec = make_spec("a", track=track)

    async def scenario() -> list:
        cfgs = [make_config({"a": (True, "app")}, options={"a": {"v": i}}) for i in range(4)]
        runtime, _ = _make_runtime(track, cfgs, specs=[spec])
        await runtime.start()
        reports = await asyncio.gather(*(runtime.reload() for _ in range(3)))
        await runtime.wait_idle()
        await runtime.shutdown()
        return list(reports)

    reports = asyncio.run(scenario())
    assert all(r.ok for r in reports)
    # 串行化：每一代依次 +1，最终代确定
    assert sorted(r.generation for r in reports) == [2, 3, 4]
    # 每次 options 都变 -> 各重建一次；每个实例最终恰好被停止一次（不泄漏、不重复停）
    assert track.count("create:a") == 4
    assert track.count("stop:a") == 4
    log_text = "\n".join(r.message for r in caplog.records)
    assert "开始热更新" in log_text


def test_inflight_request_uninterrupted_during_reload() -> None:
    async def scenario() -> None:
        cfg1 = make_config(
            {"clock": (True, "app"), "greeter": (True, "request")},
            options={"greeter": {"greeting": "hi"}},
        )
        cfg2 = make_config(
            {"clock": (True, "app"), "greeter": (True, "request")},
            options={"greeter": {"greeting": "yo"}},
        )
        runtime, _ = _make_runtime([], [cfg1, cfg2])
        await runtime.start()

        # 一个在途请求：持有第 1 代与它的请求作用域
        gen1 = runtime.current_and_acquire()
        scope1 = gen1.container.new_request_scope()
        old_greeter = scope1.get("greeter")
        old_clock = scope1.get("clock")

        report = await runtime.reload()
        assert report.ok and report.reused == ["clock"]

        # 在途请求继续用旧实例跑完：不被打断、不混用新实例
        assert scope1.get("greeter") is old_greeter
        assert old_greeter.greet("x").startswith("hi, x")
        assert not gen1.container.disposed  # 在途未归零，旧容器不得释放

        # 新请求使用新装配
        gen2 = runtime.current_and_acquire()
        scope2 = gen2.container.new_request_scope()
        new_greeter = scope2.get("greeter")
        assert new_greeter is not old_greeter
        assert new_greeter.greet("x").startswith("yo, x")
        # 复用的应用级单例在两代间是同一实例
        assert scope2.get("clock") is old_clock
        await scope2.dispose()
        gen2.release()

        # 在途请求结束 -> 旧代退役释放
        await scope1.dispose()
        gen1.release()
        await runtime.wait_idle()
        assert gen1.container.disposed
        await runtime.shutdown()

    asyncio.run(scenario())


def test_removed_plugin_stopped_after_drain() -> None:
    track: list[str] = []
    spec = make_spec("a", track=track)

    async def scenario() -> None:
        on = make_config({"a": (True, "app")})
        off = make_config({"a": (False, "app")})
        runtime, _ = _make_runtime(track, [on, off], specs=[spec])
        await runtime.start()
        report = await runtime.reload()
        assert report.ok and report.removed == ["a"]
        await runtime.wait_idle()
        await runtime.shutdown()

    asyncio.run(scenario())
    # 禁用后旧实例被停止且不会重复初始化
    assert track == ["create:a", "start:a", "stop:a"]


def test_dependent_rebuilt_when_dependency_changes() -> None:
    track: list[str] = []
    a = make_spec("a", track=track)
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),), track=track)

    async def scenario() -> tuple:
        cfg1 = make_config({"a": (True, "app"), "b": (True, "app")}, options={"a": {"v": 1}})
        cfg2 = make_config({"a": (True, "app"), "b": (True, "app")}, options={"a": {"v": 2}})
        runtime, _ = _make_runtime(track, [cfg1, cfg2], specs=[a, b])
        await runtime.start()
        report = await runtime.reload()
        await runtime.wait_idle()
        await runtime.shutdown()
        return report

    report = asyncio.run(scenario())
    # 只改了 a 的 options，但 b 依赖 a -> 两者都必须重建，杜绝新旧混用
    assert report.ok
    assert report.reused == []
    assert report.rebuilt == ["a", "b"]
    assert track.count("create:a") == 2
    assert track.count("create:b") == 2


def test_reuse_plan_unchanged_configs_reuse_everything() -> None:
    registry = build_registry([CLOCK_SPEC, GREETER_SPEC])
    cfg = make_config({"clock": (True, "app"), "greeter": (True, "request")})
    same = make_config({"clock": (True, "app"), "greeter": (True, "request")})
    assert compute_reuse_plan(registry, cfg, same) == {"clock"}
    changed = make_config(
        {"clock": (True, "app"), "greeter": (True, "request")},
        options={"clock": {"tz": "utc"}},
    )
    assert compute_reuse_plan(registry, cfg, changed) == set()
