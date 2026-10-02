"""运行期热更新编排：串行化重载、复用计划、失败整体回退。

语义约定：

* 任一时刻只有一代装配对外服务；``current_and_acquire`` 在同步区间内
  读取当前代并登记在用引用，因此切换不会出现新旧混用的中间状态；
* 并发触发的重载由单把锁串行化：后到者等待前者完成，并基于最新的
  装配代重新计算差量，最终结果确定且可解释（日志可见等待与顺序）；
* 重载失败（配置不合法/依赖不满足/装配中途报错）时，新装配整体放弃，
  当前代继续对外服务，报告中携带失败阶段、原因与可定位链路；
* 复用计划对“配置与实现都没变”的应用级插件沿用旧实例，且对依赖闭包
  收敛：依赖发生重建的插件自身也必须重建，杜绝新旧实例混用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..assembly.assembler import Assembler
from ..config.errors import ConfigError
from ..plugins.errors import AssemblyError, PluginError
from .generation import AssemblyGeneration

if TYPE_CHECKING:
    from ..config.model import AppConfig
    from ..plugins.registry import PluginRegistry

logger = logging.getLogger("app.runtime")


@dataclass
class ReloadReport:
    """一次热更新的结果：成功时的复用/重建明细，或失败时的定位信息。"""

    ok: bool
    generation: int = 0
    source_order: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    request_registered: list[str] = field(default_factory=list)
    stage: str = ""  # 失败阶段: "config" | "assemble"
    failure_plugin: str | None = None
    failure_reason: str | None = None
    failure_chain: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines: list[str] = [f"配置来源(低->高): {self.source_order}"]
        if self.ok:
            lines.append(f"热更新成功: 当前装配代={self.generation}")
            lines.append(f"  复用(未变化): {self.reused}")
            lines.append(f"  重建: {self.rebuilt}")
            lines.append(f"  新启用: {self.added} | 新禁用: {self.removed}")
            lines.append(f"  请求级登记: {self.request_registered}")
        else:
            lines.append(f"热更新失败(阶段={self.stage}): 继续使用原装配代={self.generation}")
            lines.append(f"  失败插件: {self.failure_plugin}")
            lines.append(f"  失败原因: {self.failure_reason}")
            lines.append(f"  失败链路: {' -> '.join(self.failure_chain)}")
        return "\n".join(lines)


def compute_reuse_plan(
    registry: PluginRegistry,
    old_config: AppConfig,
    new_config: AppConfig,
) -> set[str]:
    """计算可复用的应用级插件集合。

    候选条件：应用级、新旧配置都启用、插件 options 完全一致。
    随后按依赖闭包收敛：任一应用级依赖不在复用集合内，则自身也移除
    （依赖已重建时沿用旧实例会让旧实例持有旧依赖，造成新旧混用）。
    """
    candidates: set[str] = set()
    for spec in registry.all_specs():
        m = spec.manifest
        if m.scope != "app":
            continue
        old_pc = old_config.plugins.get(m.name)
        new_pc = new_config.plugins.get(m.name)
        old_enabled = old_pc.enabled if old_pc is not None else True
        new_enabled = new_pc.enabled if new_pc is not None else True
        if not (old_enabled and new_enabled):
            continue
        old_opts = old_pc.options if old_pc is not None else {}
        new_opts = new_pc.options if new_pc is not None else {}
        if old_opts != new_opts:
            continue
        candidates.add(m.name)

    changed = True
    while changed:
        changed = False
        for spec in registry.all_specs():
            m = spec.manifest
            if m.name not in candidates:
                continue
            for req in m.requires:
                provider = registry.provider_of(req.interface).manifest.name
                if provider not in candidates:
                    candidates.discard(m.name)
                    changed = True
                    break
    return candidates


class Runtime:
    """持有当前装配代，对外提供原子读取与串行化热更新。"""

    def __init__(
        self,
        registry: PluginRegistry,
        config_provider: Callable[[], AppConfig],
        initial_config: AppConfig,
        on_config_applied: Callable[[AppConfig], None] | None = None,
    ) -> None:
        self._registry = registry
        self._config_provider = config_provider
        self._initial_config = initial_config
        self._on_config_applied = on_config_applied
        self._current: AssemblyGeneration | None = None
        self._reload_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()
        self._generation_seq = 0

    async def start(self) -> None:
        """启动期首次装配（失败则抛出，应用拒绝启动）。"""
        assembler = Assembler(self._registry, self._initial_config)
        container, report = await assembler.assemble()
        self._current = self._new_generation(container, report, self._initial_config)

    def current(self) -> AssemblyGeneration:
        if self._current is None:
            raise RuntimeError("运行时尚未启动（lifespan 是否执行？）")
        return self._current

    def current_and_acquire(self) -> AssemblyGeneration:
        """同步区间内读取当前代并登记在用引用，保证请求不被换代打断。"""
        generation = self.current()
        generation.acquire()
        return generation

    async def reload(self) -> ReloadReport:
        """串行化热更新：任一时刻只有一次重载在进行。"""
        if self._reload_lock.locked():
            logger.info("已有热更新在进行，本次触发排队等待")
        async with self._reload_lock:
            return await self._reload_locked()

    async def _reload_locked(self) -> ReloadReport:
        old = self.current()
        logger.info("开始热更新: 当前装配代=%d", old.generation_id)

        try:
            new_config = self._config_provider()
        except ConfigError as exc:
            where = getattr(exc, "key", None) or getattr(exc, "source", "<config>")
            report = ReloadReport(
                ok=False,
                generation=old.generation_id,
                source_order=list(old.config.source_order),
                stage="config",
                failure_reason=str(exc),
                failure_chain=[f"config:{where}", "reload"],
            )
            logger.error("热更新失败(配置阶段): %s\n%s", exc, report.describe())
            return report

        reuse = compute_reuse_plan(self._registry, old.config, new_config)
        reuse_map = {name: old.container.get_by_name(name) for name in reuse}
        logger.info("复用计划: %s", sorted(reuse))

        assembler = Assembler(self._registry, new_config, reuse=reuse_map)
        try:
            container, asm_report = await assembler.assemble()
        except (AssemblyError, PluginError) as exc:
            chain = list(getattr(exc, "chain", None) or [getattr(exc, "plugin", "?")])
            report = ReloadReport(
                ok=False,
                generation=old.generation_id,
                source_order=list(new_config.source_order),
                stage="assemble",
                failure_plugin=getattr(exc, "plugin", None),
                failure_reason=str(exc),
                failure_chain=chain,
            )
            logger.error("热更新失败(装配阶段): %s\n%s", exc, report.describe())
            return report

        generation = self._new_generation(container, asm_report, new_config)
        self._current = generation
        if self._on_config_applied is not None:
            self._on_config_applied(new_config)
        self._schedule_retire(old, reused=reuse)

        added, removed = self._diff_enabled(old.config, new_config)
        report = ReloadReport(
            ok=True,
            generation=generation.generation_id,
            source_order=list(new_config.source_order),
            reused=list(asm_report.reused),
            rebuilt=list(asm_report.assembled),
            added=added,
            removed=removed,
            request_registered=list(asm_report.request_registered),
        )
        logger.info("热更新完成:\n%s", report.describe())
        return report

    def _diff_enabled(
        self, old_config: AppConfig, new_config: AppConfig
    ) -> tuple[list[str], list[str]]:
        """新旧配置间 启用->禁用 / 禁用->启用 的插件名（按注册序，确定可解释）。"""

        def enabled_set(cfg: AppConfig) -> set[str]:
            out: set[str] = set()
            for spec in self._registry.all_specs():
                pc = cfg.plugins.get(spec.manifest.name)
                if pc is None or pc.enabled:
                    out.add(spec.manifest.name)
            return out

        old_on, new_on = enabled_set(old_config), enabled_set(new_config)
        order = [s.manifest.name for s in self._registry.all_specs()]
        added = [n for n in order if n in new_on - old_on]
        removed = [n for n in order if n in old_on - new_on]
        return added, removed

    def _new_generation(self, container: Any, report: Any, config: AppConfig) -> AssemblyGeneration:
        self._generation_seq += 1
        return AssemblyGeneration(self._generation_seq, container, report, config, self._registry)

    def _schedule_retire(self, generation: AssemblyGeneration, reused: set[str]) -> None:
        task = asyncio.create_task(generation.retire(reused))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def wait_idle(self) -> None:
        """等待全部退役清理完成（测试与关停时使用）。"""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def shutdown(self) -> None:
        """关停：退役当前代（无复用）并等待全部清理完成。"""
        if self._current is not None:
            current = self._current
            self._current = None
            await current.retire(set())
        await self.wait_idle()
