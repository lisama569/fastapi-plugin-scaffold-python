"""插件注册、发现与装配阶段的错误分类。

每类错误均明确携带插件名、依赖链等定位信息；全部在启动阶段抛出，
不允许把问题推迟到请求运行时。
"""

from __future__ import annotations


class PluginError(Exception):
    """所有插件相关错误的基类。"""


class DuplicatePluginError(PluginError):
    """同名插件重复注册，或同一接口被多个插件重复提供。"""

    def __init__(self, plugin: str, reason: str) -> None:
        self.plugin = plugin
        self.reason = reason
        super().__init__(f"重复注册: 插件 {plugin!r}: {reason}")


class PluginNotFoundError(PluginError):
    """依赖的插件/接口不存在。"""

    def __init__(self, plugin: str, required_by: str | None = None) -> None:
        self.plugin = plugin
        self.required_by = required_by
        who = f" (被 {required_by!r} 依赖)" if required_by else ""
        super().__init__(f"插件/接口不存在: {plugin!r}{who}")


class CircularDependencyError(PluginError):
    """插件依赖图中存在环。"""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        chain = " -> ".join(cycle)
        super().__init__(f"检测到循环依赖: {chain}")


class VersionIncompatibleError(PluginError):
    """插件声明的接口版本与消费者要求的版本区间不兼容。"""

    def __init__(
        self,
        consumer: str,
        interface: str,
        required: str,
        provider_version: str,
    ) -> None:
        self.consumer = consumer
        self.interface = interface
        self.required = required
        self.provider_version = provider_version
        super().__init__(
            f"版本不兼容: {consumer!r} 需要 {interface} {required}, "
            f"但提供者版本为 {provider_version}"
        )


class PluginConfigRejectedError(PluginError):
    """插件自身在启动前校验配置时拒绝了配置。"""

    def __init__(self, plugin: str, reason: str) -> None:
        self.plugin = plugin
        self.reason = reason
        super().__init__(f"插件 {plugin!r} 拒绝配置: {reason}")


class AssemblyError(PluginError):
    """装配中途失败；携带装配顺序、已完成步骤与可定位的失败链路。"""

    def __init__(
        self,
        plugin: str,
        reason: str,
        chain: list[str],
        assembled: list[str],
        rolled_back: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.plugin = plugin
        self.reason = reason
        self.chain = chain
        self.assembled = assembled
        self.rolled_back = rolled_back or []
        self.cause = cause
        super().__init__(
            f"装配失败于 {plugin!r}: {reason} | 依赖链: {' -> '.join(chain)} "
            f"| 已完成: {assembled} | 已回滚: {self.rolled_back}"
        )
