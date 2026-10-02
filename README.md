# FastAPI 插件化脚手架

可配置的插件装配与依赖注入：多来源配置分层覆盖、启动期确定性装配、
应用级/请求级作用域隔离、失败即回滚并拒绝启动；支持**运行期热更新**——
配置变化无需重启即可安全切换装配，失败整体回退。

详细设计见 [`docs/architecture.md`](docs/architecture.md)。

## 快速开始

```bash
uv sync --group dev
uv run pytest -q                                   # 运行测试（含并发/边界场景）
uv run uvicorn main:app --reload                   # 启动示例
curl -s http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/assembly-report
```

## 运行期热更新

修改配置文件（或环境变量）后，无需重启即可重新装配：

```bash
# 1. 修改 config.example.yaml（如改 greeter 的 greeting、关掉某个插件）
# 2. 触发热更新
curl -s -X POST http://127.0.0.1:8000/admin/reload
# 3. 验证新配置已生效
curl -s http://127.0.0.1:8000/
```

语义保证：

* **原子切换**：更新成功后新请求用新装配；在途请求继续用旧实例跑完，
  不会被打断，也不会出现新旧实例混用；
* **失败整体回退**：配置不合法/依赖不满足/装配中途报错时，放弃本次更新，
  原装配继续服务，响应与日志给出失败阶段、原因与可定位的失败链路；
* **少做无用功**：配置与实现都没变的应用级插件沿用旧实例（不重复连接
  外部资源）；依赖被重建的插件会一并重建，杜绝混用；
* **并发确定**：多处同时触发更新时串行执行，后到者基于最新装配做差量。

运行期可生效：插件 `enabled` 开关、插件 `options`、`log_level`。
必须重启：新增/升级插件代码、`app_name`。详见
[`docs/architecture.md`](docs/architecture.md) 第 4 节。

## 配置优先级（低 → 高，固定不可变）

`defaults(内置默认)` < `file(YAML/JSON)` < `env(FASTAPI_PLUGIN_*)` < `overrides(显式)`

逐键深度合并；必填缺失、类型不符、取值非法、结构冲突都会抛出**可区分**的错误，
绝不静默套用默认值。

## 定义一个插件

```python
from app.plugins.contract import (
    InterfaceRequirement, PluginContext, PluginManifest, PluginSpec,
)

def factory(ctx: PluginContext):
    clock = ctx.dependencies["clock"]      # 已注入的应用级单例
    return MyService(clock, **ctx.options)

SPEC = PluginSpec(
    manifest=PluginManifest(
        name="my-service",
        version="1.0.0",
        provides=("my-service",),
        requires=(InterfaceRequirement("clock", "^1.0.0"),),
        scope="request",                   # 或 "app"
    ),
    factory=factory,
    validate_options=lambda opts: None,   # 启动前配置自检
)
```

在启动期，重名/重复接口/缺依赖/版本不兼容/循环依赖会被**明确拒绝**；
装配中途失败会**逆序回滚**已启动插件，并给出可定位的失败链路。

## 注入到路由

```python
from fastapi import Depends
from app.ext.fastapi_lifespan import requires

@app.get("/")
async def index(svc: MyService = Depends(requires("my-service"))):
    ...
```
