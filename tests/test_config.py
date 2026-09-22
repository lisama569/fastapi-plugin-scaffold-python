"""配置加载：正常分层覆盖 + 边界异常场景。"""

from __future__ import annotations

import logging

import pytest

from app_core.config.loader import load_settings
from app_core.errors import (
    ConfigFileError,
    ConfigTypeError,
    InvalidValueError,
    MissingRequiredError,
)

logger = logging.getLogger("test.config")


def test_layered_override_priority_is_fixed_and_recorded(tmp_path, caplog):
    cfg = tmp_path / "a.toml"
    cfg.write_text(
        'app_name = "from-file"\n'
        'debug = false\n'
        'log_level = "warning"\n'
        'plugins = ["cache"]\n'
        '[plugin_config.cache]\n'
        'ttl = 10\n'
        'host = "file-host"\n',
        encoding="utf-8",
    )
    env = {
        "APP_APP_NAME": "from-env",
        "APP_PLUGIN_CONFIG__CACHE__TTL": "30",  # env 只覆盖单个叶子
    }
    overrides = {"log_level": "error", "plugin_config": {"cache": {"ttl": 99}}}

    with caplog.at_level(logging.INFO, logger="app_core.config"):
        result = load_settings(
            file_path=str(cfg), environ=env, overrides=overrides
        )

    s = result.settings
    assert s.app_name == "from-env"          # env 覆盖 file
    assert s.log_level == "ERROR"            # override 覆盖 file
    assert s.debug is False                  # 来自文件
    assert s.plugins == ("cache",)
    # 叶子级覆盖：host 仍来自文件，ttl 最终来自 override（压过 env、file）
    assert s.plugin_config["cache"]["host"] == "file-host"
    assert s.plugin_config["cache"]["ttl"] == 99
    assert result.provenance["app_name"] == "env"
    assert result.provenance["log_level"] == "override"
    assert result.provenance["plugin_config.cache.ttl"] == "override"
    assert result.provenance["plugin_config.cache.host"].startswith("file:")
    logger.info("配置来源: %s", result.provenance)


def test_defaults_apply_only_to_optional_keys():
    result = load_settings(
        environ={}, overrides={"app_name": "svc"}
    )
    assert result.settings.debug is False
    assert result.settings.log_level == "INFO"
    assert result.provenance["debug"] == "defaults"


def test_missing_required_app_name_is_rejected_not_defaulted():
    with pytest.raises(MissingRequiredError) as exc:
        load_settings(environ={}, overrides={})
    assert exc.value.key == "app_name"
    assert "app_name" in str(exc.value)


def test_type_mismatch_is_distinguishable(tmp_path):
    cfg = tmp_path / "bad.toml"
    cfg.write_text('app_name = 123\n', encoding="utf-8")
    with pytest.raises(ConfigTypeError) as exc:
        load_settings(file_path=str(cfg), environ={})
    assert exc.value.key == "app_name"
    assert exc.value.expected_type == "string"


def test_invalid_enum_value_is_distinguishable():
    with pytest.raises(InvalidValueError) as exc:
        load_settings(
            environ={},
            overrides={"app_name": "svc", "log_level": "VERBOSE"},
        )
    assert exc.value.key == "log_level"
    assert "合法取值" in str(exc.value)


def test_env_bool_invalid_value_is_rejected():
    with pytest.raises(InvalidValueError) as exc:
        load_settings(environ={"APP_DEBUG": "maybe"}, overrides={"app_name": "x"})
    assert exc.value.key == "debug"


def test_plugins_must_be_string_array():
    with pytest.raises(ConfigTypeError) as exc:
        load_settings(
            environ={},
            overrides={"app_name": "x", "plugins": ["ok", 3]},
        )
    assert exc.value.key == "plugins[1]"


def test_unknown_top_level_key_is_rejected(tmp_path):
    cfg = tmp_path / "unknown.toml"
    cfg.write_text('app_name = "x"\nstrange = 1\n', encoding="utf-8")
    with pytest.raises(InvalidValueError):
        load_settings(file_path=str(cfg), environ={})


def test_unknown_env_key_outside_plugin_namespace_is_rejected():
    with pytest.raises(InvalidValueError) as exc:
        load_settings(environ={"APP_SURPRISE": "1"}, overrides={"app_name": "x"})
    assert "未识别的环境变量配置键" in str(exc.value)


def test_missing_file_raises_distinguishable_error():
    with pytest.raises(ConfigFileError) as exc:
        load_settings(file_path="/no/such/config.toml", environ={})
    assert exc.value.path == "/no/such/config.toml"


def test_broken_json_file_raises_config_file_error(tmp_path):
    cfg = tmp_path / "broken.json"
    cfg.write_text('{"app_name": ', encoding="utf-8")
    with pytest.raises(ConfigFileError):
        load_settings(file_path=str(cfg), environ={})


def test_env_nested_plugin_config_and_csv_plugins():
    result = load_settings(
        environ={
            "APP_APP_NAME": "env-svc",
            "APP_PLUGINS": "cache, queue",
            "APP_PLUGIN_CONFIG__CACHE__TTL": "42",
        },
        overrides={},
    )
    assert result.settings.plugins == ("cache", "queue")
    assert result.settings.plugin_config["cache"]["ttl"] == "42"
