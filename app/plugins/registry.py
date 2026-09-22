"""插件注册表：显式注册 + 依赖图校验（全部在启动阶段完成）。"""

from __future__ import annotations

from collections import defaultdict

from .contract import PluginSpec
from .errors import (
    CircularDependencyError,
    DuplicatePluginError,
    PluginNotFoundError,
    VersionIncompatibleError,
)
from .version import satisfies


class PluginRegistry:
    """维护已注册插件，并在启动阶段执行图级校验。"""

    def __init__(self) -> None:
        self._specs: dict[str, PluginSpec] = {}
        self._interface_provider: dict[str, PluginSpec] = {}

    def register(self, spec: PluginSpec) -> None:
        """注册插件；重名或接口重复提供立即拒绝。"""
        m = spec.manifest
        if m.name in self._specs:
            raise DuplicatePluginError(m.name, "同名插件已注册")
        for interface in m.provides:
            if interface in self._interface_provider:
                owner = self._interface_provider[interface].manifest.name
                raise DuplicatePluginError(
                    m.name, f"接口 {interface!r} 已由 {owner!r} 提供，禁止重复提供"
                )
        self._specs[m.name] = spec
        for interface in m.provides:
            self._interface_provider[interface] = spec

    def get(self, name: str) -> PluginSpec:
        """按插件名取规格，不存在则抛 PluginNotFoundError。"""
        try:
            return self._specs[name]
        except KeyError:
            raise PluginNotFoundError(name) from None

    def provider_of(self, interface: str) -> PluginSpec:
        """按接口名取唯一提供者。"""
        if interface not in self._interface_provider:
            raise PluginNotFoundError(interface)
        return self._interface_provider[interface]

    def all_specs(self) -> list[PluginSpec]:
        """返回全部已注册规格（按注册顺序，保证确定性）。"""
        return list(self._specs.values())

    # -- 图级校验 -------------------------------------------------------

    def _edges(self) -> dict[str, list[str]]:
        """构造 插件名 -> 它依赖的提供者插件名列表。"""
        edges: dict[str, list[str]] = defaultdict(list)
        for spec in self._specs.values():
            for req in spec.manifest.requires:
                provider = self.provider_of(req.interface)
                edges[spec.manifest.name].append(provider.manifest.name)
        return edges

    def validate_graph(self) -> None:
        """校验依赖存在性、版本兼容性与依赖无环。"""
        # 依赖存在性 + 版本兼容性（provider_of 已负责存在性）
        for spec in self._specs.values():
            for req in spec.manifest.requires:
                provider = self.provider_of(req.interface)
                if not satisfies(req.version, provider.manifest.version):
                    raise VersionIncompatibleError(
                        consumer=spec.manifest.name,
                        interface=req.interface,
                        required=req.version,
                        provider_version=provider.manifest.version,
                    )
        self.resolve_order()  # 内部执行环检测

    def resolve_order(self) -> list[PluginSpec]:
        """Kahn 拓扑排序；被依赖者排在前。检测到环抛 CircularDependencyError。"""
        edges = self._edges()
        indegree: dict[str, int] = {name: 0 for name in self._specs}
        dependents: dict[str, list[str]] = defaultdict(list)
        for name, deps in edges.items():
            indegree[name] = len(deps)
            for dep in deps:
                dependents[dep].append(name)

        # 以注册顺序作为同级次序，保证装配顺序确定且可解释
        ready = [name for name in self._specs if indegree[name] == 0]
        order: list[PluginSpec] = []
        while ready:
            name = ready.pop(0)
            order.append(self._specs[name])
            for dependent in dependents[name]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    # 按注册序插入，保持确定性
                    self._insert_in_registration_order(ready, dependent)
        if len(order) != len(self._specs):
            cycle = self._find_cycle(edges)
            raise CircularDependencyError(cycle)
        return order

    def _insert_in_registration_order(self, ready: list[str], name: str) -> None:
        idx = 0
        for idx, existing in enumerate(ready):
            if list(self._specs).index(name) < list(self._specs).index(existing):
                ready.insert(idx, name)
                return
        ready.append(name)

    def _find_cycle(self, edges: dict[str, list[str]]) -> list[str]:
        """在残余图中通过 DFS 找出一条具体的环，用于错误定位。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self._specs}
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            color[node] = GRAY
            stack.append(node)
            for nxt in edges.get(node, []):
                if color[nxt] == GRAY:
                    start = stack.index(nxt)
                    return stack[start:] + [nxt]
                if color[nxt] == WHITE:
                    found = visit(nxt)
                    if found is not None:
                        return found
            stack.pop()
            color[node] = BLACK
            return None

        for name in self._specs:
            if color[name] == WHITE:
                found = visit(name)
                if found is not None:
                    return found
        return list(self._specs)  # 理论不可达
