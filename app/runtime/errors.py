"""运行期热更新的错误类型。"""

from __future__ import annotations

from ..plugins.errors import PluginError


class ReloadError(PluginError):
    """一次热更新失败；携带失败阶段、原因与可定位链路。

    抛出时旧装配保持对外服务，应用不进入中间态。
    """

    def __init__(
        self,
        stage: str,
        reason: str,
        chain: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.stage = stage
        self.reason = reason
        self.chain = chain or []
        self.cause = cause
        chain_text = " -> ".join(self.chain) if self.chain else "<无>"
        super().__init__(
            f"热更新失败于阶段 {stage!r}: {reason} | 链路: {chain_text}；已保持原有装配"
        )
