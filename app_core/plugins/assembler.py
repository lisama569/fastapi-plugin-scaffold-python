"""依赖解析、兼容性校验与确定性装配。

启动阶段严格完成以下检查，任何一项不满足都立即拒绝、绝不推迟到运行时：

1. 配置启用的插件全部已注册（未知插件 -> PluginNotFoundError）；
2. 插件声明的依赖全部存在（-> PluginNotFoundError，带引用方）；
3. 依赖图无环（-> CircularDependencyError，携带环路径）；
4. 插件 ``api_level`` 落在宿主支持区间（-> IncompatiblePluginError）；
5. 依赖的最低版本满足（-> IncompatiblePluginError）。

装配按拓扑序依次 ``configure -> startup``；中途失败时逆序对已启动
插件执行 ``shutdown`` 回滚，并抛出携带失败链路与回滚清单的
:class:`~app_core.errors.AssemblyError`。
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app_core.config.schema import AppSettings
from app_core.errors import (
    AssemblyError,
    CircularDependencyError,
    IncompatiblePluginError,
    PluginNotFoundError,
)
from app_core.plugins.contracts import (
    MIN_SUPPORTED_API_LEVEL,
    CURRENT_API_LEVEL,
    Plugin,
    RequestScopedPlugin,
    compare_versions,
)
from app_core.plugins.container import Container, Scope
from app_core.plugins.registry import PluginRegistry

logger = logging.getLogger("app_core.assembly")


class Assembler:
    def __init__(self, registry: PluginRegistry, settings: AppSettings) -> None:
        self._registry = registry
        self._settings = settings

    # ------------------------------------------------------------------
    def _selected_names(self) -> list[str]:
        requested = list(self._settings.plugins)
        if not requested:
            return []
        if requested == ["*"]:
            return self._registry.names()
        for name in requested:
            if name not in self._registry:
                raise PluginNotFoundError(name)
        # 保留配置中的启用顺序作为根顺序，依赖会在排序时自然提前
        return requested

    def _validate_compatibility(self, name: str) -> None:
        plugin = self._registry.get(name)
        meta = plugin.metadata
        if not (MIN_SUPPORTED_API_LEVEL <= meta.api_level <= CURRENT_API_LEVEL):
            raise IncompatiblePluginError(
                name,
                f"api_level={meta.api_level} 超出宿主支持区间 "
                f"[{MIN_SUPPORTED_API_LEVEL}, {CURRENT_API_LEVEL}]",
            )
        for req in meta.requires:
            if req.name not in self._registry:
                raise PluginNotFoundError(req.name, referenced_by=name)
            dep_meta = self._registry.get(req.name).metadata
            if compare_versions(dep_meta.version, req.min_version) < 0:
                raise IncompatiblePluginError(
                    name,
                    f"依赖 {req.name} 需要 >= {req.min_version}，"
                    f"实际为 {dep_meta.version}",
                )

    # ------------------------------------------------------------------
    def resolve_order(self) -> list[str]:
        """返回确定性的拓扑装配顺序。

        - 只对"启用集合闭包"（启用插件及其全部依赖）排序；
        - 同层插件按名称排序，保证多进程/多实例下装配顺序一致。
        """
        roots = self._selected_names()

        # 收集闭包并做存在性 / 兼容性校验
        graph: dict[str, list[str]] = {}
        stack = list(roots)
        seen: set[str] = set()
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            if name not in self._registry:
                raise PluginNotFoundError(name)
            self._validate_compatibility(name)
            deps = [req.name for req in self._registry.get(name).metadata.requires]
            graph[name] = deps
            stack.extend(deps)

        # DFS 拓扑排序（依赖先于依赖方），环检测携带具体环路径
        order: list[str] = []
        state: dict[str, int] = defaultdict(int)  # 0 未访问 / 1 访问中 / 2 完成
        stack_trace: list[str] = []

        def visit(node: str) -> None:
            state[node] = 1
            stack_trace.append(node)
            for dep in sorted(graph[node]):
                if state[dep] == 0:
                    visit(dep)
                elif state[dep] == 1:
                    start = stack_trace.index(dep)
                    raise CircularDependencyError(stack_trace[start:] + [dep])
            state[node] = 2
            stack_trace.pop()
            order.append(node)

        for root in sorted(seen):
            if state[root] == 0:
                visit(root)
        return order

    # ------------------------------------------------------------------
    async def assemble(self, container: Container) -> list[str]:
        """执行装配，返回装配顺序；失败时回滚并抛 AssemblyError。"""
        order = self.resolve_order()
        logger.info("插件装配顺序: %s", order)
        started: list[str] = []
        for name in order:
            plugin = self._registry.get(name)
            logger.info("装配插件: %s@%s (scope=%s)",
                        name, plugin.metadata.version, plugin.metadata.scope.value)
            try:
                plugin.configure(self._settings.plugin_config.get(name, {}))
                if plugin.metadata.scope is Scope.APP:
                    instance = await plugin.startup(container)
                    await container.set_app_instance(name, instance)
                else:
                    self._register_request_factory(container, name, plugin)
            except Exception as exc:  # noqa: BLE001 - 需统一包装为 AssemblyError
                logger.error(
                    "插件 %s 启动失败，失败链路: %s",
                    name, started + [name],
                )
                rolled_back = await self.rollback(started, container)
                raise AssemblyError(
                    name, started + [name], rolled_back, exc
                ) from exc
            started.append(name)
        return order

    def _register_request_factory(
        self, container: Container, name: str, plugin: Plugin
    ) -> None:
        # 工厂在容器上注册一次，每个 RequestContainer 构建时获得副本。
        async def factory(rc: object) -> object:
            if isinstance(plugin, RequestScopedPlugin):
                return await plugin.create_request_instance(rc)  # type: ignore[attr-defined]
            return await plugin.startup(rc)

        container.register_request_factory(name, factory)

    async def rollback(
        self, started: list[str], container: Container
    ) -> list[str]:
        """对已启动的应用级插件逆序 shutdown，返回已回滚清单。"""
        rolled_back: list[str] = []
        for name in reversed(started):
            plugin = self._registry.get(name)
            if plugin.metadata.scope is not Scope.APP:
                continue
            logger.warning("回滚插件: %s", name)
            try:
                await plugin.shutdown(container)
            except Exception:  # noqa: BLE001 - 回滚需继续，不掩盖原始失败
                logger.exception("插件 %s 回滚时再次出错（已忽略）", name)
            await container.remove_app_instance(name)
            rolled_back.append(name)
        return rolled_back

