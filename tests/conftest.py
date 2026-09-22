"""pytest 公共装置与日志配置。

所有测试统一打开 app 命名空间的 DEBUG 日志，单测输出中即可看到
配置来源、逐键来源、装配顺序与失败链路。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.config.model import AppConfig, PluginConfig
from app.plugins.contract import (
    InterfaceRequirement,
    PluginContext,
    PluginManifest,
    PluginSpec,
)


@pytest.fixture(autouse=True)
def _app_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger="app")
    return caplog


def make_spec(
    name: str,
    *,
    version: str = "1.0.0",
    provides: tuple[str, ...] | None = None,
    requires: tuple[InterfaceRequirement, ...] = (),
    scope: str = "app",
    options: dict[str, Any] | None = None,
    fail_factory: bool = False,
    fail_message: str | None = None,
    start_fail: bool = False,
    track: list[str] | None = None,
    instance_factory: Any = None,
) -> PluginSpec:
    """构造一个可追踪生命周期的测试插件规格。"""

    provides = provides if provides is not None else (name,)

    def factory(ctx: PluginContext) -> Any:
        if fail_factory:
            raise RuntimeError(fail_message or f"{name} 工厂失败")
        if track is not None:
            track.append(f"create:{name}")
        if instance_factory is not None:
            return instance_factory(ctx)
        return {"name": name, "options": ctx.options, "deps": dict(ctx.dependencies)}

    async def on_start(instance: Any) -> None:
        if start_fail:
            raise RuntimeError(fail_message or f"{name} 启动失败")
        if track is not None:
            track.append(f"start:{name}")

    async def on_stop(instance: Any) -> None:
        if track is not None:
            track.append(f"stop:{name}")

    return PluginSpec(
        manifest=PluginManifest(
            name=name,
            version=version,
            provides=provides,
            requires=requires,
            scope=scope,  # type: ignore[arg-type]
        ),
        factory=factory,
        on_start=on_start,
        on_stop=on_stop,
    )


def make_config(
    plugins: dict[str, tuple[bool, str]] | None = None,
    *,
    options: dict[str, dict[str, Any]] | None = None,
    app_name: str = "test-app",
    source_order: tuple[str, ...] = ("defaults",),
) -> AppConfig:
    """快速构造测试用 AppConfig。plugins: {name: (enabled, scope)}。"""
    opts = options or {}
    plugin_cfgs = {
        name: PluginConfig(enabled=enabled, scope=scope, options=opts.get(name, {}))  # type: ignore[arg-type]
        for name, (enabled, scope) in (plugins or {}).items()
    }
    return AppConfig(
        app_name=app_name,
        log_level="info",
        plugins=plugin_cfgs,
        source_order=source_order,
    )
