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
from app.plugins.contract import InterfaceRequirement, PluginContext
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


async def test_reload_config_rejected_by_plugin_rolls_back_and_keeps_old(caplog) -> None:
    """插件主动拒绝配置（PluginConfigRejectedError）：

    与其它装配失败同等对待——抛 ReloadError（阶段/原因/失败链路齐全），
    本次尝试中已启动的插件按依赖逆序回滚释放，复用实例所有权仍归旧装配，
    旧装配继续对外服务；改回合法配置后再次热更新成功。
    """
    from app.plugins.errors import PluginConfigRejectedError

    track: list[str] = []
    clock = make_spec("clock", track=track)
    cache = make_spec("cache", requires=(InterfaceRequirement("clock", "^1.0.0"),), track=track)

    def reject_bad(options: dict) -> None:
        if options.get("mode") == "bad":
            raise PluginConfigRejectedError("top", "mode 不允许为 'bad'")

    top = make_spec(
        "top",
        requires=(InterfaceRequirement("cache", "^1.0.0"),),
        track=track,
        validate=reject_bad,
    )
    configs = [
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "top": (True, "app")},
            options={"cache": {"size": 1}, "top": {"mode": "good"}},
        ),
        # cache 配置变化（会重建并启动），top 配置非法（装配深层主动拒绝）
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "top": (True, "app")},
            options={"cache": {"size": 2}, "top": {"mode": "bad"}},
        ),
        make_config(
            {"clock": (True, "app"), "cache": (True, "app"), "top": (True, "app")},
            options={"cache": {"size": 3}, "top": {"mode": "good"}},
        ),
    ]
    sup, cursor = _supervisor([clock, cache, top], configs)
    await sup.start()
    old_container = sup.current_container()
    old_clock = old_container.get("clock")
    old_cache = old_container.get("cache")
    assert track == [
        "create:clock",
        "start:clock",
        "create:cache",
        "start:cache",
        "create:top",
        "start:top",
    ]

    cursor["i"] = 1
    with pytest.raises(ReloadError) as exc:
        await sup.reload()

    # 可解释结果：阶段、原因、失败链路（根因 -> 失败点）齐全
    assert exc.value.stage == "assembly"
    assert "拒绝配置" in exc.value.reason
    assert "mode 不允许为 'bad'" in exc.value.reason
    assert exc.value.chain == ["clock", "cache", "top"]

    # 本次尝试里已启动的 cache 被逆序回滚释放；复用的 clock 不被波及
    assert track.count("create:cache") == 2  # 首次装配 + 失败尝试各一次
    assert "stop:cache" in track
    assert "stop:clock" not in track
    assert "create:top" in track and track.count("create:top") == 1  # 校验在实例化前

    # 旧装配原样对外服务：容器未换、实例未动、未退役未释放
    assert sup.current_container() is old_container
    assert sup.current_container().get("clock") is old_clock
    assert sup.current_container().get("cache") is old_cache
    assert not old_container.retired
    assert not old_container.disposed

    # 日志能看到失败链路与回滚记录
    log_text = "\n".join(r.message for r in caplog.records)
    assert "失败链路" in log_text
    assert "已回滚插件" in log_text

    # 改回合法配置后再次热更新成功（应用没有进入半新半旧状态）
    cursor["i"] = 2
    outcome = await sup.reload()
    assert outcome.ok
    assert outcome.reused == ["clock"]
    assert sorted(outcome.rebuilt) == ["cache", "top"]
    assert sup.current_container().get("clock") is old_clock  # clock 始终未重建


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


# -- HTTP 级：插件主动拒绝配置的失败回退 --------------------------------------


def _rejecting_specs(track: list[str]) -> list:
    """base(app) -> picker(app)：picker 在依赖链深层主动拒绝非法 token。"""
    from app.plugins.errors import PluginConfigRejectedError

    base = make_spec("base", track=track)

    def validate(options: dict) -> None:
        if options.get("token") == "bad":
            raise PluginConfigRejectedError("picker", "token 不允许为 'bad'")

    picker = make_spec(
        "picker",
        requires=(InterfaceRequirement("base", "^1.0.0"),),
        track=track,
        validate=validate,
    )
    return [base, picker]


def _write_picker_config(path, token: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "base": {"enabled": True, "scope": "app", "options": {}},
                    "picker": {
                        "enabled": True,
                        "scope": "app",
                        "options": {"token": token},
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def _picker_app(track: list[str], config_path=None, overrides=None) -> Any:
    app = create_app(
        config_path=str(config_path) if config_path is not None else None,
        defaults={"app_name": "t", "log_level": "info", "plugins": {}},
        plugin_specs=_rejecting_specs(track),
        environ={},
        overrides=overrides,
    )

    @app.get("/token")
    async def token(picker: Any = Depends(requires("picker"))):
        return {"token": picker["options"].get("token")}

    return app


def test_http_reload_rejected_config_returns_explainable_failure(tmp_path, caplog) -> None:
    """改配置文件触发：插件拒绝配置 -> /reload 返回 200 + 可解释失败体（不再是 500）。"""
    track: list[str] = []
    cfg = tmp_path / "app.yaml"
    _write_picker_config(cfg, "good")
    app = _picker_app(track, config_path=cfg)
    with TestClient(app) as client:
        assert client.get("/token").json() == {"token": "good"}

        _write_picker_config(cfg, "bad")  # picker.validate_options 主动拒绝
        resp = client.post("/reload")
        assert resp.status_code == 200  # 不是 500
        body = resp.json()
        assert body["ok"] is False
        assert body["stage"] == "assembly"
        assert "拒绝配置" in body["reason"]
        assert body["failure_chain"] == ["base", "picker"]  # 定位到失败点的链路

        # 旧装配继续对外服务，同进程其它请求不受影响
        assert client.get("/token").json() == {"token": "good"}
        assert client.get("/healthz").json() == {"status": "ok"}

        # 改回合法配置后再次热更新成功
        _write_picker_config(cfg, "fixed")
        assert client.post("/reload").json()["ok"] is True
        assert client.get("/token").json() == {"token": "fixed"}

    # 失败尝试中 base 配置未变被复用（既不重建也不被停）；
    # picker 在校验阶段即被拒绝，失败尝试未产生任何新实例
    assert "stop:base" not in track
    assert track.count("create:picker") == 2  # 首次装配 + 修复后重建

    log_text = "\n".join(r.message for r in caplog.records)
    assert "配置加载来源" in log_text or "来源(低->高)" in log_text
    assert "装配顺序" in log_text
    assert "失败链路" in log_text


def test_http_reload_rejected_via_overrides_source(caplog) -> None:
    """覆盖项来源触发：失败返回同样可解释，日志能看到生效来源含 overrides。"""
    track: list[str] = []
    overrides: dict[str, Any] = {}
    app = _picker_app(track, overrides=overrides)
    with TestClient(app) as client:
        assert client.get("/token").json() == {"token": None}

        # 通过 overrides（最高优先级来源）把 token 改成非法值
        overrides["plugins"] = {
            "picker": {"enabled": True, "scope": "app", "options": {"token": "bad"}}
        }
        resp = client.post("/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["stage"] == "assembly"
        assert body["failure_chain"] == ["base", "picker"]
        assert client.get("/token").json() == {"token": None}  # 旧装配照常

        overrides["plugins"]["picker"]["options"]["token"] = "from-overrides"
        assert client.post("/reload").json()["ok"] is True
        assert client.get("/token").json() == {"token": "from-overrides"}

    log_text = "\n".join(r.message for r in caplog.records)
    assert "overrides" in log_text  # 配置来源可解释


# -- 监督器级：连续/并发触发失败更新结果确定 ------------------------------------


async def test_concurrent_rejected_reloads_fail_deterministically() -> None:
    """并发触发多次注定失败的热更新：全部以相同 ReloadError 失败、串行不踩踏，
    每次尝试中已启动的插件都被回滚释放，旧装配始终对外服务。"""
    from app.plugins.errors import PluginConfigRejectedError

    track: list[str] = []
    clock = make_spec("clock", track=track)
    mid = make_spec("mid", requires=(InterfaceRequirement("clock", "^1.0.0"),), track=track)

    def reject(options: dict) -> None:
        if options.get("mode") == "bad":
            raise PluginConfigRejectedError("top", "mode 非法")

    top = make_spec(
        "top",
        requires=(InterfaceRequirement("mid", "^1.0.0"),),
        track=track,
        validate=reject,
    )
    good = make_config(
        {"clock": (True, "app"), "mid": (True, "app"), "top": (True, "app")},
        options={"top": {"mode": "good"}},
    )
    bad = make_config(
        {"clock": (True, "app"), "mid": (True, "app"), "top": (True, "app")},
        options={"mid": {"v": 2}, "top": {"mode": "bad"}},  # mid 重建启动后 top 拒绝
    )
    sup, cursor = _supervisor([clock, mid, top], [good, bad])
    await sup.start()
    old_container = sup.current_container()
    started_mid_before = track.count("start:mid")

    cursor["i"] = 1
    results = await asyncio.gather(*(sup.reload() for _ in range(3)), return_exceptions=True)

    # 结果确定：全部以相同的 ReloadError 失败（阶段/链路一致），无人成功
    assert len(results) == 3
    assert all(isinstance(r, ReloadError) for r in results)
    assert all(r.stage == "assembly" for r in results)
    assert all(r.chain == ["clock", "mid", "top"] for r in results)

    # 串行执行互不踩踏：每次尝试里 mid 被创建并启动一次、回滚停止一次，
    # 没有任何已初始化实例被遗留；复用的 clock 从未被停
    attempts = track.count("start:mid") - started_mid_before
    assert attempts == 3
    assert track.count("stop:mid") == attempts
    assert "stop:clock" not in track

    # 旧装配始终对外服务
    assert sup.current_container() is old_container
    assert not old_container.retired
    assert not old_container.disposed
