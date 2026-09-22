"""FastAPI 生命周期接入。

- 启动：按拓扑序装配插件，成功后把 :class:`~app_core.plugins.container.Container`
  挂到 ``app.state.container``；任何装配错误向上抛出（应用启动失败、绝不带病运行），
  且已启动插件已由 assembler 逆序回滚。
- 关闭：按装配的逆序 shutdown 应用级插件并释放单例。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app_core.plugins.assembler import Assembler
from app_core.plugins.container import Container
from app_core.plugins.registry import PluginRegistry

logger = logging.getLogger("app_core.lifespan")


@asynccontextmanager
async def plugin_lifespan(app: FastAPI) -> AsyncIterator[None]:
    registry: PluginRegistry = app.state.registry
    assembler = Assembler(registry, app.state.settings)
    container = Container()

    order = await assembler.assemble(container)
    app.state.container = container
    app.state.plugin_order = order
    logger.info("应用启动完成，已装配插件: %s", order)

    try:
        yield
    finally:
        logger.info("应用关闭，逆序关闭插件: %s", list(reversed(order)))
        await assembler.rollback(order, container)
