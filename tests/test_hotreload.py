"""运行期热更新：成功切换、失败回退、并发串行化、未变复用、在途请求不受影响。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import yaml
from fastapi import Depends
from fastapi.testclient import TestClient

from app.ext.fastapi_lifespan import requires
from app.factory import create_app
from app.plugins.contract import InterfaceRequirement, PluginContext, PluginSpec
from app.plugins.errors import PluginConfigRejectedError
from app.plugins.registry import PluginRegistry
from app.runtime.errors import ReloadError
from app.runtime.supervisor import PluginSupervisor
from app.sample_plugins.clock import CLOCK_SPEC, ClockService
from app.sample_plugins.greeter import GREETER_SPEC, GreeterService

from .conftest import make_config, make_spec


def _supervisor(specs, configs: list) -> tuple[PluginSupervisor, dict[str, int]]:
    """构造监督器；configs[cursor["i"]] 为当前应加载的配置。"""
    registry = PluginRegistry()
    for spec in specs:
        registry.register(spec)
    registry.validate_graph()
    cursor = {"i": 0}
    return PluginSupervisor(registry, lambda: configs[cursor["i"]]), cursor


def _spec_with_validator(base: PluginSpec, validate) -> PluginSpec:
    """给测试规格追加 validate_options（插件加载前自检并可拒绝更新）。"""
    return PluginSpec(
        manifest=base.manifest,
        factory=base.factory,
        on_start=base.on_start,
        on_stop=base.on_stop,
        validate_options=validate,
    )


def _reject_when_option_empty(plugin: str, option: str):
    """构造一个 validate_options：option 为空字符串时主动拒绝（PluginConfigRejectedError）。"""

    def validate(options: dict) -> None:
        value = options.get(option)
        if not isinstance(value, str) or not value.strip():
            raise PluginConfigRejectedError(
                plugin, f"{option} 必须是非空字符串，拒绝本次更新（收到 {value!r}）"
            )

    return validate


# -- 监督器级：成功切换与复用 --------------------------------------------


async def test_reload_reuses_unchanged_and_rebuilds_changed() -> None:
    """配置未变的应用级插件复用旧实例；配置变化的才重建并级联重建依赖方。"""
    track: list[str] = []
    clock = make_spec("clock", track=track)
    cache = make_spec("cache", requires=(InterfaceRequirement("clock", "^1.0.0"),), track=track)
    configs = [
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"cache": {"size": 1}},
        ),
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"cache": {"size": 2}},
        ),
    ]
    sup, cursor = _supervisor([clock, cache], configs)
    await sup.start()
    old_container = sup.current_container()
    old_clock = old_container.get("clock")

    cursor["i"] = 1
    outcome = await sup.reload()

    assert outcome.ok
    assert outcome.reused == ["clock"]
    assert outcome.rebuilt == ["cache"]
    new_container = sup.current_container()
    assert new_container is not old_container
    # 未变化的 clock 复用旧实例：不重复创建、不被停止
    assert new_container.get("clock") is old_clock
    assert track.count("create:clock") == 1
    assert "stop:clock" not in track
    # 变化的 cache 重建：新容器里是新实例
    assert new_container.get("cache") is not old_container.get_by_name("cache")
    # 旧容器已退役；无在途请求 -> 排空后旧 cache 被停止、旧容器被释放
    assert old_container.retired
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "stop:cache" in track
    assert old_container.disposed


async def test_dependency_change_cascades_rebuild() -> None:
    """依赖被重建时，依赖方即使配置未变也必须级联重建（不能持有旧依赖）。"""
    track: list[str] = []
    clock = make_spec("clock", track=track)
    cache = make_spec("cache", requires=(InterfaceRequirement("clock", "^1.0.0"),), track=track)
    configs = [
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"clock": {"zone": "utc"}},
        ),
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"clock": {"zone": "asia"}},
        ),
    ]
    sup, cursor = _supervisor([clock, cache], configs)
    await sup.start()
    cursor["i"] = 1
    outcome = await sup.reload()
    assert outcome.ok
    assert outcome.reused == []
    assert sorted(outcome.rebuilt) == ["cache", "clock"]
    assert track.count("create:clock") == 2
    assert track.count("create:cache") == 2


# -- 监督器级：失败整体回退 -----------------------------------------------


async def test_reload_failure_keeps_old_assembly_serving(caplog) -> None:
    """装配中途失败：抛 ReloadError（带失败链路），旧装配继续可用。"""
    track: list[str] = []
    clock = make_spec("clock", track=track)

    def maybe_broken(ctx: PluginContext) -> Any:
        if ctx.options.get("broken"):
            raise RuntimeError("cache 初始化爆炸")
        return {"size": ctx.options.get("size")}

    cache = make_spec(
        "cache",
        requires=(InterfaceRequirement("clock", "^1.0.0"),),
        instance_factory=maybe_broken,
        track=track,
    )
    configs = [
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"cache": {"size": 1}},
        ),
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"cache": {"broken": True}},
        ),
    ]
    sup, cursor = _supervisor([clock, cache], configs)
    await sup.start()
    old_container = sup.current_container()
    old_clock = old_container.get("clock")

    cursor["i"] = 1
    with pytest.raises(ReloadError) as exc:
        await sup.reload()
    assert exc.value.stage == "assembly"
    assert "cache" in exc.value.chain  # 失败链路可定位到失败点

    # 旧装配原样对外服务：容器未换、实例未动、未退役
    assert sup.current_container() is old_container
    assert sup.current_container().get("clock") is old_clock
    assert not old_container.retired
    assert not old_container.disposed
    assert "stop:clock" not in track
    # 日志可看到失败链路
    assert "失败链路" in "\n".join(r.message for r in caplog.records) or exc.value.chain

    # 修复配置后再次热更新可以成功（应用没有进入半死不活状态）
    configs.append(configs[0])
    cursor["i"] = 2
    outcome = await sup.reload()
    assert outcome.ok


async def test_reload_plugin_rejects_config_rolls_back_and_keeps_old(caplog) -> None:
    """插件装配前自检不通过并主动拒绝：更新失败可解释，本次尝试已启动的
    插件按依赖逆序回滚释放，旧装配继续服务；配置改回合法后再次更新成功。

    依赖链 clock -> cache -> auth，拒绝点在末端 auth；clock/cache 在本次
    尝试中因配置变化被重建并启动，失败时必须被逆序 stop，且新容器被释放。
    """
    track: list[str] = []
    clock = make_spec("clock", track=track)
    cache = make_spec(
        "cache",
        requires=(InterfaceRequirement("clock", "^1.0.0"),),
        track=track,
    )
    auth0 = make_spec(
        "auth",
        requires=(InterfaceRequirement("cache", "^1.0.0"),),
        track=track,
    )
    auth = _spec_with_validator(auth0, _reject_when_option_empty("auth", "token"))

    configs = [
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "auth": (True, "app")},
            options={"clock": {"zone": "utc"}, "cache": {"size": 1}, "auth": {"token": "tok"}},
        ),
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "auth": (True, "app")},
            # clock/cache 都变了需重建；auth 的 token 为空 -> 主动拒绝
            options={"clock": {"zone": "asia"}, "cache": {"size": 2}, "auth": {"token": ""}},
        ),
    ]
    sup, cursor = _supervisor([clock, cache, auth], configs)
    await sup.start()
    old_container = sup.current_container()
    old_clock = old_container.get("clock")
    old_cache = old_container.get_by_name("cache")

    cursor["i"] = 1
    with pytest.raises(ReloadError) as exc:
        await sup.reload()

    # 可解释结果：阶段=装配、原因来自插件自检、链路指向失败点
    assert exc.value.stage == "assembly"
    assert "auth" in exc.value.reason
    assert "token" in exc.value.reason
    assert exc.value.chain == ["clock", "cache", "auth"]

    # 让旧容器的排空回调跑完（旧容器不应被退役，此处理论上无任务，保险起见）
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # 旧装配原样对外服务：容器未换、旧实例未动、未退役未释放
    assert sup.current_container() is old_container
    assert sup.current_container().get("clock") is old_clock
    assert sup.current_container().get_by_name("cache") is old_cache
    assert not old_container.retired
    assert not old_container.disposed

    # 本次尝试：clock/cache 被重建并启动，auth 在 validate 阶段拒绝（未实例化）；
    # 失败后按依赖逆序回滚：cache -> clock，各自恰好停止一次
    assert track == [
        "create:clock",
        "start:clock",
        "create:cache",
        "start:cache",
        "create:auth",
        "start:auth",
        "create:clock",
        "start:clock",
        "create:cache",
        "start:cache",
        "stop:cache",
        "stop:clock",
    ]
    assert "create:auth" in track  # 仅初始装配创建过 auth
    assert track.count("create:auth") == 1  # 拒绝的这次没有产生 auth 实例
    # 旧实例仍可用
    assert old_clock["options"] == {"zone": "utc"}
    assert old_cache["options"] == {"size": 1}

    # 日志覆盖配置来源、装配顺序与失败链路
    log_text = "\n".join(r.message for r in caplog.records)
    assert "配置来源" in log_text or "配置加载来源" in log_text
    assert "装配顺序" in log_text
    assert "失败链路: clock -> cache -> auth" in log_text
    assert "已回滚: ['cache', 'clock']" in log_text

    # 配置改回合法（含新值）后再次触发热更新，成功切换到新装配
    configs.append(
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "auth": (True, "app")},
            options={"clock": {"zone": "tokyo"}, "cache": {"size": 3}, "auth": {"token": "new"}},
        )
    )
    cursor["i"] = 2
    outcome = await sup.reload()
    assert outcome.ok
    assert sup.current_container() is not old_container
    assert sup.current_container().get("clock")["options"] == {"zone": "tokyo"}
    assert sup.current_container().get_by_name("cache")["options"] == {"size": 3}
    assert sup.current_container().get_by_name("auth")["options"] == {"token": "new"}


async def test_reload_rejected_config_reuses_unchanged_and_old_keeps_serving() -> None:
    """拒绝点之前的插件配置未变时被复用：回滚不得波及复用实例，旧装配继续可用。"""
    track: list[str] = []
    clock = make_spec("clock", track=track)
    auth0 = make_spec("auth", requires=(InterfaceRequirement("clock", "^1.0.0"),), track=track)
    auth = _spec_with_validator(auth0, _reject_when_option_empty("auth", "token"))
    configs = [
        make_config(
            {"clock": (True, "app"), "auth": (True, "app")},
            options={"auth": {"token": "ok"}},
        ),
        make_config(
            {"clock": (True, "app"), "auth": (True, "app")},
            options={"auth": {"token": "  "}},
        ),
    ]
    sup, cursor = _supervisor([clock, auth], configs)
    await sup.start()
    old_container = sup.current_container()
    old_clock = old_container.get("clock")

    cursor["i"] = 1
    with pytest.raises(ReloadError) as exc:
        await sup.reload()
    assert exc.value.chain == ["clock", "auth"]

    await asyncio.sleep(0)
    # clock 配置未变 -> 复用旧实例：不重建、不被停止；新容器被丢弃释放
    assert track == ["create:clock", "start:clock", "create:auth", "start:auth"]
    assert "stop:clock" not in track
    assert sup.current_container() is old_container
    assert sup.current_container().get("clock") is old_clock


async def test_concurrent_failed_reloads_are_deterministic_and_isolated() -> None:
    """并发多次触发失败更新：全部可解释失败、结果确定、不互相踩踏、旧实例不泄漏。"""
    track: list[str] = []
    clock = make_spec("clock", track=track)
    cache = make_spec("cache", requires=(InterfaceRequirement("clock", "^1.0.0"),), track=track)
    auth0 = make_spec("auth", requires=(InterfaceRequirement("cache", "^1.0.0"),), track=track)
    auth = _spec_with_validator(auth0, _reject_when_option_empty("auth", "token"))
    configs = [
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "auth": (True, "app")},
            options={"clock": {"z": 1}, "cache": {"s": 1}, "auth": {"token": "t"}},
        ),
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "auth": (True, "app")},
            options={"clock": {"z": 2}, "cache": {"s": 2}, "auth": {"token": ""}},
        ),
    ]
    sup, cursor = _supervisor([clock, cache, auth], configs)
    await sup.start()
    old_container = sup.current_container()
    old_clock = old_container.get("clock")

    cursor["i"] = 1
    errors = await asyncio.gather(*(sup.reload() for _ in range(5)), return_exceptions=True)
    assert all(isinstance(e, ReloadError) for e in errors)
    assert all(e.stage == "assembly" for e in errors)  # type: ignore[union-attr]
    assert all(e.chain == ["clock", "cache", "auth"] for e in errors)  # type: ignore[union-attr]

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # 旧装配确定不变：每次失败尝试重建出的 clock/cache 都成对回收，
    # 每次尝试新增 create/start clock,cache 各一次，并被逆序 stop 各一次
    assert sup.current_container() is old_container
    assert sup.current_container().get("clock") is old_clock
    assert not old_container.retired
    assert track.count("create:clock") == 6  # 初始 1 + 5 次失败尝试
    assert track.count("start:clock") == 6
    assert track.count("stop:clock") == 5  # 只有本次尝试新建的被停
    assert track.count("create:cache") == 6
    assert track.count("stop:cache") == 5
    # auth 仅初始装配创建过一次（后续都在 validate 阶段被拒绝）
    assert track.count("create:auth") == 1

    # 串行化后改回合法配置，仍可正常成功切换
    configs.append(
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "auth": (True, "app")},
            options={"clock": {"z": 3}, "cache": {"s": 3}, "auth": {"token": "tt"}},
        )
    )
    cursor["i"] = 2
    outcome = await sup.reload()
    assert outcome.ok


async def test_reload_with_invalid_config_fails_at_config_stage() -> None:
    """新配置不合法：在配置阶段即失败，不进入装配，旧装配不受影响。"""
    clock = make_spec("clock")
    good = make_config({"clock": (True, "app")})
    sup, _ = _supervisor([clock], [good])
    await sup.start()
    old_container = sup.current_container()

    def bad_loader():
        raise ValueError("配置文件第 3 行: enabled 必须是 bool")

    sup._config_loader = bad_loader  # noqa: SLF001 - 模拟配置源变坏
    with pytest.raises(ReloadError) as exc:
        await sup.reload()
    assert exc.value.stage == "config"
    assert "enabled" in exc.value.reason
    assert sup.current_container() is old_container


# -- 监督器级：并发触发确定可解释 -----------------------------------------


async def test_concurrent_reloads_serialized_no_wasted_work() -> None:
    """并发触发：串行执行、结果确定；配置相同的后续更新不做无用功。"""
    track: list[str] = []
    clock = make_spec("clock", track=track)
    cache = make_spec("cache", requires=(InterfaceRequirement("clock", "^1.0.0"),), track=track)
    configs = [
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"cache": {"size": 1}},
        ),
        make_config(
            {"clock": (True, "app"), "cache": (True, "app")},
            options={"cache": {"size": 2}},
        ),
    ]
    sup, cursor = _supervisor([clock, cache], configs)
    await sup.start()

    cursor["i"] = 1
    outcomes = await asyncio.gather(*(sup.reload() for _ in range(5)))
    assert all(o.ok for o in outcomes)
    # 串行化后：第一个完成的更新重建 cache，其余发现配置已一致 -> 全部复用
    assert track.count("create:cache") == 2  # 首次装配 + 仅一次重建
    assert track.count("create:clock") == 1  # clock 始终未变，从不重建
    assert sup.current_container().get_by_name("cache")["options"] == {"size": 2}
    # 最终状态确定：当前配置即目标配置
    assert sup.current_config().plugins["cache"].options == {"size": 2}


# -- 监督器级：在途请求不被打断 -------------------------------------------


async def test_inflight_request_scope_unaffected_by_reload() -> None:
    """更新期间在途请求继续用旧装配跑完；旧容器等其结束后才释放。"""
    clock = make_spec("clock")
    greeter = make_spec(
        "greeter", requires=(InterfaceRequirement("clock", "^1.0.0"),), scope="request"
    )
    configs = [
        make_config(
            {"clock": (True, "app"), "greeter": (True, "request")},
            options={"greeter": {"greeting": "old"}},
        ),
        make_config(
            {"clock": (True, "app"), "greeter": (True, "request")},
            options={"greeter": {"greeting": "new"}},
        ),
    ]
    sup, cursor = _supervisor([clock, greeter], configs)
    await sup.start()
    old_container = sup.current_container()

    # 模拟一个在途请求：从旧容器派生作用域并取实例
    scope = old_container.new_request_scope()
    inflight_greeter = scope.get("greeter")

    cursor["i"] = 1
    await sup.reload()

    # 切换已生效，但在途作用域仍持有旧装配：实例不被打断、不混用
    assert sup.current_container() is not old_container
    assert scope.get("greeter") is inflight_greeter
    assert not old_container.disposed  # 在途未结束，旧容器不得释放
    assert old_container.active_scopes == 1

    # 在途请求结束 -> 旧容器排空并释放
    await scope.dispose()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert old_container.disposed

    # 新请求走新装配
    new_scope = sup.current_container().new_request_scope()
    assert new_scope.get("greeter") is not inflight_greeter
    await new_scope.dispose()


# -- HTTP 级：端到端热更新 -------------------------------------------------


def _write_config(path, greeting: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "clock": {"enabled": True, "scope": "app", "options": {}},
                    "greeter": {
                        "enabled": True,
                        "scope": "request",
                        "options": {"greeting": greeting},
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def _http_app(config_path) -> Any:
    app = create_app(
        config_path=str(config_path),
        defaults={"app_name": "t", "log_level": "info", "plugins": {}},
        plugin_specs=[CLOCK_SPEC, GREETER_SPEC],
        environ={},
    )

    @app.get("/hello")
    async def hello(
        greeter: GreeterService = Depends(requires("greeter")),
        clock: ClockService = Depends(requires("clock")),
    ):
        return {"greeting": greeter.greet("api"), "tick": clock.tick()}

    return app


def _http_app_with_billing(config_path, overrides: dict, events: list[str]) -> Any:
    """装配一个会在 validate_options 阶段主动拒绝非法配置的应用级 billing 插件。

    依赖 clock，失败链路应是 clock -> billing；clock/billing 均为可追踪生命周期
    的自定义规格（on_stop 记入 events）。通过 overrides（独立配置来源）改动
    options，覆盖“非配置文件来源触发更新”的真实用法。
    """
    from app.plugins.contract import PluginManifest

    class BillingService:
        def __init__(self, clock, api_key: str) -> None:
            self.clock = clock
            self.api_key = api_key

        def describe(self) -> str:
            return f"billing({self.api_key})"

    def _clock_factory(ctx: PluginContext):
        events.append("create:clock")
        return {"name": "clock", "options": ctx.options}

    async def _clock_on_start(_) -> None:
        events.append("start:clock")

    async def _clock_on_stop(_) -> None:
        events.append("stop:clock")

    clock_spec = PluginSpec(
        manifest=PluginManifest(name="clock", version="1.0.0", provides=("clock",), scope="app"),
        factory=_clock_factory,
        on_start=_clock_on_start,
        on_stop=_clock_on_stop,
    )

    def _factory(ctx: PluginContext) -> BillingService:
        events.append("create:billing")
        return BillingService(ctx.dependencies["clock"], ctx.options.get("api_key", ""))

    async def _on_start(_: BillingService) -> None:
        events.append("start:billing")

    async def _on_stop(_: BillingService) -> None:
        events.append("stop:billing")

    def _validate(options: dict) -> None:
        api_key = options.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise PluginConfigRejectedError("billing", "api_key 必须是非空字符串")

    billing_spec = PluginSpec(
        manifest=PluginManifest(
            name="billing",
            version="1.0.0",
            provides=("billing",),
            requires=(InterfaceRequirement("clock", "^1.0.0"),),
            scope="app",
        ),
        factory=_factory,
        on_start=_on_start,
        on_stop=_on_stop,
        validate_options=_validate,
    )
    app = create_app(
        config_path=str(config_path),
        defaults={"app_name": "t", "log_level": "info", "plugins": {}},
        plugin_specs=[clock_spec, billing_spec],
        environ={},
        overrides=overrides,
    )

    @app.get("/billing")
    async def billing(svc: BillingService = Depends(requires("billing"))):
        return {"billing": svc.describe(), "zone": svc.clock["options"].get("zone")}

    return app


def test_http_reload_switches_without_restart(tmp_path, caplog) -> None:
    """改配置文件 -> POST /reload -> 新请求用新配置；clock 单例被复用。"""
    cfg = tmp_path / "app.yaml"
    _write_config(cfg, "hi")
    app = _http_app(cfg)
    with TestClient(app) as client:
        r1 = client.get("/hello").json()
        assert r1["greeting"].startswith("hi, api")

        _write_config(cfg, "hola")
        resp = client.post("/reload").json()
        assert resp["ok"] is True
        assert resp["reused"] == ["clock"]

        r2 = client.get("/hello").json()
        assert r2["greeting"].startswith("hola, api")
        # clock 复用旧实例：tick 在原有基础上继续累加，没有重新初始化
        assert r2["tick"] == r1["tick"] + 2

    log_text = "\n".join(r.message for r in caplog.records)
    assert "配置加载来源" in log_text
    assert "装配顺序" in log_text
    assert "热更新" in log_text


def test_http_reload_failure_keeps_old_assembly(tmp_path, caplog) -> None:
    """新配置非法（插件拒绝 greeting 为空）：更新失败，旧装配继续服务。"""
    cfg = tmp_path / "app.yaml"
    _write_config(cfg, "hi")
    app = _http_app(cfg)
    with TestClient(app) as client:
        assert client.get("/hello").json()["greeting"].startswith("hi, api")

        _write_config(cfg, "   ")  # greeter.validate_options 拒绝空白 greeting
        resp = client.post("/reload").json()
        assert resp["ok"] is False
        assert resp["stage"] == "assembly"
        assert resp["failure_chain"]  # 能定位到失败点

        # 旧装配继续对外服务
        assert client.get("/hello").json()["greeting"].startswith("hi, api")

        # 修复后再次更新成功
        _write_config(cfg, "fixed")
        assert client.post("/reload").json()["ok"] is True
        assert client.get("/hello").json()["greeting"].startswith("fixed, api")

    log_text = "\n".join(r.message for r in caplog.records)
    assert "热更新" in log_text


def test_http_reload_plugin_reject_via_overrides_returns_explained_failure(
    tmp_path, caplog
) -> None:
    """overrides 配置来源把应用级插件改成非法配置：POST /reload 必须返回
    带 stage/reason/failure_chain 的可解释失败（而非 500），旧装配继续服务，
    本次尝试已启动插件被回滚；改回合法覆盖项后再次更新成功。
    """
    cfg = tmp_path / "app.yaml"
    _write_config(cfg, "hi")  # clock/greeter 也在文件里，但本次只装配 clock+billing
    events: list[str] = []
    overrides = {
        "plugins": {
            "clock": {"enabled": True, "scope": "app", "options": {"zone": "utc"}},
            "billing": {"enabled": True, "scope": "app", "options": {"api_key": "key-1"}},
        }
    }
    app = _http_app_with_billing(cfg, overrides, events)
    with TestClient(app) as client:
        r1 = client.get("/billing").json()
        assert r1 == {"billing": "billing(key-1)", "zone": "utc"}

        # 通过 overrides（非文件来源）触发：clock 配置变化需重建，billing 拒绝空 api_key
        overrides["plugins"]["clock"]["options"] = {"zone": "asia"}
        overrides["plugins"]["billing"]["options"] = {"api_key": ""}
        resp = client.post("/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["stage"] == "assembly"
        assert "billing" in body["reason"]
        assert "api_key" in body["reason"]
        assert body["failure_chain"] == ["clock", "billing"]

        # 旧装配继续对外服务：同进程其它请求不受影响，仍是旧 clock 与旧 billing
        r2 = client.get("/billing").json()
        assert r2 == {"billing": "billing(key-1)", "zone": "utc"}

        # 本次尝试：clock 重建并启动后，billing 在自检阶段拒绝（未实例化）；
        # 失败按依赖逆序回滚 -> 新 clock 被 stop，新容器被释放；旧实例不受影响
        assert events == [
            "create:clock",
            "start:clock",
            "create:billing",
            "start:billing",
            "create:clock",
            "start:clock",
            "stop:clock",
        ]

        # 改回合法覆盖项，再次热更新成功切换
        overrides["plugins"]["clock"]["options"] = {"zone": "tokyo"}
        overrides["plugins"]["billing"]["options"] = {"api_key": "key-2"}
        ok_body = client.post("/reload").json()
        assert ok_body["ok"] is True
        r3 = client.get("/billing").json()
        assert r3 == {"billing": "billing(key-2)", "zone": "tokyo"}

    log_text = "\n".join(r.message for r in caplog.records)
    # 配置来源（含 overrides）、装配顺序、失败链路都在日志里
    assert "overrides" in log_text
    assert "装配顺序" in log_text
    assert "clock -> billing" in log_text
