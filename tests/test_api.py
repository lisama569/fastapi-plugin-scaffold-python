"""端到端：FastAPI lifespan 装配、接口注入、并发共享、启动失败即拒绝。"""

from __future__ import annotations

import threading

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.config.errors import ConfigMissingError
from app.ext.fastapi_lifespan import get_report, requires
from app.factory import create_app
from app.plugins.contract import InterfaceRequirement
from app.plugins.errors import (
    AssemblyError,
    CircularDependencyError,
    DuplicatePluginError,
    VersionIncompatibleError,
)
from app.sample_plugins.clock import CLOCK_SPEC, ClockService
from app.sample_plugins.greeter import GREETER_SPEC, GreeterService

from .conftest import make_spec


def _build_app(**kwargs):
    app = create_app(
        defaults={
            "app_name": "test",
            "log_level": "info",
            "plugins": {
                "clock": {"enabled": True, "scope": "app", "options": {}},
                "greeter": {"enabled": True, "scope": "request", "options": {"greeting": "hi"}},
            },
        },
        plugin_specs=[CLOCK_SPEC, GREETER_SPEC],
        **kwargs,
    )

    @app.get("/hello")
    async def hello(
        greeter: GreeterService = Depends(requires("greeter")),
        clock: ClockService = Depends(requires("clock")),
    ):
        return {"greeting": greeter.greet("api"), "tick": clock.tick()}

    return app


def test_full_assembly_and_injection(caplog) -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.get("/hello")
        assert r.status_code == 200
        assert r.json()["greeting"] == "hi, api (tick=1)"

        report = get_report(app)
        assert report.ok is True
        assert report.assembled == ["clock"]
        assert report.request_registered == ["greeter"]
        assert report.source_order == ["defaults", "env"]

    # 日志中必须出现来源、装配顺序
    log_text = "\n".join(r.message for r in caplog.records)
    assert "配置加载来源" in log_text
    assert "装配顺序" in log_text


def test_clock_singleton_shared_across_http_requests() -> None:
    app = _build_app()
    with TestClient(app) as client:
        ticks = [client.get("/hello").json()["tick"] for _ in range(5)]
        # clock 是应用级单例：每请求 greet 与路由各 tick 一次(+2)，单调累加
        assert ticks == [2, 4, 6, 8, 10]


def test_concurrent_http_requests_share_one_clock_instance() -> None:
    app = _build_app()
    with TestClient(app) as client:
        results: list[int] = []
        errors: list[Exception] = []

        def hit() -> None:
            try:
                results.append(client.get("/hello").json()["tick"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # 20 个并发请求每请求 +2，tick 无重复、覆盖 2..40 -> 单例未重复初始化/污染
        assert sorted(results) == list(range(2, 41, 2))


def test_greeter_is_request_scoped() -> None:
    app = _build_app()
    with TestClient(app) as client:
        # 每个请求 greeter 重新创建；但共享同一 clock
        r1 = client.get("/hello").json()
        r2 = client.get("/hello").json()
        assert r1["greeting"].startswith("hi, api")
        assert r2["greeting"].startswith("hi, api")
        assert r2["tick"] > r1["tick"]  # clock 状态持续累积


def test_config_override_via_env_changes_greeting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTAPI_PLUGIN_PLUGINS__GREETER__OPTIONS__GREETING", "env-hello")
    app = _build_app()
    with TestClient(app) as client:
        body = client.get("/hello").json()
        assert body["greeting"].startswith("env-hello, api")


def test_startup_fails_on_missing_required_config() -> None:
    with pytest.raises(ConfigMissingError):
        create_app(
            defaults={"log_level": "info", "plugins": {}},
            plugin_specs=[CLOCK_SPEC],
            environ={},
        )  # lifespan 启动即应抛出


def test_startup_fails_on_circular_dependency() -> None:
    a = make_spec("a", requires=(InterfaceRequirement("b", "^1.0.0"),))
    b = make_spec("b", requires=(InterfaceRequirement("a", "^1.0.0"),))
    with pytest.raises(CircularDependencyError):
        create_app(
            defaults={"app_name": "x", "log_level": "info", "plugins": {}},
            plugin_specs=[a, b],
            environ={},
        )


def test_startup_fails_on_duplicate_registration() -> None:
    with pytest.raises(DuplicatePluginError):
        create_app(
            defaults={"app_name": "x", "log_level": "info", "plugins": {}},
            plugin_specs=[CLOCK_SPEC, CLOCK_SPEC],
            environ={},
        )


def test_startup_fails_on_version_incompatibility() -> None:
    old_clock = make_spec("clock", version="9.0.0", provides=("clock",))
    # greeter 要求 clock ^1.0.0
    with pytest.raises(VersionIncompatibleError):
        create_app(
            defaults={
                "app_name": "x",
                "log_level": "info",
                "plugins": {
                    "clock": {"enabled": True, "scope": "app", "options": {}},
                    "greeter": {"enabled": True, "scope": "request", "options": {"greeting": "hi"}},
                },
            },
            plugin_specs=[old_clock, GREETER_SPEC],
            environ={},
        )


def test_startup_fails_on_mid_assembly_error(caplog) -> None:
    # clock 正常；另一个 app 级插件 boom 依赖 clock，启动钩子失败 -> 回滚 clock
    boom = make_spec(
        "boom",
        requires=(InterfaceRequirement("clock", "^1.0.0"),),
        start_fail=True,
        fail_message="boom cannot start",
    )
    bad_app = create_app(
        defaults={
            "app_name": "x",
            "log_level": "info",
            "plugins": {
                "clock": {"enabled": True, "scope": "app", "options": {}},
                "boom": {"enabled": True, "scope": "app", "options": {}},
            },
        },
        plugin_specs=[CLOCK_SPEC, boom],
        environ={},
    )
    with pytest.raises(AssemblyError) as exc:
        with TestClient(bad_app):
            pass
    assert exc.value.plugin == "boom"
    assert exc.value.assembled == ["clock"]
    assert exc.value.rolled_back == ["clock"]
    assert "失败链路" in "\n".join(r.message for r in caplog.records)
