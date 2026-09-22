"""示例插件包：暴露 PLUGINS 供自动发现。"""

from __future__ import annotations

from ..plugins.contract import PluginSpec
from .clock import CLOCK_SPEC
from .greeter import GREETER_SPEC

PLUGINS: list[PluginSpec] = [CLOCK_SPEC, GREETER_SPEC]

__all__ = ["PLUGINS", "CLOCK_SPEC", "GREETER_SPEC"]
