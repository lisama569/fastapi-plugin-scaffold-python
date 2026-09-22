# FastAPI 插件化脚手架

可配置的插件装配与依赖注入：多来源配置分层覆盖、启动期确定性装配、
应用级/请求级作用域隔离、失败即回滚并拒绝启动。

详细设计见 [`docs/architecture.md`](docs/architecture.md)。

## 快速开始

```bash
uv sync --group dev
uv run pytest -q                                   # 运行测试（含并发/边界场景）
uv run uvicorn main:app --reload                   # 启动示例
curl -s http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/assembly-report
```

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
