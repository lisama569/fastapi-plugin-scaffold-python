"""示例：请求级 greeter 插件，依赖应用级 clock。"""

from __future__ import annotations

from ..plugins.contract import (
    InterfaceRequirement,
    PluginContext,
    PluginManifest,
    PluginSpec,
)
from .clock import ClockService


class GreeterService:
    """每个请求独立实例；内部持有的 clock 是应用级共享单例。"""

    def __init__(self, clock: ClockService, greeting: str = "hello") -> None:
        self.clock = clock
        self.greeting = greeting

    def greet(self, name: str) -> str:
        return f"{self.greeting}, {name} (tick={self.clock.tick()})"


def _validate_options(options: dict) -> None:
    greeting = options.get("greeting", "hello")
    if not isinstance(greeting, str) or not greeting.strip():
        raise ValueError("greeting 必须是非空字符串")


def _factory(ctx: PluginContext) -> GreeterService:
    clock = ctx.dependencies["clock"]
    return GreeterService(clock, ctx.options.get("greeting", "hello"))


GREETER_SPEC = PluginSpec(
    manifest=PluginManifest(
        name="greeter",
        version="1.0.0",
        provides=("greeter",),
        requires=(InterfaceRequirement(interface="clock", version="^1.0.0"),),
        scope="request",
        description="请求级问候服务",
    ),
    factory=_factory,
    validate_options=_validate_options,
)

PLUGINS: list[PluginSpec] = [GREETER_SPEC]
