"""示例：应用级 clock 插件，提供 ``clock`` 接口的全局单例。"""

from __future__ import annotations

from ..plugins.contract import PluginContext, PluginManifest, PluginSpec


class ClockService:
    """统计自身被初始化次数的时钟服务（用于验证单例语义）。"""

    def __init__(self) -> None:
        self.tick_count = 0

    def tick(self) -> int:
        self.tick_count += 1
        return self.tick_count


def _factory(ctx: PluginContext) -> ClockService:
    return ClockService()


async def _on_start(service: ClockService) -> None:
    # 真实插件可在此建立连接池等资源
    service.tick_count = 0


async def _on_stop(service: ClockService) -> None:
    pass


CLOCK_SPEC = PluginSpec(
    manifest=PluginManifest(
        name="clock",
        version="1.0.0",
        provides=("clock",),
        scope="app",
        description="应用级时钟单例",
    ),
    factory=_factory,
    on_start=_on_start,
    on_stop=_on_stop,
)
