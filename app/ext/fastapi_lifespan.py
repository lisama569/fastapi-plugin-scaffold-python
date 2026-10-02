"""FastAPI 集成：lifespan 装配/释放、请求作用域与接口注入、热更新切换。

* ``build_lifespan``：启动时由监督器完成首次装配（失败则应用拒绝启动），
  关停时释放当前装配；
* 请求中间件：每个请求**起点**从 app.state 取当前容器并派生独立
  ``RequestScope``——热更新切换容器是原子替换，单个请求自始至终只持有
  起点那一刻的装配，绝不出现新旧实例混用；旧容器等在途请求排空后释放；
* ``requires(interface)``：路由依赖，自动从当前请求作用域取实例
  （应用级接口拿到全局单例，请求级接口拿到请求私有实例）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from ..di.container import Container, RequestScope
from ..runtime.supervisor import PluginSupervisor

logger = logging.getLogger("app.ext")

CONTAINER_ATTR = "plugin_container"
REPORT_ATTR = "assembly_report"
SCOPE_REQUEST_ATTR = "plugin_scope"


def get_request_scope(request: Request) -> RequestScope:
    """FastAPI 依赖：获取当前请求的作用域。"""
    scope = getattr(request.state, SCOPE_REQUEST_ATTR, None)
    if scope is None:
        raise RuntimeError("请求作用域尚未初始化（中间件是否启用？）")
    return scope


def requires(interface: str) -> Callable[[Request], Any]:
    """FastAPI 依赖工厂：按接口名注入实例。"""

    def _resolver(request: Request) -> Any:
        return get_request_scope(request).get(interface)

    _resolver.__name__ = f"requires_{interface}"
    return _resolver


def build_lifespan(supervisor: PluginSupervisor):
    """构造 FastAPI lifespan：启动装配、关停释放。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        report = await supervisor.start(
            publish=lambda container, rep: attach_container(app, container, rep)
        )
        logger.info("应用启动完成，装配报告:\n%s", report.describe())
        try:
            yield
        finally:
            logger.info("应用关停，释放当前装配")
            await supervisor.shutdown()

    return lifespan


def install(app: FastAPI, supervisor: PluginSupervisor) -> FastAPI:
    """安装 lifespan 与请求作用域中间件。"""
    app.router.lifespan_context = build_lifespan(supervisor)

    @app.middleware("http")
    async def request_scope_middleware(request: Request, call_next):
        # 请求起点一次性取当前容器：此后即使发生热更新切换，
        # 本请求仍持有起点那套装配直到结束，不被打断、不混用。
        container = get_container(request.app)
        scope = container.new_request_scope()
        setattr(request.state, SCOPE_REQUEST_ATTR, scope)
        try:
            return await call_next(request)
        finally:
            await scope.dispose()

    return app


def attach_container(app: FastAPI, container: Container, report) -> None:
    """把容器与装配报告挂到 app.state（热更新切换时整体替换）。"""
    setattr(app.state, CONTAINER_ATTR, container)
    setattr(app.state, REPORT_ATTR, report)


def get_container(app: FastAPI) -> Container:
    """从 app.state 取容器。"""
    container = getattr(app.state, CONTAINER_ATTR, None)
    if container is None:
        raise RuntimeError("容器尚未装配完成（lifespan 是否执行？）")
    return container


def get_report(app: FastAPI):
    report = getattr(app.state, REPORT_ATTR, None)
    if report is None:
        raise RuntimeError("装配报告不存在（lifespan 是否执行？）")
    return report
