"""装配编排：按拓扑序创建实例、执行启动钩子，失败时逆序回滚。

装配步骤（全部发生在应用启动阶段）：

1. 过滤掉配置中 enabled=false 的插件；
2. 注册表图校验（依赖存在/版本/环，已在注册阶段与 resolve_order 覆盖）；
3. 校验作用域规则：请求级插件不能被应用级插件依赖；
4. 拓扑序逐插件：先执行 validate_options（拒绝非法配置，绝不静默套用默认），
   通过后再实例化；app 级登记单例，request 级登记规格；
5. 按序执行 on_start；任一步失败（含 validate_options 主动拒绝）：
   逆序执行本次已启动插件的 on_stop，释放容器，并抛 AssemblyError
   （携带装配顺序、已完成列表、失败链路）。拒绝发生在实例化前，
   因此回滚的是排在它之前、本次已经启动的依赖插件。
"""

from __future__ import annotations

import inspect
import logging

from ..config.model import AppConfig
from ..di.container import Container
from ..plugins.contract import PluginContext, PluginSpec
from ..plugins.errors import AssemblyError
from ..plugins.registry import PluginRegistry
from .report import AssemblyReport, AssemblyStep

logger = logging.getLogger("app.assembly")


class Assembler:
    """根据配置与注册表完成确定性装配。"""

    def __init__(self, registry: PluginRegistry, config: AppConfig) -> None:
        self._registry = registry
        self._config = config

    def _enabled_specs(self, order: list[PluginSpec]) -> list[PluginSpec]:
        """按拓扑序过滤禁用插件（显式 enabled=false 才禁用）。"""
        result: list[PluginSpec] = []
        for spec in order:
            pc = self._config.plugins.get(spec.manifest.name)
            # 配置未显式列出的插件默认启用（显式 enabled=false 才禁用）
            if pc is None or pc.enabled:
                result.append(spec)
            else:
                logger.info("插件 %s 被配置禁用，跳过", spec.manifest.name)
        return result

    def _validate_enabled_dependencies(self, specs: list[PluginSpec]) -> None:
        """启用插件所依赖的提供者也必须处于启用集合中。"""
        enabled_names = {s.manifest.name for s in specs}
        for spec in specs:
            for req in spec.manifest.requires:
                provider = self._registry.provider_of(req.interface)
                if provider.manifest.name not in enabled_names:
                    raise AssemblyError(
                        plugin=spec.manifest.name,
                        reason=(f"依赖的插件 {provider.manifest.name!r} 已被禁用或缺失"),
                        chain=[provider.manifest.name, spec.manifest.name],
                        assembled=[],
                    )

    def _validate_scope_rules(self, specs: list[PluginSpec]) -> None:
        """请求级插件不得被应用级插件依赖（否则单例会持有请求级实例）。"""
        by_name = {s.manifest.name: s for s in specs}
        for spec in specs:
            m = spec.manifest
            pc = self._config.plugins.get(m.name)
            if pc is not None and pc.scope != m.scope:
                raise AssemblyError(
                    plugin=m.name,
                    reason=(f"配置作用域 {pc.scope!r} 与清单声明 {m.scope!r} 不一致"),
                    chain=[m.name],
                    assembled=[],
                )
            if m.scope != "app":
                continue
            for req in spec.manifest.requires:
                provider = self._registry.provider_of(req.interface)
                dep = by_name.get(provider.manifest.name)
                if dep is not None and dep.manifest.scope == "request":
                    raise AssemblyError(
                        plugin=spec.manifest.name,
                        reason=(f"应用级插件不能依赖请求级插件 {dep.manifest.name!r}"),
                        chain=[dep.manifest.name, spec.manifest.name],
                        assembled=[],
                    )

    def _failure_chain(self, failed: PluginSpec) -> list[str]:
        """构造失败链路：最深层根因插件 -> ... -> 装配失败的插件。

        沿失败插件的依赖边做确定性后序 DFS（依赖按注册序展开），
        回答“失败经由怎样一条依赖链传导到失败点”。
        """
        order = {s.manifest.name: i for i, s in enumerate(self._registry.all_specs())}
        chain: list[str] = []

        def visit(name: str, seen: frozenset[str]) -> None:
            if name in seen:
                return
            seen = seen | {name}
            spec = self._registry.get(name)
            deps = [
                self._registry.provider_of(req.interface).manifest.name
                for req in spec.manifest.requires
            ]
            deps.sort(key=lambda n: order[n])
            for dep in deps:
                visit(dep, seen)
            chain.append(name)

        visit(failed.manifest.name, frozenset())
        return chain

    async def assemble(
        self, reuse: dict[str, object] | None = None
    ) -> tuple[Container, AssemblyReport]:
        """执行完整装配，返回容器与报告；失败回滚并抛 AssemblyError。

        ``reuse``：插件名 -> 既有应用级实例（热更新时配置与实现均未变化的
        插件）。被复用的插件跳过工厂与 on_start，直接登记到本次容器；
        它们不进入 started 列表，装配失败回滚时**不会**被 on_stop，
        所有权仍属于旧装配。
        """
        reuse = reuse or {}
        report = AssemblyReport(source_order=list(self._config.source_order))
        order = self._registry.resolve_order()
        specs = self._enabled_specs(order)
        self._validate_scope_rules(specs)
        self._validate_enabled_dependencies(specs)

        logger.info("装配顺序: %s", [s.manifest.name for s in specs])
        if reuse:
            logger.info("复用既有实例: %s", sorted(reuse))

        container = Container()
        started: list[PluginSpec] = []

        for spec in specs:
            m = spec.manifest
            pc = self._config.plugins.get(m.name)
            options = pc.options if pc is not None else {}
            if m.scope == "app" and m.name in reuse:
                instance = reuse[m.name]
                for interface in (None, *m.provides):
                    container.register_instance(m.name, interface, instance)
                report.record_step(AssemblyStep(m.name, m.provides, m.scope, "reused"))
                logger.info("复用装配: %s (app)", m.name)
                continue
            try:
                if spec.validate_options is not None:
                    spec.validate_options(options)
                instance = await self._instantiate(spec, container)
                if m.scope == "app":
                    # 按所有提供接口登记到同一单例（by_name 只登记一次）
                    for interface in (None, *m.provides):
                        container.register_instance(m.name, interface, instance)
                else:
                    container.register_request_spec(spec, options)
                if spec.on_start is not None:
                    await self._maybe_await(spec.on_start(instance))
                    started.append(spec)
            except AssemblyError:
                raise
            except Exception as exc:  # noqa: BLE001 - 含插件主动拒绝，统一转 AssemblyError
                # 插件 validate_options 主动拒绝（PluginConfigRejectedError）与工厂/
                # 启动失败走同一条失败路径：记录失败点、逆序回滚本次尝试已启动的
                # 插件、释放本次容器，绝不留下已初始化未回收的实例。
                report.record_step(AssemblyStep(m.name, m.provides, m.scope, "failed", str(exc)))
                chain = self._failure_chain(spec)
                await self._rollback(container, started, report)
                report.ok = False
                report.failure_plugin = m.name
                report.failure_reason = str(exc)
                report.failure_chain = chain
                logger.error("装配失败于 %s: %s\n%s", m.name, exc, report.describe())
                raise AssemblyError(
                    plugin=m.name,
                    reason=str(exc),
                    chain=chain,
                    assembled=[s.manifest.name for s in started],
                    rolled_back=list(report.rolled_back),
                    cause=exc,
                ) from exc
            else:
                report.record_step(AssemblyStep(m.name, m.provides, m.scope, "ok"))
                logger.info("装配完成: %s (%s)", m.name, m.scope)

        logger.info("装配成功: %s", report.assembled)
        return container, report

    async def _instantiate(self, spec: PluginSpec, container: Container) -> object:
        """创建插件实例，依赖从已装配的容器中解析。"""
        m = spec.manifest
        deps = {}
        for req in m.requires:
            deps[req.interface] = container.get(req.interface)
        pc = self._config.plugins.get(m.name)
        options = pc.options if pc is not None else {}
        ctx = PluginContext(options=options, dependencies=deps)
        instance = spec.factory(ctx)
        if inspect.isawaitable(instance):
            instance = await instance
        return instance

    async def _maybe_await(self, result: object) -> None:
        if inspect.isawaitable(result):
            await result

    async def _rollback(
        self,
        container: Container,
        started: list[PluginSpec],
        report: AssemblyReport,
    ) -> None:
        """对已执行 on_start 的插件逆序执行 on_stop。"""
        for spec in reversed(started):
            instance = container.get_by_name(spec.manifest.name)
            if spec.on_stop is not None:
                try:
                    await self._maybe_await(spec.on_stop(instance))
                except Exception as exc:  # noqa: BLE001
                    logger.error("回滚 %s 时再失败: %s", spec.manifest.name, exc)
            report.rolled_back.append(spec.manifest.name)
            report.record_step(
                AssemblyStep(
                    spec.manifest.name,
                    spec.manifest.provides,
                    spec.manifest.scope,
                    "rolled-back",
                )
            )
        await container.dispose()
        logger.info("已回滚插件: %s", report.rolled_back)
