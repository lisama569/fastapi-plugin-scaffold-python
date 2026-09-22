"""示例插件：一个应用级共享计数器、一个回显插件、一个请求级插件。"""

from __future__ import annotations

from app_core.plugins.container import RequestContainer, Scope
from app_core.plugins.contracts import (
    BasePlugin,
    PluginMetadata,
    Requirement,
)


class SharedCounter:
    """应用级单例对象：用来在并发请求下验证"只初始化一次"。"""

    def __init__(self, start: int = 0) -> None:
        self.value = start
        self.init_count = 0

    async def aclose(self) -> None:
        pass


class CounterPlugin(BasePlugin):
    """提供应用级单例 SharedCounter。"""

    metadata = PluginMetadata(name="counter", version="1.2.0")

    def __init__(self) -> None:
        self.start = 0
        self.shutdown_called = False

    def configure(self, config: dict[str, object]) -> None:
        super().configure(config)
        self.start = int(config.get("start", 0))

    async def startup(self, ctx) -> SharedCounter:  # noqa: ANN001
        counter = SharedCounter(self.start)
        counter.init_count += 1
        return counter

    async def shutdown(self, ctx) -> None:  # noqa: ANN001
        self.shutdown_called = True


class EchoPlugin(BasePlugin):
    """依赖 counter 的应用级插件，验证依赖版本与拓扑顺序。"""

    metadata = PluginMetadata(
        name="echo",
        version="1.0.0",
        requires=(Requirement("counter", min_version="1.0.0"),),
    )

    async def startup(self, ctx) -> str:  # noqa: ANN001
        return "echo-ready"


class RequestScopedSession:
    """请求级实例：每请求独立，close 时标记释放。"""

    def __init__(self) -> None:
        self.closed = False
        self.request_id: str | None = None

    def close(self) -> None:
        self.closed = True


class SessionPlugin(BasePlugin):
    """请求级插件：惰性为每个请求创建独立 session。"""

    metadata = PluginMetadata(
        name="session",
        version="0.9.0",
        scope=Scope.REQUEST,
        requires=(Requirement("counter", min_version="1.0.0"),),
    )

    async def create_request_instance(self, rc: RequestContainer) -> RequestScopedSession:
        rc.get_app_instance("counter")  # 依赖应用级单例
        return RequestScopedSession()


# 发现机制读取的标准导出形式
PLUGINS = [CounterPlugin(), EchoPlugin(), SessionPlugin()]
