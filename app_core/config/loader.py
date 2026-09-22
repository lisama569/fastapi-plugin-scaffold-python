"""多来源配置加载与分层覆盖。

覆盖优先级固定为（后者覆盖前者）::

    defaults < file:<路径> < env(APP_*) < override

合并在 *叶子键* 层面进行：嵌套 dict 会被展平为点分键
（如 ``plugin_config.cache.ttl``），因此高优先级来源只覆盖同名叶子键，
不会整体替换整棵子树。每个键最终来自哪个来源会记录在
:attr:`LoadedConfig.provenance`，便于日志与断言。
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from typing import Any

from app_core.config.schema import SOURCE_PRIORITY, AppSettings, LoadedConfig
from app_core.errors import (
    ConfigFileError,
    ConfigTypeError,
    InvalidValueError,
    MissingRequiredError,
)

logger = logging.getLogger("app_core.config")

_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_TYPE_NAMES = {
    str: "string",
    bool: "bool",
    int: "int",
}

#: 叶子键 -> (python 类型, 是否必填)
_LEAF_SPECS: dict[str, tuple[type, bool]] = {
    "app_name": (str, True),
    "debug": (bool, False),
    "log_level": (str, False),
}

#: 环境变量（去掉前缀后）-> 点分配置键
_ENV_MAP = {
    "APP_NAME": "app_name",
    "DEBUG": "debug",
    "LOG_LEVEL": "log_level",
    "PLUGINS": "plugins",
}

_DEFAULTS: dict[str, object] = {
    "debug": False,
    "log_level": "INFO",
    "plugins": [],
    "plugin_config": {},
}

_TOP_LEVEL_KEYS = {"app_name", "debug", "log_level", "plugins", "plugin_config"}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, object]:
    """把嵌套 dict 展平为点分叶子键；list 视为叶子。"""
    flat: dict[str, object] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _unflatten(flat: dict[str, object]) -> dict[str, Any]:
    """点分叶子键还原为嵌套 dict。"""
    nested: dict[str, Any] = {}
    for path, value in flat.items():
        parts = path.split(".")
        node = nested
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return nested


def _read_file(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        raise ConfigFileError(path, "文件不存在")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if path.endswith(".json"):
            data = json.loads(raw)
        elif path.endswith(".toml"):
            data = tomllib.loads(raw.decode("utf-8"))
        else:
            raise ConfigFileError(path, "仅支持 .toml / .json 后缀")
    except ConfigFileError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigFileError(path, f"解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigFileError(path, "顶层必须是表/对象")
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise InvalidValueError(
            "<root>", sorted(unknown), f"存在未知配置项: {sorted(unknown)}"
        )
    return data


def _coerce_scalar(path: str, raw: str, expected: type) -> object:
    """把环境变量字符串转换为标量；无法转换时给可区分错误。"""
    if expected is bool:
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise InvalidValueError(
            path, raw, "布尔值只能是 true/false/1/0/yes/no/on/off"
        )
    if expected is int:
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigTypeError(path, raw, "int") from exc
    return raw


def _read_env(environ: dict[str, str], prefix: str) -> dict[str, object]:
    """从环境变量读取叶子键。

    - ``<prefix>APP_NAME / DEBUG / LOG_LEVEL / PLUGINS`` 为预定义标量/数组；
    - 其余以 prefix 开头的变量按 ``__`` 分隔映射到嵌套插件配置，
      例如 ``APP_PLUGIN_CONFIG__CACHE__TTL=30`` ->
      ``plugin_config.cache.ttl``；
    - plugins 数组使用逗号分隔。
    """
    out: dict[str, object] = {}
    known_env_keys = {prefix + name for name in _ENV_MAP}
    for raw_key, raw_value in environ.items():
        if not raw_key.startswith(prefix):
            continue
        if raw_key in known_env_keys:
            path = _ENV_MAP[raw_key[len(prefix):]]
            if path == "plugins":
                out[path] = [item.strip() for item in raw_value.split(",") if item.strip()]
            else:
                expected = _LEAF_SPECS.get(path, (str, False))[0]
                out[path] = _coerce_scalar(path, raw_value, expected)
        else:
            # 仅允许写入 plugin_config.* 命名空间，避免环境变量误改顶层键。
            suffix = raw_key[len(prefix):]
            path = suffix.lower().replace("__", ".")
            if not path.startswith("plugin_config."):
                raise InvalidValueError(
                    raw_key, raw_value, "未识别的环境变量配置键"
                )
            out[path] = raw_value
    return out


def _check_type(path: str, value: object, expected: type) -> None:
    # bool 是 int 的子类，需先排除。
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigTypeError(path, value, _TYPE_NAMES[bool])
    elif expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigTypeError(path, value, _TYPE_NAMES[int])
    elif not isinstance(value, expected):
        raise ConfigTypeError(path, value, _TYPE_NAMES.get(expected, expected.__name__))


def _validate(flat: dict[str, object]) -> None:
    """对合并后的叶子键做必填、类型、取值合法性校验。"""
    if "app_name" not in flat:
        raise MissingRequiredError("app_name", "string")

    # 顶层标量
    for path, (expected, _required) in _LEAF_SPECS.items():
        if path in flat:
            _check_type(path, flat[path], expected)
    value = flat.get("log_level")
    if isinstance(value, str) and value.upper() not in _VALID_LOG_LEVELS:
        raise InvalidValueError(
            "log_level", value, f"合法取值为 {list(_VALID_LOG_LEVELS)}"
        )

    # plugins 数组
    if "plugins" in flat:
        plugins = flat["plugins"]
        if not isinstance(plugins, list | tuple) or isinstance(plugins, bool):
            raise ConfigTypeError("plugins", plugins, "array[string]")
        for idx, item in enumerate(plugins):
            if not isinstance(item, str):
                raise ConfigTypeError(f"plugins[{idx}]", item, "string")
            if not item.strip():
                raise InvalidValueError(f"plugins[{idx}]", item, "插件名不能为空")

    # plugin_config：键必须为字符串，值必须落在 table 命名空间（展平后以点号出现）
    plugin_names: set[str] = set()
    for path, value in flat.items():
        if not path.startswith("plugin_config."):
            continue
        parts = path.split(".")
        if len(parts) < 3 or not parts[1]:
            raise InvalidValueError(path, value, "plugin_config 下必须以插件名为键")
        plugin_names.add(parts[1])
        if isinstance(value, dict):  # pragma: no cover - 展平后不应出现
            raise ConfigTypeError(path, value, "叶子值")


def load_settings(
    *,
    file_path: str | None = None,
    environ: dict[str, str] | None = None,
    env_prefix: str = "APP_",
    overrides: dict[str, object] | None = None,
) -> LoadedConfig:
    """加载并校验配置，返回值与来源溯源。

    :param file_path: 可选的 TOML/JSON 配置文件路径。
    :param environ: 环境变量映射（测试可注入），默认取 ``os.environ``。
    :param env_prefix: 环境变量前缀，默认 ``APP_``。
    :param overrides: 代码内显式覆盖，优先级最高。
    :raises MissingRequiredError: 必填的 ``app_name`` 缺失。
    :raises ConfigTypeError: 类型不匹配。
    :raises InvalidValueError: 取值非法或存在未知键。
    :raises ConfigFileError: 文件不存在或解析失败。
    """
    env = os.environ if environ is None else environ

    # 1) 收集各层（保持固定优先级顺序）
    layers: dict[str, dict[str, object]] = {
        "defaults": _flatten(_DEFAULTS),
    }
    if file_path is not None:
        layers[f"file:{file_path}"] = _flatten(_read_file(file_path))
    layers["env"] = _flatten(_read_env(env, env_prefix))
    layers["override"] = _flatten(dict(overrides or {}))

    # 2) 按固定优先级逐层覆盖（后者胜出）
    merged: dict[str, object] = {}
    provenance: dict[str, str] = {}
    for layer_name in ("defaults", f"file:{file_path}" if file_path else None,
                       "env", "override"):
        if layer_name is None:
            continue
        for path, value in layers[layer_name].items():
            merged[path] = value
            provenance[path] = layer_name

    # 3) 校验（错误绝不静默回退默认值）
    _validate(merged)

    nested = _unflatten(merged)
    plugin_config = {
        name: dict(values)
        for name, values in nested.get("plugin_config", {}).items()
    }
    settings = AppSettings(
        app_name=nested["app_name"],
        debug=nested.get("debug", False),
        log_level=nested.get("log_level", "INFO").upper(),
        plugins=tuple(nested.get("plugins", ())),
        plugin_config=plugin_config,
    )

    logger.info("配置加载完成，来源优先级: %s", list(SOURCE_PRIORITY))
    for path in sorted(provenance):
        logger.info("配置项 %-40s <- %s", path, provenance[path])

    return LoadedConfig(settings=settings, provenance=provenance, layers=layers)
