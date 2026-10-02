"""应用组装入口：配置 -> 注册表 -> 运行时 -> FastAPI。

首次装配在 FastAPI lifespan 启动阶段确定性完成；任何配置/插件/装配错误
都会让应用启动失败，绝不带病进入服务状态。运行期可通过
``POST /admin/reload``（或编程式 ``Runtime.reload()``）重新加载配置并
热切换装配，失败时整体回退、原装配继续服务。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config.loader import load_config, validate
from .config.model import AppConfig
from .ext.fastapi_lifespan import get_report, get_runtime, install
from .plugins.contract import PluginSpec
from .plugins.discovery import register_all
from .plugins.registry import PluginRegistry
from .runtime.reloader import Runtime

logger = logging.getLogger("app")

DEFAULT_DEFAULTS: dict[str, Any] = {
    "app_name": "fastapi-plugin-scaffold",
    "log_level": "info",
    "plugins": {},
}


def configure_logging(level: int = logging.INFO) -> None:
    """为 ``app`` 命名空间配置一次控制台输出（幂等，不覆盖已有 handler）。"""
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(level)


def build_registry(specs: list[PluginSpec]) -> PluginRegistry:
    """注册全部插件规格并完成图校验（重名/接口冲突在注册期立即拒绝）。"""
    registry = PluginRegistry()
    register_all(registry, specs)
    registry.validate_graph()
    return registry


def _apply_log_level(config: AppConfig) -> None:
    level = logging.getLevelName(config.log_level.upper())
    configure_logging(level if isinstance(level, int) else logging.INFO)


def create_app(
    *,
    config_path: str | None = None,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
    plugin_specs: list[PluginSpec] | None = None,
    defaults: dict[str, Any] | None = None,
) -> FastAPI:
    """以确定性方式加载配置、注册插件并构造 FastAPI 应用。"""
    effective_defaults = defaults or DEFAULT_DEFAULTS

    def config_provider() -> AppConfig:
        """从同一组来源重新加载并校验配置（启动与热更新共用一条路径）。"""
        result = load_config(
            defaults=effective_defaults,
            config_path=config_path,
            environ=environ,
            overrides=overrides,
        )
        config = validate(result)
        logger.info("最终配置来源优先级(低->高): %s", result.source_order)
        logger.info("逐键生效来源: %s", result.key_sources)
        return config

    config = config_provider()
    _apply_log_level(config)

    registry = build_registry(plugin_specs or [])
    runtime = Runtime(
        registry,
        config_provider,
        initial_config=config,
        on_config_applied=_apply_log_level,
    )

    app = FastAPI(title=config.app_name)
    install(app, runtime)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/assembly-report")
    async def assembly_report() -> dict[str, Any]:
        report = get_report(app)
        return {
            "ok": report.ok,
            "source_order": report.source_order,
            "assembled": report.assembled,
            "reused": report.reused,
            "failure_chain": report.failure_chain,
        }

    @app.post("/admin/reload")
    async def admin_reload() -> JSONResponse:
        """重新加载配置并热切换装配；失败时整体回退并返回失败链路。"""
        report = await get_runtime(app).reload()
        return JSONResponse(
            status_code=200 if report.ok else 500,
            content={
                "ok": report.ok,
                "generation": report.generation,
                "source_order": report.source_order,
                "reused": report.reused,
                "rebuilt": report.rebuilt,
                "added": report.added,
                "removed": report.removed,
                "request_registered": report.request_registered,
                "stage": report.stage,
                "failure_plugin": report.failure_plugin,
                "failure_reason": report.failure_reason,
                "failure_chain": report.failure_chain,
            },
        )

    return app
