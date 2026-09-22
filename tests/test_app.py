"""HTTP 集成：配置驱动装配、并发共享单例、请求级隔离。"""

from __future__ import annotations

import concurrent.futures
import logging

import pytest
from fastapi.testclient import TestClient

from app_core.app import create_app
from app_core.config.schema import AppSettings
from app_core.errors import (
    ConfigTypeError,
    MissingRequiredError,
    PluginNotFoundError,
)

logger = logging.getLogger("test.app")


def _client(*, settings=None, discover=True):
    app = create_app(settings=settings, discover=discover)
    return TestClient(app), app


def test_app_created_from_config_file(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        'app_name = "file-app"\nplugins = ["counter"]\n'
        '[plugin_config.counter]\nstart = 5\n',
        encoding="utf-8",
    )
    # 端到端：create_app 内部按文件加载配置并据此装配
    app = create_app(config_file=str(cfg), discover=True)
    with TestClient(app) as client:
        assert client.get("/health").json()["app"] == "file-app"
        assert client.get("/counter/incr").json()["value"] == 6
    logger.info("配置来源: %s", app.state.config_provenance)


def test_concurrent_http_requests_share_one_app_singleton():
    settings = AppSettings(
        app_name="conc",
        plugins=("counter", "session"),
        plugin_config={"counter": {"start": 0}},
    )
    app = create_app(settings, discover=True)

    with TestClient(app) as client:
        def hit(_: int) -> dict:
            return client.get("/counter/incr").json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(hit, range(200)))

    values = sorted(row["value"] for row in results)
    assert values == list(range(1, 201))               # 同一单例上 200 次自增
    assert {row["init_count"] for row in results} == {1}
    # 请求级实例：每次请求新建，session_id 不会只有一个
    assert len({row["session_id"] for row in results}) > 1
    logger.info("200 并发请求完成，单例 init_count 恒为 1")


def test_unknown_plugin_in_config_makes_startup_fail():
    settings = AppSettings(app_name="bad", plugins=("nope",))
    app = create_app(settings)  # 不发现 -> 注册表为空
    with pytest.raises(PluginNotFoundError) as exc:
        with TestClient(app):
            pass
    assert exc.value.name == "nope"


def test_factory_validates_required_config():
    with pytest.raises(MissingRequiredError):
        create_app(overrides={})


def test_factory_rejects_type_mismatch(tmp_path):
    cfg = tmp_path / "bad.toml"
    cfg.write_text('app_name = 999\n', encoding="utf-8")
    with pytest.raises(ConfigTypeError):
        create_app(config_file=str(cfg))
