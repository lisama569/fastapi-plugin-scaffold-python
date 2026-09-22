"""生命周期：启动装配、关闭回滚、装配失败时应用拒绝启动。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app_core.app import create_app
from app_core.config.schema import AppSettings
from app_core.errors import AssemblyError
from app_core.plugins.registry import PluginRegistry
from plugins_example.basic import CounterPlugin, EchoPlugin
from plugins_example.failing import FailingStartupPlugin

logger = logging.getLogger("test.lifespan")


def _settings(plugins):
    return AppSettings(app_name="life", plugins=tuple(plugins))


def test_lifespan_assembles_on_startup_and_releases_on_shutdown():
    registry = PluginRegistry()
    counter = CounterPlugin()
    echo = EchoPlugin()
    registry.register_many([counter, echo])

    app = create_app(_settings(["echo"]), registry=registry)
    with TestClient(app) as client:
        order = app.state.plugin_order
        assert order == ["counter", "echo"]
        logger.info("装配顺序: %s", order)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["plugins"] == ["counter", "echo"]
        assert counter in registry.all()  # 插件仍在注册表
    # with 退出 => lifespan 关闭：实例已撤销
    assert not app.state.container.has("counter")
    assert not app.state.container.has("echo")


def test_startup_failure_aborts_app_and_rolls_back():
    registry = PluginRegistry()
    counter = CounterPlugin()
    registry.register(counter)
    registry.register(FailingStartupPlugin())
    app = create_app(_settings(["failing"]), registry=registry)

    with pytest.raises(AssemblyError) as exc:
        with TestClient(app):
            pass

    err = exc.value
    assert err.chain == ["counter", "failing"]
    assert err.rolled_back == ["counter"]
    logger.info("启动失败链路: %s, 回滚: %s", err.chain, err.rolled_back)
    assert counter.shutdown_called is True
    # 装配在设置 container 之前失败，应用不应暴露半成品容器
    assert "container" not in app.state._state
