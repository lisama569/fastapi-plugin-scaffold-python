# 插件化装配与依赖注入 · 架构说明

本脚手架让同一套 FastAPI 应用在不同部署配置下，**确定性地加载、组装并隔离**各组件。
所有装配决策都在应用启动阶段完成；任何配置或插件问题都会让应用**拒绝启动**，
绝不带病进入服务状态或把错误推迟到请求运行时。

---

## 1. 配置来源与覆盖优先级

配置由四个来源合并而成，优先级**固定且不可由配置自身改变**（数值越大优先级越高）：

| 优先级 | 来源         | 标识                  | 说明                                            |
| ----: | ------------ | --------------------- | ----------------------------------------------- |
|    10 | `defaults`   | `defaults`            | 应用内置默认（代码中传入）                      |
|    20 | 配置文件     | `file:<路径>`         | YAML / JSON 文件（按扩展名解析）                |
|    30 | 环境变量     | `env`                 | 前缀 `FASTAPI_PLUGIN_`，`__` 表示嵌套层级       |
|    40 | 显式覆盖     | `overrides`           | 编程式 `overrides=`，最高优先级                 |

合并规则：

* **逐键深度合并**：字典递归合并，标量整键替换。高优先级只覆盖它显式给出的键，
  不会把低优先级的同级其它键冲掉。
* 每个叶子键最终“由哪个来源生效”会记录在
  `LoadResult.key_sources`，并在启动日志中打印，覆盖链路完全可解释。
* 环境变量值用 JSON 字面量推断类型（`true`/`false`/数字/字符串），推断失败按字符串处理。
  例：`FASTAPI_PLUGIN_PLUGINS__CLOCK__ENABLED=false`
  → `plugins.clock.enabled = False`。

示例（低 → 高）：

```
defaults:            app_name=default-app, log_level=info
file:config.yaml:    app_name=from-file,   log_level=warning
env:                 log_level=error
overrides:           app_name=from-override

最终: app_name=from-override(overrides), log_level=error(env)
```

### 配置结构

```yaml
app_name: fastapi-plugin-scaffold
log_level: info            # debug|info|warning|error|critical
plugins:
  clock:
    enabled: true
    scope: app             # app|request，必须与插件清单声明一致
    options: {}            # 透传给插件工厂的自由配置
```

---

## 2. 插件契约与兼容规则

一个插件由 `PluginSpec` 描述（见 `app/plugins/contract.py`）：

* `manifest`：`name`、`version`（`x.y.z`）、`provides`（提供的接口名元组）、
  `requires`（对其它接口的 `InterfaceRequirement(interface, version)` 依赖）、
  `scope`（`app` / `request`）。
* `factory(ctx) -> instance`：同步或异步工厂，`ctx.dependencies` 已按接口名注入依赖，
  `ctx.options` 为该插件的配置。
* `on_start` / `on_stop`：应用级生命周期钩子（异步）。
* `validate_options(options)`：启动前配置自检，抛异常即拒绝装配。

### 兼容规则（全部在启动阶段判定，不满足即拒绝）

1. **唯一命名**：同名插件重复注册 → `DuplicatePluginError`。
2. **唯一接口提供者**：同一接口被多个插件 `provides` → `DuplicatePluginError`
   （注册当下即拒绝，不允许静默择一）。
3. **依赖必须存在**：`requires` 的接口无提供者 → `PluginNotFoundError`。
4. **版本必须兼容**：`^x.y.z` 表示同主版本且不低于（主版本 0 时退化为同次版本）；
   精确版本必须完全相等。不满足 → `VersionIncompatibleError`。
5. **依赖图必须无环**：拓扑排序（Kahn，同级按注册序）检测到环
   → `CircularDependencyError`，错误信息给出具体环（首尾相接）。
6. **作用域约束**：应用级插件不得依赖请求级插件（否则单例会持有请求私有对象）
   → `AssemblyError`；配置声明的 `scope` 必须与清单一致。

### 接入方式

* **显式注册**：`create_app(plugin_specs=[...])`。
* **发现接入**：目标模块/包暴露 `PLUGINS: Iterable[PluginSpec]`，
  用 `app.plugins.discovery.discover_plugins("pkg.path")` 自动收集；
  模块无法导入、缺少 `PLUGINS` 或元素类型不符都会立即报错，不静默跳过。

---

## 3. 生命周期与作用域

| 作用域    | 创建时机             | 生命周期             | 隔离性                       |
| --------- | -------------------- | -------------------- | ---------------------------- |
| `app`     | 启动装配时创建一次   | 整个 FastAPI lifespan | 全局单例，并发请求共享       |
| `request` | 首次被该请求取用时   | 单次 HTTP 请求       | 每请求独立实例，结束即释放   |

* 装配按**拓扑序**进行（被依赖者先装配）；实例化后执行 `on_start`。
* **中途失败回滚**：任一插件工厂或 `on_start` 失败，对已完成 `on_start` 的插件
  **逆序执行 `on_stop`**，随后释放容器，并抛出 `AssemblyError`。
  错误对象携带：失败插件、原因、失败链路（最深根因 → 失败点）、已完成列表、已回滚列表。
* 应用级实例登记进 `Container`；请求级插件登记“创建规格”，在每个 `RequestScope`
  中惰性创建。请求级插件依赖应用级接口时，拿到的是同一个全局单例。
* 请求结束（含异常）由中间件保证 `RequestScope.dispose()` 被调用；
  在已释放的容器/作用域上取用会抛 `ContainerDisposedError`，杜绝跨请求泄漏。

---

## 4. 错误分类

### 配置（`app/config/errors.py`）

| 异常                    | 触发场景                                   |
| ----------------------- | ------------------------------------------ |
| `ConfigSourceError`     | 文件不存在、不可读、YAML/JSON 解析失败     |
| `ConfigMissingError`    | 必填项缺失（含插件 `enabled`/`scope`）     |
| `ConfigTypeError`       | 类型不匹配（携带期望/实际类型与来源）      |
| `ConfigValueError`      | 取值非法（枚举/范围，如 `log_level`、scope）|
| `ConfigConflictError`   | 同一路径在不同来源结构冲突（映射 vs 标量） |

### 插件与装配（`app/plugins/errors.py`）

| 异常                          | 触发阶段   | 场景                                   |
| ----------------------------- | ---------- | -------------------------------------- |
| `DuplicatePluginError`        | 注册期     | 同名插件 / 同接口重复提供              |
| `PluginNotFoundError`         | 图校验期   | 依赖接口无提供者                       |
| `VersionIncompatibleError`    | 图校验期   | 版本区间不满足                         |
| `CircularDependencyError`     | 拓扑排序期 | 依赖成环（携带具体环）                 |
| `PluginConfigRejectedError`   | 装配期     | 插件 `validate_options` 拒绝配置       |
| `AssemblyError`               | 装配期     | 工厂/钩子失败，携带失败链路与回滚列表  |

这些错误互不重叠，调用方可按异常类型稳定区分失败原因；它们均在启动阶段抛出。

---

## 5. 本地验证方法

前置：已安装 `uv`（Python 3.14）。

```bash
# 安装依赖（含 dev 测试依赖）
uv sync --group dev

# 运行全部单测（49 个），日志会打印配置来源、装配顺序、失败链路
uv run pytest -q
uv run pytest -q -o log_cli=true -o log_cli_level=DEBUG   # 实时查看装配日志

# 代码检查与格式化
uvx ruff check app tests main.py
uvx ruff format app tests main.py

# 启动示例应用
uv run uvicorn main:app --reload
curl -s http://127.0.0.1:8000/             # 演示依赖注入
curl -s http://127.0.0.1:8000/assembly-report
```

测试覆盖（`tests/`）：

* `test_config.py`：来源优先级、逐键深度合并、env 类型推断、五类配置错误、结构冲突；
* `test_plugins.py`：重名/重复接口、缺依赖、版本不兼容、循环/自依赖、拓扑确定性、发现；
* `test_assembly.py`：装配顺序、注入、禁用过滤、选项拒绝、工厂/启动失败回滚、
  作用域约束、失败链路；
* `test_scopes.py`：并发请求共享单例、请求级隔离与不泄漏、释放保护、幂等释放；
* `test_api.py`：HTTP 端到端注入、20 并发单例共享、env 覆盖、各类启动期失败。
