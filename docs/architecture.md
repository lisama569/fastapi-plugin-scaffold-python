# 架构说明

## 总览

```
            ┌──────────────────────────────────────────────┐
配置文件 ──▶ │ loader: defaults<file<env<override → 校验     │ ── AppSettings
环境变量 ──▶ │        (provenance 逐键记录来源)              │    + provenance
overrides ─▶└──────────────────────────────────────────────┘
                                   │
            ┌──────────────────────▼───────────────────────┐
            │ registry: register / discover（去重、拒重复） │
            └──────────────────────┬───────────────────────┘
                                   ▼
            ┌──────────────────────────────────────────────┐
            │ assembler: 闭包收集 → 兼容校验 → 拓扑排序      │
            │            → configure/startup（失败即回滚）   │
            └──────────────────────┬───────────────────────┘
                                   ▼
        ┌──────────────────── Container (APP 单例, lock 保护) ───────────────────┐
        │                                                                       │
        ├── RequestContainer(请求1: REQUEST 实例惰性创建, aclose 释放)            │
        ├── RequestContainer(请求2: ...)                                         │
        └───────────────────────────────────────────────────────────────────────┘
```

## 配置合并细节

- 合并在叶子键进行：嵌套 dict 先展平（`plugin_config.cache.ttl`），
  逐层覆盖后再还原。因此 `file` 里给 `cache.host`、`env` 里只给
  `cache.ttl`，两者会共存。
- `provenance` 的值固定为 `defaults` / `file:<path>` / `env` / `override`，
  既是日志内容，也可以在测试中稳定断言。
- 校验在合并完成后统一进行（fail-fast，一次暴露第一个错误），错误类
  带有结构化字段（`key`、`value`、`expected_type`、`reason`）。

## 拓扑排序的确定性

1. 从配置启用的插件出发，沿 `requires` 收集闭包；
2. 收集过程中完成注册存在性、api_level 与依赖版本校验；
3. DFS 三色标记：遇到"访问中"节点即找到环，环从该节点在栈中的位置切出；
4. 根按名称排序、邻接边也按名称排序遍历，保证任何机器上输出顺序一致。

## 并发安全

- 应用级实例在装配阶段（应用接受请求之前）全部创建完毕；容器内部仍用
  `asyncio.Lock` 保护写入，`build_request_container` 每次复制请求级
  工厂表，请求容器之间不共享任何可变状态。
- 请求级实例在请求容器内"双检 + 锁"惰性创建，同一请求内多次取用拿到
  同一对象；`aclose()` 调用实例的 `aclose()`/`close()` 后清空实例表，
  并标记容器关闭，杜绝泄漏与复用。

## 编写一个新插件

1. 在 `plugins_example/` 下新建模块，定义 `BasePlugin` 子类并设置
   `PluginMetadata`；
2. 在模块末尾导出 `PLUGINS = [MyPlugin()]`；
3. 应用级插件在 `startup` 中返回单例对象；请求级插件
   （`scope=Scope.REQUEST`）实现 `create_request_instance(rc)`，
   可经 `rc.get_app_instance(...)` 读取依赖的应用级单例；
4. 需要私有配置时从 `[plugin_config.<插件名>]` 读取，`configure(config)`
   接收该子表；
5. 可选实现 `register_routes(router)` 自行挂载 FastAPI 路由。

## 日志可观测性

- `app_core.config`：来源优先级 + 每个配置键的来源；
- `app_core.assembly`：最终装配顺序、每个插件的版本与作用域、
  失败时的失败链路、回滚动作；
- `app_core.lifespan`：启动完成清单与关闭顺序。

`pytest --log-cli-level=INFO` 可在本地直接观察上述输出。
