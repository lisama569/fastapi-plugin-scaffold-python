"""错误分类定义。

错误体系分为两大类：

- :class:`ConfigError` 配置类错误（必填缺失 / 类型不符 / 取值非法）；
- :class:`PluginError` 插件类错误（重复注册 / 依赖缺失 / 循环依赖 /
  接口与版本不兼容 / 装配中途失败）。

每个错误都携带可定位的结构化信息（配置键、插件名、失败链路等），
且 ``str(err)`` 是稳定、可断言的人类可读描述。
"""

from __future__ import annotations


class ScaffoldError(Exception):
    """所有脚手架错误的基类。"""


# ---------------------------------------------------------------------------
# 配置错误
# ---------------------------------------------------------------------------


class ConfigError(ScaffoldError):
    """配置错误基类。"""


class MissingRequiredError(ConfigError):
    """必填配置在所有来源中均缺失。

    :param key: 点分配置键，例如 ``plugin_config.cache.ttl``。
    :param expected_type: 期望类型的可读名称。
    """

    def __init__(self, key: str, expected_type: str = "") -> None:
        self.key = key
        self.expected_type = expected_type
        suffix = f" (expected {expected_type})" if expected_type else ""
        super().__init__(f"缺少必填配置项: {key}{suffix}")


class ConfigTypeError(ConfigError):
    """配置值类型与声明不匹配。"""

    def __init__(self, key: str, value: object, expected_type: str) -> None:
        self.key = key
        self.value = value
        self.expected_type = expected_type
        super().__init__(
            f"配置项类型不匹配: {key} 的值 {value!r} 不是 {expected_type}"
        )


class InvalidValueError(ConfigError):
    """配置取值非法（不在允许集合内或超出范围）。"""

    def __init__(self, key: str, value: object, reason: str) -> None:
        self.key = key
        self.value = value
        self.reason = reason
        super().__init__(f"配置项取值非法: {key}={value!r} ({reason})")


class ConfigFileError(ConfigError):
    """配置文件不存在或格式无法解析。"""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"配置文件无法加载: {path} ({reason})")


# ---------------------------------------------------------------------------
# 插件错误
# ---------------------------------------------------------------------------


class PluginError(ScaffoldError):
    """插件相关错误基类。"""


class DuplicatePluginError(PluginError):
    """同一插件名被重复注册（包括发现来源与手动注册冲突）。"""

    def __init__(self, name: str, second_source: str = "") -> None:
        self.name = name
        self.second_source = second_source
        tail = f"，冲突来源: {second_source}" if second_source else ""
        super().__init__(f"插件重复注册: {name}{tail}")


class PluginNotFoundError(PluginError):
    """某个插件依赖或配置中引用了未注册的插件。"""

    def __init__(self, name: str, referenced_by: str = "") -> None:
        self.name = name
        self.referenced_by = referenced_by
        tail = f"（被 {referenced_by} 依赖）" if referenced_by else ""
        super().__init__(f"插件未注册: {name}{tail}")


class CircularDependencyError(PluginError):
    """插件依赖图存在环。

    :param cycle: 构成环的插件名序列，例如 ``["a", "b", "c", "a"]``。
    """

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = list(cycle)
        super().__init__(f"检测到插件循环依赖: {' -> '.join(cycle)}")


class IncompatiblePluginError(PluginError):
    """插件声明的接口级别或依赖版本与当前系统/其他插件不兼容。"""

    def __init__(self, plugin: str, reason: str) -> None:
        self.plugin = plugin
        self.reason = reason
        super().__init__(f"插件不兼容: {plugin} ({reason})")


class AssemblyError(PluginError):
    """装配中途失败。

    :param plugin: 启动失败的插件名。
    :param chain: 到此为止的装配顺序（含失败插件），即失败链路。
    :param rolled_back: 已执行回滚（shutdown）的插件名，按回滚顺序。
    :param cause: 插件 startup 抛出的原始异常。
    """

    def __init__(
        self,
        plugin: str,
        chain: list[str],
        rolled_back: list[str],
        cause: BaseException,
    ) -> None:
        self.plugin = plugin
        self.chain = list(chain)
        self.rolled_back = list(rolled_back)
        self.cause = cause
        super().__init__(
            "插件装配失败于 "
            f"{plugin}: {cause!r}; 失败链路: {' -> '.join(chain)}; "
            f"已回滚: [{', '.join(rolled_back) if rolled_back else '-'}]"
        )
