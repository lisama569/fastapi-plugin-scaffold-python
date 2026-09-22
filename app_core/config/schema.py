"""配置数据模型。

所有配置最终归一为不可变的 :class:`AppSettings`；每个键的取值来源
记录在 :class:`ConfigProvenance` 中，键为点分路径，值为来源名称，
来源名称固定取自
``defaults / file:<path> / env / override``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 固定的配置来源优先级（后者覆盖前者）。
#:
#: 1. ``defaults`` 内置默认（仅非必填项有默认）；
#: 2. ``file:<path>`` 配置文件（TOML/JSON）；
#: 3. ``env`` 环境变量（前缀 APP_）；
#: 4. ``override`` 代码内显式覆盖，优先级最高。
SOURCE_PRIORITY: tuple[str, ...] = ("defaults", "file", "env", "override")


@dataclass(frozen=True)
class AppSettings:
    """归一化后的应用配置。"""

    #: 应用名称（必填）。
    app_name: str
    #: 调试开关。
    debug: bool = False
    #: 日志级别，取值受限于 loader 中的合法集合。
    log_level: str = "INFO"
    #: 启用的插件名（文件中为数组）；``["*"]`` 表示全部已注册插件。
    plugins: tuple[str, ...] = ()
    #: 各插件的私有配置，键为插件名。
    plugin_config: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedConfig:
    """加载结果：配置值 + 每个键的来源溯源。"""

    settings: AppSettings
    provenance: dict[str, str]
    #: 参与合并的各来源原始数据（已展平为点分键），用于日志输出。
    layers: dict[str, dict[str, object]]
