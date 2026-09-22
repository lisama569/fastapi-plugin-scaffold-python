"""应用工厂：把配置加载、插件注册/发现、装配与 HTTP 依赖注入组装起来。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from app_core.config.loader import load_settings
from app_core.config.schema import AppSettings, LoadedConfig
from app_core.plugins.container import Container, RequestContainer
from app_core.plugins.lifespan import plugin_lifespan
from app_core.plugins.registry import PluginRegistry

logger = logging.getLogger("app_core")


def create_app(
    settings: AppSettings | None = None,
    *,
    registry: PluginRegistry | None = None,
    config_file: str | None = None,
    overrides: dict[str, object] | None = None,
    discover: bool = False,
    discover_package: str = "plugins_example",
) -> FastAPI:
    """创建 FastAPI 应用。

    :param settings: 已加载配置；为 None 时按 file/env/override 现场加载。
    :param registry: 已注册插件的注册表；为 None 时新建。
    :param config_file: 现场加载配置时使用的配置文件路径。
    :param overrides: 现场加载配置时的最高优先级覆盖。
    :param discover: 是否自动发现示例插件包中的插件。
    :param discover_package: 发现的目标包名。
    """
    loaded: LoadedConfig | None = None
    if settings is None:
        loaded = load_settings(file_path=config_file, overrides=overrides)
        settings = loaded.settings

    if registry is None:
        registry = PluginRegistry()
        if discover:
            found = registry.discover(discover_package)
            logger.info("自动发现插件 %d 个: %s", found, registry.names())

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=plugin_lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.config_provenance = loaded.provenance if loaded else {}

    @app.get("/health")
    async def health(request: Request) -> dict[str, object]:
        return {
            "status": "ok",
            "app": settings.app_name,
            "plugins": app.state.plugin_order,
        }

    @app.get("/counter/incr")
    async def counter_incr(
        rc: RequestContainerDep,
    ) -> dict[str, object]:
        """演示依赖注入：读应用级单例 + 按需创建请求级实例。

        所有并发请求拿到同一个 counter 单例；session 则每请求独立。
        """
        counter = rc.get_app_instance("counter")
        counter.value += 1
        payload: dict[str, object] = {"value": counter.value,
                                      "init_count": counter.init_count}
        if "session" in app.state.plugin_order:
            session = await rc.get_request_instance("session")
            payload["session_id"] = id(session)
        return payload

    # 允许插件通过可选钩子 register_routes 自行挂载路由
    for plugin in registry.all():
        register_routes = getattr(plugin, "register_routes", None)
        if callable(register_routes):
            register_routes(app.router)

    return app


# --- 请求级依赖注入辅助 ----------------------------------------------------

async def get_request_container(request: Request) -> RequestContainer:
    """每个请求构建独立的 RequestContainer，请求结束时关闭。"""
    app_container: Container = request.app.state.container
    rc = app_container.build_request_container()
    try:
        yield rc  # type: ignore[misc]
    finally:
        await rc.aclose()


RequestContainerDep = Annotated[RequestContainer, Depends(get_request_container)]
