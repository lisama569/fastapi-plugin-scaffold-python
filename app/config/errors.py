"""配置加载与校验的错误分类。

每一类错误都携带：所在配置来源、配置键路径、可直接定位的人类可读说明。
错误互不重叠，调用方可按异常类型区分失败原因。
"""

from __future__ import annotations


class ConfigError(Exception):
    """所有配置相关错误的基类。"""


class ConfigSourceError(ConfigError):
    """配置来源本身不可读或格式非法（文件缺失/解析失败等）。"""

    def __init__(self, source: str, detail: str) -> None:
        self.source = source
        self.detail = detail
        super().__init__(f"配置来源不可用 [{source}]: {detail}")


class ConfigMissingError(ConfigError):
    """必填配置项缺失。"""

    def __init__(self, key: str, source: str = "<merged>") -> None:
        self.key = key
        self.source = source
        super().__init__(f"必填配置缺失: {key} (来源: {source})")


class ConfigTypeError(ConfigError):
    """配置项类型不匹配。"""

    def __init__(self, key: str, expected: str, actual: str, source: str) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        self.source = source
        super().__init__(f"配置类型错误: {key} 期望 {expected}, 实际 {actual} (来源: {source})")


class ConfigValueError(ConfigError):
    """配置项取值非法（枚举/范围/格式约束不满足）。"""

    def __init__(self, key: str, value: object, reason: str, source: str) -> None:
        self.key = key
        self.value = value
        self.reason = reason
        self.source = source
        super().__init__(f"配置取值非法: {key}={value!r} ({reason}) (来源: {source})")


class ConfigConflictError(ConfigError):
    """多个来源在不可合并的语义上发生冲突（例如类型不一致的同名键）。"""

    def __init__(self, key: str, sources: list[str], detail: str) -> None:
        self.key = key
        self.sources = sources
        self.detail = detail
        super().__init__(f"配置冲突: {key} 在 {sources} 上冲突: {detail}")
