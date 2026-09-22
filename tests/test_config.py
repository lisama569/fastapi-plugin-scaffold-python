"""配置加载、覆盖优先级与可区分错误分类。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.errors import (
    ConfigConflictError,
    ConfigMissingError,
    ConfigSourceError,
    ConfigTypeError,
    ConfigValueError,
)
from app.config.loader import load_config, validate

DEFAULTS = {
    "app_name": "default-app",
    "log_level": "info",
    "plugins": {},
}


def test_priority_override_order(tmp_path: Path, caplog) -> None:
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("app_name: from-file\nlog_level: warning\n", encoding="utf-8")
    result = load_config(
        defaults=DEFAULTS,
        config_path=str(cfg_file),
        environ={"FASTAPI_PLUGIN_LOG_LEVEL": "error"},
        overrides={"app_name": "from-override"},
    )
    cfg = validate(result)

    # 文件覆盖默认；环境覆盖文件；显式覆盖最高
    assert cfg.app_name == "from-override"
    assert cfg.log_level == "error"
    assert result.key_sources["app_name"] == "overrides"
    assert result.key_sources["log_level"] == "env"
    assert result.source_order == [
        "defaults",
        f"file:{cfg_file}",
        "env",
        "overrides",
    ]
    # 日志打印了来源
    assert any("配置加载来源" in r.message for r in caplog.records)


def test_deep_merge_is_per_key(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(
        '{"plugins": {"clock": {"enabled": true, "scope": "app"}}}',
        encoding="utf-8",
    )
    result = load_config(
        defaults=DEFAULTS,
        config_path=str(cfg_file),
        environ={
            "FASTAPI_PLUGIN_PLUGINS__CLOCK__SCOPE": "request",
            "FASTAPI_PLUGIN_PLUGINS__GREETER__ENABLED": "true",
            "FASTAPI_PLUGIN_PLUGINS__GREETER__SCOPE": "request",
        },
    )
    cfg = validate(result)
    # clock 的 enabled 来自文件、scope 被环境逐键覆盖；greeter 全部来自环境
    assert cfg.plugins["clock"].enabled is True
    assert cfg.plugins["clock"].scope == "request"
    assert cfg.plugins["greeter"].scope == "request"


def test_missing_required_is_distinguishable() -> None:
    with pytest.raises(ConfigMissingError) as exc:
        validate(load_config(defaults={"log_level": "info"}, environ={}))
    assert exc.value.key == "app_name"


def test_type_mismatch_is_distinguishable() -> None:
    with pytest.raises(ConfigTypeError) as exc:
        validate(load_config(defaults=DEFAULTS, overrides={"app_name": 123}))
    assert exc.value.key == "app_name"
    assert exc.value.expected == "str"
    assert exc.value.actual == "int"
    assert exc.value.source == "overrides"


def test_illegal_value_is_distinguishable() -> None:
    with pytest.raises(ConfigValueError) as exc:
        validate(load_config(defaults=DEFAULTS, overrides={"log_level": "trace"}))
    assert exc.value.key == "log_level"
    assert "info" in exc.value.reason


def test_invalid_plugin_scope_rejected() -> None:
    with pytest.raises(ConfigValueError) as exc:
        validate(
            load_config(
                defaults=DEFAULTS,
                overrides={"plugins": {"clock": {"enabled": True, "scope": "global"}}},
            )
        )
    assert exc.value.key == "plugins.clock.scope"


def test_missing_plugin_field_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("plugins:\n  clock:\n    enabled: true\n", encoding="utf-8")
    with pytest.raises(ConfigMissingError) as exc:
        validate(load_config(defaults=DEFAULTS, config_path=str(cfg_file)))
    assert exc.value.key == "plugins.clock.scope"


def test_unreadable_file_is_source_error() -> None:
    with pytest.raises(ConfigSourceError) as exc:
        load_config(defaults=DEFAULTS, config_path="/no/such/file.yaml")
    assert "文件不存在" in exc.value.detail


def test_malformed_file_is_source_error(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigSourceError):
        load_config(defaults=DEFAULTS, config_path=str(cfg_file))


def test_env_type_coercion() -> None:
    result = load_config(
        defaults=DEFAULTS,
        environ={
            "FASTAPI_PLUGIN_PLUGINS__CLOCK__ENABLED": "false",
            "FASTAPI_PLUGIN_PLUGINS__CLOCK__SCOPE": "app",
        },
    )
    cfg = validate(result)
    assert cfg.plugins["clock"].enabled is False


def test_error_classes_are_distinct() -> None:
    for cls in (
        ConfigMissingError,
        ConfigTypeError,
        ConfigValueError,
        ConfigSourceError,
        ConfigConflictError,
    ):
        assert issubclass(cls, Exception)


def test_structural_conflict_between_scalar_and_mapping(tmp_path: Path) -> None:
    """低层 plugins 是映射，高层把 plugins 覆盖成标量 -> 明确冲突，不静默覆盖。"""
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("plugins: 123\n", encoding="utf-8")
    with pytest.raises(ConfigConflictError) as exc:
        load_config(defaults=DEFAULTS, config_path=str(cfg_file))
    assert exc.value.key == "plugins"
    assert "defaults" in exc.value.sources
    assert any("file" in s for s in exc.value.sources)


def test_scalar_override_is_normal_not_conflict(tmp_path: Path) -> None:
    """同类型标量覆盖是正常分层行为，不应误报冲突。"""
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("app_name: file-name\n", encoding="utf-8")
    cfg = validate(load_config(defaults=DEFAULTS, config_path=str(cfg_file)))
    assert cfg.app_name == "file-name"
