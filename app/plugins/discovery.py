"""插件发现：从包路径自动收集 PluginSpec。

约定：目标模块需暴露 ``PLUGINS``（PluginSpec 的可迭代对象）。
模块导入失败或属性类型不符合约定时立即报错，不静默跳过。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable

from .contract import PluginSpec
from .errors import PluginError


def discover_plugins(module_path: str) -> list[PluginSpec]:
    """导入模块并收集其 PLUGINS 属性中的插件规格。"""
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise PluginError(f"插件发现失败: 无法导入模块 {module_path!r}: {exc}") from exc
    if not hasattr(module, "PLUGINS"):
        raise PluginError(f"插件发现失败: 模块 {module_path!r} 未声明 PLUGINS")
    raw = module.PLUGINS
    if not isinstance(raw, Iterable) or isinstance(raw, PluginSpec):
        raise PluginError(
            f"插件发现失败: 模块 {module_path!r} 的 PLUGINS 必须是可迭代的 PluginSpec"
        )
    specs = list(raw)
    for item in specs:
        if not isinstance(item, PluginSpec):
            raise PluginError(
                f"插件发现失败: 模块 {module_path!r} 的 PLUGINS 含有非 PluginSpec 元素: {item!r}"
            )
    return specs


def register_all(registry: object, specs: Iterable[PluginSpec]) -> None:
    """将发现到的规格批量注册进注册表。"""
    for spec in specs:
        registry.register(spec)  # type: ignore[attr-defined]
