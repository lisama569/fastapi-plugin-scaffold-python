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

## 运行期热更新

改配置不用重启进程：`POST /reload` 会按相同来源与优先级重新加载配置、
增量装配并原子切换——新请求立即用新装配，在途请求用旧装配跑完，
配置未变的应用级插件复用旧实例（不重复初始化/连接外部资源）。

```bash
# 修改 config.example.yaml（如 greeter 的 greeting、某插件 enabled）后：
curl -s -X POST http://127.0.0.1:8000/reload
# 成功: {"ok": true, "reused": ["clock"], "rebuilt": [...], ...}
# 失败: {"ok": false, "stage": "config|assembly", "reason": ..., "failure_chain": [...]}
#       旧装配继续对外服务，修复配置后再发一次即可
```

### 插件自检拒绝配置时的行为

插件可在 `validate_options` 中对自己的配置做加载前自检，非法时抛
`PluginConfigRejectedError`（或任意异常）主动拒绝。运行期热更新撞上这种拒绝时：

* `POST /reload` 返回 **HTTP 200 + `{"ok": false, "stage": "assembly",
  "reason": <插件给出的原因>, "failure_chain": [根因插件, ..., 拒绝插件]}`**，
  而不是没有解释的 500；配置文件、env、`overrides` 任一来源触发都一样；
* 本次尝试整体回退：已经启动过的新实例按依赖逆序 `on_stop`、新容器释放，
  复用的旧实例与旧装配不受影响，继续对外服务，同进程其它请求无感；
* 把配置改回合法后再次 `POST /reload` 即可成功切换（可反复失败-修复-成功）；
* 并发/连续触发由单锁串行化，每次失败各自回滚各自的尝试，结果确定、不互相踩踏。

可运行期生效：插件参数（options）、启用/禁用插件、启用已注册的新插件。
仍需重启：增删/升级插件代码、改插件 scope、改配置来源结构。
失败回滚的本地验证步骤见 [`docs/architecture.md`](docs/architecture.md) 第 4、6 节。
