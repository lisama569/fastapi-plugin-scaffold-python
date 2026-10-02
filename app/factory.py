"""应用组装入口：配置 -> 注册表 -> 监督器 -> FastAPI。

首次装配在 FastAPI lifespan 启动阶段确定性完成；任何配置/插件/装配错误
都会让应用启动失败，绝不带病进入服务状态。运行期可通过 ``POST /reload``
触发热更新：重载配置、增量装配、原子切换，失败则整体回退到旧装配。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from .config.loader import load_config, validate
from .config.model import AppConfig
from .ext.fastapi_lifespan import get_report, install
from .plugins.contract import PluginSpec
from .plugins.discovery import register_all
from .plugins.registry import PluginRegistry
from .runtime.errors import ReloadError
from .runtime.supervisor import PluginSupervisor

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

    def config_loader() -> AppConfig:
        """按相同来源与优先级（重新）加载配置；启动与热更新共用。"""
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

    config = config_loader()
    level = logging.getLevelName(config.log_level.upper())
    configure_logging(level if isinstance(level, int) else logging.INFO)

    registry = build_registry(plugin_specs or [])
    supervisor = PluginSupervisor(registry, config_loader)

    app = FastAPI(title=config.app_name)
    app.state.plugin_supervisor = supervisor
    install(app, supervisor)

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
            "failure_chain": report.failure_chain,
        }

    @app.post("/reload")
    async def reload_plugins() -> dict[str, Any]:
        """触发热更新：重载配置并增量装配；失败整体回退，旧装配继续服务。"""
        try:
            outcome = await supervisor.reload()
        except ReloadError as exc:
            return {
                "ok": False,
                "stage": exc.stage,
                "reason": exc.reason,
                "failure_chain": exc.chain,
            }
        return {
            "ok": True,
            "reused": outcome.reused,
            "rebuilt": outcome.rebuilt,
            "added": outcome.added,
            "removed": outcome.removed,
        }

    return app
