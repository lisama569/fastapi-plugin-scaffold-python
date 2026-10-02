"""插件监督器：持有当前装配，负责运行期热更新的编排。

热更新流程（全程串行，单把锁保证并发触发时结果确定、可解释）：

1. 重新加载配置（来源与优先级与启动期完全一致）；
2. 计算复用计划：配置与实现均未变化、且全部依赖也被复用的应用级插件，
   沿用旧实例，不重复初始化（避免重复连接外部资源）；
3. 用新配置 + 复用计划增量装配出一套**全新**容器（失败即整体放弃，
   复用实例所有权仍归旧装配，不会被回滚波及）；
4. 原子切换：新状态整组替换旧状态并发布，此后新请求走新装配；
5. 退役旧容器：不再派生新请求作用域，等在途请求排空后，
   对未沿用的旧实例执行 on_stop 并释放旧容器。

任一阶段失败都抛 ``ReloadError``（携带阶段、原因与失败链路），
旧装配继续对外服务，应用不会进入半新不旧的中间态。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from ..assembly.assembler import Assembler
from ..assembly.report import AssemblyReport
from ..config.model import AppConfig
from ..di.container import Container
from ..plugins.errors import AssemblyError
from ..plugins.registry import PluginRegistry
from .errors import ReloadError

logger = logging.getLogger("app.runtime")

ConfigLoader = Callable[[], AppConfig]
PublishHook = Callable[[Container, AssemblyReport], None]


@dataclass
class ReloadOutcome:
    """一次热更新的结果记录（成功或失败均可解释）。"""

    ok: bool
    reused: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failure_stage: str | None = None
    failure_reason: str | None = None
    failure_chain: list[str] = field(default_factory=list)


@dataclass
class _State:
    """当前对外服务的一套装配（不可变整体，切换时整组替换）。"""

    container: Container
    report: AssemblyReport
    config: AppConfig


class PluginSupervisor:
    """持有当前装配状态，串行编排热更新。"""

    def __init__(self, registry: PluginRegistry, config_loader: ConfigLoader) -> None:
        self._registry = registry
        self._config_loader = config_loader
        self._state: _State | None = None
        self._reload_lock = asyncio.Lock()
        self._publish: PublishHook | None = None

    # -- 生命周期 -------------------------------------------------------

    async def start(self, publish: PublishHook | None = None) -> AssemblyReport:
        """首次装配并发布；失败直接抛出（应用拒绝启动）。"""
        self._publish = publish
        config = self._config_loader()
        assembler = Assembler(self._registry, config)
        container, report = await assembler.assemble()
        self._state = _State(container, report, config)
        self._publish_state()
        return report

    async def reload(self) -> ReloadOutcome:
        """执行一次热更新；失败抛 ReloadError，旧装配不受影响。

        并发触发时在锁上排队串行执行：每次都在拿到锁后重新加载最新
        配置，因此最终结果确定（以最后一次完成的重载为准），互不踩踏。
        """
        async with self._reload_lock:
            if self._state is None:
                raise ReloadError("lifecycle", "应用尚未启动，无法热更新")
            old = self._state
            logger.info("热更新开始（当前装配: %s）", old.report.assembled)

            new_config = self._load_new_config()
            reuse = self._reuse_plan(old, new_config)
            container, report = await self._assemble_new(new_config, reuse)

            # 原子切换：状态整组替换，新请求即刻走新装配
            self._state = _State(container, report, new_config)
            self._publish_state()
            logger.info("热更新切换完成，新装配报告:\n%s", report.describe())

            outcome = self._build_outcome(old, report, reuse)
            self._retire(old, carried_ids={id(inst) for inst in reuse.values()})
            logger.info(
                "热更新成功: 复用=%s 重建=%s 新增=%s 移除=%s",
                outcome.reused,
                outcome.rebuilt,
                outcome.added,
                outcome.removed,
            )
            return outcome

    async def shutdown(self) -> None:
        """释放当前装配（应用关停）。"""
        state, self._state = self._state, None
        if state is not None:
            await state.container.dispose()

    # -- 查询 -----------------------------------------------------------

    def current_container(self) -> Container:
        """取当前对外服务的容器（请求起点调用，保证单请求内不混用）。"""
        if self._state is None:
            raise RuntimeError("容器尚未装配完成（lifespan 是否执行？）")
        return self._state.container

    def current_report(self) -> AssemblyReport:
        if self._state is None:
            raise RuntimeError("装配报告不存在（lifespan 是否执行？）")
        return self._state.report

    def current_config(self) -> AppConfig:
        if self._state is None:
            raise RuntimeError("配置尚未加载（lifespan 是否执行？）")
        return self._state.config

    # -- 内部步骤 -------------------------------------------------------

    def _publish_state(self) -> None:
        if self._publish is not None and self._state is not None:
            self._publish(self._state.container, self._state.report)

    def _load_new_config(self) -> AppConfig:
        """重新加载并校验配置；任何配置错误都终止本次更新。"""
        try:
            config = self._config_loader()
        except Exception as exc:
            logger.error("热更新失败：配置加载/校验未通过: %s", exc)
            raise ReloadError("config", str(exc), cause=exc) from exc
        logger.info(
            "热更新配置加载成功: 来源(低->高)=%s 插件=%s",
            list(config.source_order),
            {n: (p.enabled, p.scope) for n, p in config.plugins.items()},
        )
        return config

    def _reuse_plan(self, old: _State, new_config: AppConfig) -> dict[str, object]:
        """计算可复用的应用级实例：配置未变、实现未变、依赖链全部复用。

        按拓扑序判定：任一依赖需要重建时，依赖它的插件也必须重建
        （否则旧实例会继续持有被替换掉的旧依赖）。
        """
        plan: dict[str, object] = {}
        reusable: set[str] = set()
        for spec in self._registry.resolve_order():
            m = spec.manifest
            if m.scope != "app":
                continue
            old_pc = old.config.plugins.get(m.name)
            new_pc = new_config.plugins.get(m.name)
            old_enabled = old_pc.enabled if old_pc is not None else True
            new_enabled = new_pc.enabled if new_pc is not None else True
            if not (old_enabled and new_enabled):
                continue
            old_opts = old_pc.options if old_pc is not None else {}
            new_opts = new_pc.options if new_pc is not None else {}
            if old_opts != new_opts:
                logger.info("插件 %s 配置变化，将重建", m.name)
                continue
            if new_pc is not None and new_pc.scope != m.scope:
                continue  # 作用域冲突交给装配器统一报错，此处保守不复用
            deps_reused = all(
                self._registry.provider_of(req.interface).manifest.name in reusable
                for req in m.requires
            )
            if not deps_reused:
                logger.info("插件 %s 的依赖发生变化，将级联重建", m.name)
                continue
            try:
                instance = old.container.get_by_name(m.name)
            except KeyError:
                continue
            plan[m.name] = instance
            reusable.add(m.name)
        return plan

    async def _assemble_new(
        self, new_config: AppConfig, reuse: dict[str, object]
    ) -> tuple[Container, AssemblyReport]:
        """用新配置增量装配；失败时新装配整体回滚，旧装配不受波及。"""
        assembler = Assembler(self._registry, new_config)
        try:
            return await assembler.assemble(reuse=reuse)
        except AssemblyError as exc:
            logger.error(
                "热更新装配失败于 %s: %s | 失败链路: %s；保持原有装配",
                exc.plugin,
                exc.reason,
                " -> ".join(exc.chain),
            )
            raise ReloadError("assembly", exc.reason, chain=list(exc.chain), cause=exc) from exc

    def _build_outcome(
        self, old: _State, report: AssemblyReport, reuse: dict[str, object]
    ) -> ReloadOutcome:
        new_app = [n for n in report.assembled]
        old_app = list(old.report.assembled)
        return ReloadOutcome(
            ok=True,
            reused=sorted(reuse),
            rebuilt=[n for n in new_app if n not in reuse and n in old_app],
            added=[n for n in new_app if n not in old_app],
            removed=[n for n in old_app if n not in new_app],
        )

    def _retire(self, old: _State, carried_ids: set[int]) -> None:
        """退役旧容器：在途请求排空后释放未沿用的旧实例。"""

        def _on_drained() -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # 无运行中的事件循环（同步测试场景）
                return
            loop.create_task(self._finalize_old(old, carried_ids))

        old.container.retire(on_drained=_on_drained)
        logger.info(
            "旧装配已退役（在途请求作用域=%d，排空后释放）",
            old.container.active_scopes,
        )

    async def _finalize_old(self, old: _State, carried_ids: set[int]) -> None:
        """旧容器排空后：对未沿用的应用级实例执行 on_stop，再释放容器。"""
        for name in reversed(old.report.assembled):
            try:
                instance = old.container.get_by_name(name)
            except Exception:  # noqa: BLE001 - 容器可能已释放
                break
            if id(instance) in carried_ids:
                continue  # 实例已沿用到新装配，所有权转移，不得停止
            spec = self._registry.get(name)
            if spec.on_stop is not None:
                try:
                    result = spec.on_stop(instance)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    logger.error("释放旧实例 %s 时出错: %s", name, exc)
            logger.info("旧实例已停止: %s", name)
        await old.container.dispose()
        logger.info("旧装配已释放")
