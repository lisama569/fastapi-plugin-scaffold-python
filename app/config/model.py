"""合并后的强类型配置模型与逐键来源追踪。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LogLevel = Literal["debug", "info", "warning", "error", "critical"]
Scope = Literal["app", "request"]


@dataclass(frozen=True)
class ConfigSource:
    """单个配置来源的标识与原始数据。"""

    name: str
    priority: int
    data: dict[str, Any]


@dataclass(frozen=True)
class PluginConfig:
    """单个插件的装配参数。"""

    enabled: bool
    scope: Scope
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    """应用启动配置（校验通过后的最终视图）。"""

    app_name: str
    log_level: LogLevel
    plugins: dict[str, PluginConfig]
    raw: dict[str, Any] = field(default_factory=dict)
    source_order: tuple[str, ...] = ()
    key_sources: dict[str, str] = field(default_factory=dict)


@dataclass
class LoadResult:
    """加载结果：合并后的原始字典与每个键最终生效的来源。"""

    merged: dict[str, Any]
    key_sources: dict[str, str] = field(default_factory=dict)
    source_order: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FieldSpec:
    """必填字段的校验规格。"""

    key: str
    expected_type: type[Any]
    required: bool = True
    choices: tuple[Any, ...] | None = None
    default: Any = None
