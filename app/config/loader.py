"""多来源配置加载、分层合并与校验。

固定优先级（数值越大优先级越高，高优先级逐键覆盖低优先级）：

    1. defaults      内置默认          priority=10
    2. file          配置文件(yaml/json) priority=20
    3. env           环境变量           priority=30
    4. overrides     编程式显式覆盖      priority=40

合并方式为逐键深度合并（字典递归，标量整键替换）。每个叶子键最终从哪个
来源生效会被记录在 ``LoadResult.key_sources`` 中，保证覆盖链路可解释。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .errors import (
    ConfigConflictError,
    ConfigMissingError,
    ConfigSourceError,
    ConfigTypeError,
    ConfigValueError,
)
from .model import AppConfig, ConfigSource, FieldSpec, LoadResult, PluginConfig

logger = logging.getLogger("app.config")

DEFAULT_SOURCE_NAME = "defaults"
FILE_SOURCE_NAME = "file"
ENV_SOURCE_NAME = "env"
OVERRIDES_SOURCE_NAME = "overrides"

SOURCE_PRIORITY: dict[str, int] = {
    DEFAULT_SOURCE_NAME: 10,
    FILE_SOURCE_NAME: 20,
    ENV_SOURCE_NAME: 30,
    OVERRIDES_SOURCE_NAME: 40,
}

ENV_PREFIX = "FASTAPI_PLUGIN_"

_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
_SCOPES = ("app", "request")

# 顶层强类型字段规格；plugins 子结构单独校验。
REQUIRED_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(key="app_name", expected_type=str),
    FieldSpec(
        key="log_level",
        expected_type=str,
        choices=_LOG_LEVELS,
    ),
)


def build_sources(
    *,
    defaults: dict[str, Any],
    config_path: str | None = None,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> list[ConfigSource]:
    """按固定优先级构造配置来源列表（低 -> 高）。"""
    sources: list[ConfigSource] = []
    sources.append(
        ConfigSource(DEFAULT_SOURCE_NAME, SOURCE_PRIORITY[DEFAULT_SOURCE_NAME], defaults)
    )
    if config_path is not None:
        path_name = f"{FILE_SOURCE_NAME}:{config_path}"
        sources.append(
            ConfigSource(
                path_name,
                SOURCE_PRIORITY[FILE_SOURCE_NAME],
                read_file_source(config_path),
            )
        )
    env_data = read_env_source(os.environ if environ is None else environ)
    sources.append(ConfigSource(ENV_SOURCE_NAME, SOURCE_PRIORITY[ENV_SOURCE_NAME], env_data))
    if overrides:
        sources.append(
            ConfigSource(
                OVERRIDES_SOURCE_NAME,
                SOURCE_PRIORITY[OVERRIDES_SOURCE_NAME],
                overrides,
            )
        )
    return sources


def read_file_source(path: str) -> dict[str, Any]:
    """读取并解析 yaml/json 配置文件。"""
    p = Path(path)
    if not p.is_file():
        raise ConfigSourceError(path, "文件不存在或不是普通文件")
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigSourceError(path, f"解析失败: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigSourceError(path, "顶层结构必须是映射/对象")
    return data


def read_env_source(environ: dict[str, str]) -> dict[str, Any]:
    """从环境变量解析配置。

    规则：去掉前缀 ``FASTAPI_PLUGIN_``，小写化，``__`` 表示嵌套层级。
    例如 ``FASTAPI_PLUGIN_LOG_LEVEL=debug`` -> {"log_level": "debug"}；
    ``FASTAPI_PLUGIN_PLUGINS__CLOCK__ENABLED`` ->
    {"plugins": {"clock": {"enabled": ...}}}。标量值做 JSON 字面量推断，
    推断失败则按原始字符串处理。
    """
    root: dict[str, Any] = {}
    for raw_key, raw_val in environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        path = raw_key[len(ENV_PREFIX) :].lower().split("__")
        if not path or any(seg == "" for seg in path):
            raise ConfigSourceError(ENV_SOURCE_NAME, f"环境变量键路径非法: {raw_key!r}")
        node = root
        for seg in path[:-1]:
            node = node.setdefault(seg, {})
            if not isinstance(node, dict):
                raise ConfigConflictError(
                    ".".join(path), [ENV_SOURCE_NAME], "环境变量嵌套路径与已有键冲突"
                )
        node[path[-1]] = _coerce_env_value(raw_val)
    return root


def _coerce_env_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return raw


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """以 over 为高层逐键深度合并到 base，返回新字典。

    若同一键在两层分别是“映射”和“非映射”，属于结构性冲突，
    抛 ConfigConflictError 而不是静默用某一方覆盖。
    """
    result = dict(base)
    for key, value in over.items():
        if key in result:
            old = result[key]
            if isinstance(old, dict) and isinstance(value, dict):
                result[key] = deep_merge(old, value)
            elif isinstance(old, dict) != isinstance(value, dict):
                raise ConfigConflictError(
                    key,
                    ["<lower>", "<higher>"],
                    f"低层为 {type(old).__name__}，高层为 {type(value).__name__}，结构不一致",
                )
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def load_config(
    *,
    defaults: dict[str, Any],
    config_path: str | None = None,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> LoadResult:
    """加载并按优先级深度合并所有来源，记录逐键来源。"""
    sources = build_sources(
        defaults=defaults,
        config_path=config_path,
        environ=environ,
        overrides=overrides,
    )
    logger.info("配置加载来源(低->高): %s", [s.name for s in sources])

    merged: dict[str, Any] = {}
    key_sources: dict[str, str] = {}
    lower_name = sources[0].name if sources else "<merged>"
    for source in sources:
        try:
            merged = deep_merge(merged, source.data)
        except ConfigConflictError as exc:
            raise ConfigConflictError(exc.key, [lower_name, source.name], exc.detail) from exc
        for leaf in _leaves("", source.data):
            key_sources[leaf] = source.name
        lower_name = source.name
    return LoadResult(
        merged=merged,
        key_sources=key_sources,
        source_order=[s.name for s in sources],
    )


def _leaves(prefix: str, data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            out.extend(_leaves(path, value))
        else:
            out.append(path)
    return out


def validate(result: LoadResult) -> AppConfig:
    """对合并结果做强类型校验，非法时抛出可区分的配置错误。"""
    merged = result.merged
    source_of = result.key_sources

    for spec in REQUIRED_FIELDS:
        path = spec.key
        if path not in merged:
            if spec.required and spec.default is None:
                raise ConfigMissingError(path)
            merged[path] = spec.default
        value = merged[path]
        # 严格类型匹配（bool 不应被当作 str 之外的类型，这里逐字段显式判定）
        if not isinstance(value, spec.expected_type) or (
            spec.expected_type is not bool and isinstance(value, bool)
        ):
            raise ConfigTypeError(
                path,
                spec.expected_type.__name__,
                type(value).__name__,
                source_of.get(path, "<merged>"),
            )
        if spec.choices is not None and value not in spec.choices:
            raise ConfigValueError(
                path,
                value,
                f"取值必须是 {list(spec.choices)} 之一",
                source_of.get(path, "<merged>"),
            )

    plugins_raw = merged.get("plugins", {})
    if not isinstance(plugins_raw, dict):
        raise ConfigTypeError(
            "plugins", "dict", type(plugins_raw).__name__, source_of.get("plugins", "<merged>")
        )
    plugins: dict[str, PluginConfig] = {}
    for name, conf in plugins_raw.items():
        prefix = f"plugins.{name}"

        def src(leaf: str, _prefix: str = prefix) -> str:
            return source_of.get(f"{_prefix}.{leaf}", "<merged>")

        if not isinstance(conf, dict):
            raise ConfigTypeError(prefix, "dict", type(conf).__name__, "<merged>")
        if "enabled" not in conf:
            raise ConfigMissingError(f"{prefix}.enabled")
        if not isinstance(conf["enabled"], bool):
            raise ConfigTypeError(
                f"{prefix}.enabled", "bool", type(conf["enabled"]).__name__, src("enabled")
            )
        if "scope" not in conf:
            raise ConfigMissingError(f"{prefix}.scope")
        if not isinstance(conf["scope"], str) or conf["scope"] not in _SCOPES:
            raise ConfigValueError(
                f"{prefix}.scope",
                conf.get("scope"),
                f"取值必须是 {list(_SCOPES)} 之一",
                src("scope"),
            )
        options = conf.get("options", {})
        if not isinstance(options, dict):
            raise ConfigTypeError(
                f"{prefix}.options", "dict", type(options).__name__, src("options")
            )
        plugins[name] = PluginConfig(enabled=conf["enabled"], scope=conf["scope"], options=options)

    cfg = AppConfig(
        app_name=merged["app_name"],
        log_level=merged["log_level"],
        plugins=plugins,
        raw=merged,
        source_order=tuple(result.source_order),
        key_sources=dict(result.key_sources),
    )
    logger.info(
        "配置校验通过: app_name=%r log_level=%r 插件=%s",
        cfg.app_name,
        cfg.log_level,
        {n: (p.enabled, p.scope) for n, p in cfg.plugins.items()},
    )
    return cfg
