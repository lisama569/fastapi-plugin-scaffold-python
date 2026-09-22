"""pytest 公共夹具与日志配置。

单测日志需要能看到：配置来源、装配顺序与失败链路，这里统一打开
INFO 级别日志（caplog 与 pytest -o log_cli 可直接观察）。
"""

from __future__ import annotations

import logging
from typing import Sequence

import pytest

from app_core.config.schema import AppSettings
from app_core.plugins.assembler import Assembler
from app_core.plugins.container import Container
from app_core.plugins.contracts import BasePlugin, PluginMetadata, Requirement
from app_core.plugins.registry import PluginRegistry


@pytest.fixture(autouse=True)
def _enable_core_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app_core")


@pytest.fixture
def make_settings():
    def _make(
        plugins: Sequence[str] = (),
        plugin_config: dict[str, dict[str, object]] | None = None,
        *,
        app_name: str = "test-app",
    ) -> AppSettings:
        return AppSettings(
            app_name=app_name,
            plugins=tuple(plugins),
            plugin_config=plugin_config or {},
        )

    return _make


class _SimplePlugin(BasePlugin):
    """测试用可配置插件。"""

    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        requires: Sequence[Requirement] = (),
        api_level: int = 1,
        scope=None,
        startup_error: Exception | None = None,
    ) -> None:
        kwargs: dict[str, object] = {
            "name": name,
            "version": version,
            "requires": tuple(requires),
            "api_level": api_level,
        }
        if scope is not None:
            kwargs["scope"] = scope
        self.metadata = PluginMetadata(**kwargs)  # type: ignore[arg-type]
        self._startup_error = startup_error
        self.shutdown_called = False

    async def startup(self, ctx):  # noqa: ANN001
        if self._startup_error is not None:
            raise self._startup_error
        return f"{self.metadata.name}-instance"

    async def shutdown(self, ctx) -> None:  # noqa: ANN001
        self.shutdown_called = True


@pytest.fixture
def simple_plugin_cls():
    return _SimplePlugin


@pytest.fixture
def make_registry(simple_plugin_cls):
    def _make(specs: Sequence[dict]) -> PluginRegistry:
        registry = PluginRegistry()
        for spec in specs:
            registry.register(simple_plugin_cls(**spec))
        return registry

    return _make


@pytest.fixture
def assemble():
    async def _run(registry: PluginRegistry, settings: AppSettings) -> tuple[Container, Assembler, list[str]]:
        assembler = Assembler(registry, settings)
        container = Container()
        order = await assembler.assemble(container)
        return container, assembler, order

    return _run
