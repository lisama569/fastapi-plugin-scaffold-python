"""FastAPI 集成：lifespan 装配/释放、请求作用域与接口注入、运行期热更新。

* ``build_lifespan``：启动时执行首次装配（失败则应用拒绝启动），关停时
  退役当前装配代并等待全部清理完成；
* 请求中间件：在同步区间内读取当前装配代并登记在用引用（热更新切换
  不会打断在途请求），每个请求派生独立 ``RequestScope``，请求结束
  逆序释放并归还代的引用；
* ``requires(interface)``：路由依赖，自动从当前请求作用域取实例
  （应用级接口拿到全局单例，请求级接口拿到请求私有实例）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from ..assembly.report import AssemblyReport
from ..di.container import Container, RequestScope
from ..runtime.reloader import Runtime

logger = logging.getLogger("app.ext")

RUNTIME_ATTR = "plugin_runtime"
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


def build_lifespan(runtime: Runtime):
    """构造 FastAPI lifespan：启动装配、关停退役。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        attach_runtime(app, runtime)
        report = runtime.current().report
        logger.info("应用启动完成，装配报告:\n%s", report.describe())
        try:
            yield
        finally:
            logger.info("应用关停，退役当前装配代")
            await runtime.shutdown()

    return lifespan


def install(app: FastAPI, runtime: Runtime) -> FastAPI:
    """安装 lifespan 与请求作用域中间件。"""
    app.router.lifespan_context = build_lifespan(runtime)

    @app.middleware("http")
    async def request_scope_middleware(request: Request, call_next):
        runtime = get_runtime(request.app)
        # 读取当前代 + 登记引用在同一同步区间内完成：
        # 重载协程无法插入其间，在途请求始终跑完整的一代
        generation = runtime.current_and_acquire()
        try:
            scope = generation.container.new_request_scope()
            setattr(request.state, SCOPE_REQUEST_ATTR, scope)
            try:
                return await call_next(request)
            finally:
                await scope.dispose()
        finally:
            generation.release()

    return app


def attach_runtime(app: FastAPI, runtime: Runtime) -> None:
    """把运行时挂到 app.state。"""
    setattr(app.state, RUNTIME_ATTR, runtime)


def get_runtime(app: FastAPI) -> Runtime:
    """从 app.state 取运行时。"""
    runtime = getattr(app.state, RUNTIME_ATTR, None)
    if runtime is None:
        raise RuntimeError("运行时尚未就绪（lifespan 是否执行？）")
    return runtime


def get_container(app: FastAPI) -> Container:
    """当前对外服务装配代的容器。"""
    return get_runtime(app).current().container


def get_report(app: FastAPI) -> AssemblyReport:
    """当前对外服务装配代的装配报告。"""
    return get_runtime(app).current().report
