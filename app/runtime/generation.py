"""装配代：一次成功装配产出的容器、配置与其生命周期。

热更新的核心不变量：

* 请求开始时对当前代 ``acquire``（与读取当前代同属一个同步区间，不会被
  重载协程插入），请求结束 ``release``；正在处理的请求因此始终跑在
  开始时的那一代实例上，不会被打断，也不会看到新旧混用；
* 一代被替换后进入退役（retire）：先在途请求归零，再逆序停止
  “未被下一代复用”的应用级插件，最后释放容器。被复用的实例所有权
  已移交给下一代，本代绝不停掉它们。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..assembly.report import AssemblyReport
    from ..config.model import AppConfig
    from ..di.container import Container
    from ..plugins.registry import PluginRegistry

logger = logging.getLogger("app.runtime")


class GenerationRetiredError(RuntimeError):
    """在已退役的代上登记新请求。"""


class AssemblyGeneration:
    """一代装配结果：容器 + 报告 + 配置 + 在途请求计数。"""

    def __init__(
        self,
        generation_id: int,
        container: Container,
        report: AssemblyReport,
        config: AppConfig,
        registry: PluginRegistry,
    ) -> None:
        self.generation_id = generation_id
        self.container = container
        self.report = report
        self.config = config
        self._registry = registry
        self._inflight = 0
        self._retired = False
        # 单事件循环内计数无需加锁；事件用于退役方等待在途归零
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def retired(self) -> bool:
        return self._retired

    def acquire(self) -> None:
        """请求开始：登记一个在用引用（必须与读取当前代在同一同步区间内）。"""
        if self._retired:
            raise GenerationRetiredError(f"装配代 {self.generation_id} 已退役")
        self._inflight += 1
        self._drained.clear()

    def release(self) -> None:
        """请求结束：释放一个在用引用。"""
        self._inflight -= 1
        if self._inflight < 0:  # 防御：acquire/release 必须成对
            self._inflight = 0
            logger.error("装配代 %s 的 release 多于 acquire", self.generation_id)
        if self._inflight == 0:
            self._drained.set()

    def app_instance_names(self) -> list[str]:
        """本代持有的全部应用级插件名（按装配顺序，含新建与复用）。"""
        return [
            step.plugin
            for step in self.report.steps
            if step.scope == "app" and step.status in ("ok", "reused")
        ]

    async def retire(self, reused: set[str]) -> None:
        """退役：等待在途请求归零，逆序停止未复用的应用级插件并释放容器。

        ``reused`` 是下一代复用的插件名集合，这些实例所有权已移交，不在
        本代停止。幂等：重复调用直接返回。
        """
        if self._retired:
            return
        self._retired = True
        if self._inflight > 0:
            logger.info(
                "装配代 %d 等待 %d 个在途请求结束后退役",
                self.generation_id,
                self._inflight,
            )
        await self._drained.wait()
        for name in reversed(self.app_instance_names()):
            if name in reused:
                logger.info("装配代 %d: 实例 %s 被下一代复用，跳过停止", self.generation_id, name)
                continue
            spec = self._registry.get(name)
            if spec.on_stop is not None:
                try:
                    result = spec.on_stop(self.container.get_by_name(name))
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001 - 退役停止失败不阻断后续清理
                    logger.error("装配代 %d 停止 %s 失败: %s", self.generation_id, name, exc)
            logger.info("装配代 %d: 已停止 %s", self.generation_id, name)
        await self.container.dispose()
        logger.info("装配代 %d 已退役并释放", self.generation_id)
