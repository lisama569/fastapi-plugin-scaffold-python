"""装配报告：记录配置来源、装配顺序与失败链路，供日志与测试断言。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AssemblyStep:
    plugin: str
    interfaces: tuple[str, ...]
    scope: str
    status: str  # "ok" | "failed" | "rolled-back"
    detail: str = ""


@dataclass
class AssemblyReport:
    source_order: list[str] = field(default_factory=list)
    steps: list[AssemblyStep] = field(default_factory=list)
    assembled: list[str] = field(default_factory=list)
    request_registered: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)
    failure_chain: list[str] = field(default_factory=list)
    failure_plugin: str | None = None
    failure_reason: str | None = None
    ok: bool = True

    def record_step(self, step: AssemblyStep) -> None:
        self.steps.append(step)
        if step.status == "ok":
            if step.scope == "app":
                self.assembled.append(step.plugin)
            else:
                self.request_registered.append(step.plugin)

    def describe(self) -> str:
        """生成稳定、可断言的多行文本描述。"""
        lines: list[str] = []
        lines.append(f"配置来源(低->高): {self.source_order}")
        lines.append("装配顺序:")
        for step in self.steps:
            lines.append(
                f"  - {step.plugin} 提供={list(step.interfaces)} "
                f"作用域={step.scope} 状态={step.status}"
                + (f" 详情={step.detail}" if step.detail else "")
            )
        if self.ok:
            lines.append(f"装配成功: 应用级 {self.assembled} | 请求级 {self.request_registered}")
        else:
            lines.append(f"装配失败于: {self.failure_plugin}: {self.failure_reason}")
            lines.append(f"失败链路: {' -> '.join(self.failure_chain)}")
            lines.append(f"已回滚: {self.rolled_back}")
        return "\n".join(lines)
