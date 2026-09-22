"""装配失败示例插件：startup 必定抛错，用于回滚测试。"""

from __future__ import annotations

from app_core.plugins.contracts import BasePlugin, PluginMetadata, Requirement


class FailingStartupPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="failing",
        version="1.0.0",
        requires=(Requirement("counter", min_version="1.0.0"),),
    )

    async def startup(self, ctx):  # noqa: ANN001
        raise RuntimeError("failing-plugin deliberately exploded")


PLUGINS = [FailingStartupPlugin()]
