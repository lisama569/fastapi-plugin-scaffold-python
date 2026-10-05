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

## 4. 运行期热更新

启动后可通过 `POST /reload`（或编程式 `await supervisor.reload()`）在不重启进程的
情况下重新加载配置并重新装配插件。编排由 `app/runtime/supervisor.py` 的
`PluginSupervisor` 完成，一次热更新分五步：

1. **重载配置**：按与启动期完全相同的来源与优先级重新加载、合并、校验；
2. **计算复用计划**：配置（enabled/scope/options）未变、且全部依赖也可复用的
   应用级插件，沿用旧实例——不重复执行工厂与 `on_start`，避免重复连接外部资源；
   任一依赖需要重建时，依赖方**级联重建**（绝不让新实例持有旧依赖）；
3. **增量装配**：用新配置 + 复用计划装配出一套**全新**容器；
4. **原子切换**：新状态（容器 + 报告 + 配置）整组替换 app.state 上的旧状态，
   此后新请求走新装配；
5. **退役旧装配**：旧容器不再派生新请求作用域，等在途请求排空后，
   对未沿用的旧实例逆序执行 `on_stop` 并释放容器。

### 一致性保证

* **单请求不混用**：请求中间件在请求**起点**一次性取当前容器，该请求自始至终
  持有起点那套装配；更新期间在途请求用旧实例正常跑完，不被打断。
* **失败整体回退**：新配置不合法、依赖不满足或装配中途任何插件报错，
  本次更新整体放弃——新装配中已启动的插件逆序回滚，复用实例所有权仍归旧装配
  （不会被回滚波及），旧装配继续对外服务。调用方收到 `ReloadError`，
  携带失败阶段（`config` / `assembly`）、原因与可定位的失败链路；
  `POST /reload` 返回 `{"ok": false, "stage", "reason", "failure_chain"}`。

### 插件主动拒绝配置（`PluginConfigRejectedError`）

插件在 `validate_options` 中判定配置非法时，抛
`PluginConfigRejectedError(plugin, reason)` **主动拒绝**本次更新。
这类拒绝与工厂/`on_start` 抛错**完全同等对待**：

* 装配器记录失败步骤、计算失败链路（最深根因 → 失败点），对本次尝试中
  已 `on_start` 的插件**逆序执行 `on_stop`** 并释放新容器——不会留下
  已初始化却未回收的实例，也不会出现半新半旧的状态；
* 被复用的旧实例所有权仍归旧装配，回滚不会波及它们；
* 监督器将其包装为 `ReloadError(stage="assembly")`，`POST /reload`
  返回 **HTTP 200** + `{"ok": false, "stage": "assembly", "reason",
  "failure_chain": [...]}`（`reason` 含“插件主动拒绝配置”与插件给出的原因），
  而不是 500；旧装配继续对外服务，同进程其它请求不受影响；
* 把配置改回合法后再次 `POST /reload` 即可成功切换，应用不进入中间态。

无论失败由哪个配置来源触发（配置文件 / 环境变量 / overrides），也无论
失败点位于依赖链的哪一层，返回的 `failure_chain` 都能定位到失败插件及其
依赖传导路径；并发或连续触发时所有更新在锁上串行执行，结果确定、可解释。
* **并发触发确定可解释**：所有热更新在一把锁上串行执行，每次都在拿到锁后
  重新加载最新配置——最终结果确定（以最后一次完成的更新为准），互不踩踏；
  配置已无变化的后续更新会整体复用，不做无用功。
* **应用级单例不重复初始化**：配置与实现都未变的应用级插件在成功更新后
  仍是同一个实例（如 `clock` 的 tick 计数在更新前后连续累加）。

### 哪些变更可以运行期生效

| 变更                                   | 运行期生效 | 说明                                       |
| -------------------------------------- | ---------- | ------------------------------------------ |
| 插件 `options` 参数调整                | ✅         | 该插件（及其依赖方）重建，其余复用         |
| 启用/禁用某个插件（`enabled`）         | ✅         | 被依赖者不可禁用（装配校验会拒绝并回退）   |
| 新增已在代码中注册、但原配置禁用的插件 | ✅         | 仅装配新插件，未变插件复用                 |
| 新增/移除/升级插件**代码**             | ❌ 需重启  | 注册表在进程启动时构建，热更新不重载代码   |
| 修改插件 `scope`                       | ❌ 需重启  | 配置 scope 必须与清单一致，变更属代码变更  |
| 配置来源结构（增删配置文件/env 前缀）  | ❌ 需重启  | 来源与优先级在 `create_app` 时固定         |

---

## 5. 错误分类

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
| `PluginConfigRejectedError`   | 装配期     | 插件 `validate_options` 主动拒绝配置；装配器统一包装为 `AssemblyError`（热更新时再包装为 `ReloadError`），携带失败链路与回滚列表 |
| `AssemblyError`               | 装配期     | 工厂/钩子失败，携带失败链路与回滚列表  |

### 运行期热更新（`app/runtime/errors.py`）

| 异常           | 触发阶段            | 场景                                             |
| -------------- | ------------------- | ------------------------------------------------ |
| `ReloadError`  | 热更新 config 阶段  | 新配置加载/校验失败（包装底层配置错误）          |
| `ReloadError`  | 热更新 assembly 阶段| 新装配失败（包装 AssemblyError，携带失败链路）   |

`ReloadError` 抛出时旧装配保持对外服务，应用不进入中间态；修复配置后可再次触发更新。

这些错误互不重叠，调用方可按异常类型稳定区分失败原因。

---

## 6. 本地验证方法

前置：已安装 `uv`（Python 3.14）。

```bash
# 安装依赖（含 dev 测试依赖）
uv sync --group dev

# 运行全部单测（62 个），日志会打印配置来源、装配顺序、失败链路
uv run pytest -q
uv run pytest -q -o log_cli=true -o log_cli_level=DEBUG   # 实时查看装配日志

# 代码检查与格式化
uvx ruff check app tests main.py
uvx ruff format app tests main.py

# 启动示例应用
uv run uvicorn main:app --reload
curl -s http://127.0.0.1:8000/             # 演示依赖注入
curl -s http://127.0.0.1:8000/assembly-report

# 验证热更新：修改 config.example.yaml（如把 greeter 的 greeting 改掉，
# 或把某个插件 enabled 置为 false），然后：
curl -s -X POST http://127.0.0.1:8000/reload
# -> {"ok": true, "reused": ["clock"], "rebuilt": [], ...}
curl -s http://127.0.0.1:8000/             # 新配置已生效，无需重启

# 验证失败回退：把 greeting 改成空白字符串（greeter.validate_options 会
# 主动拒绝），再触发更新：
curl -s -X POST http://127.0.0.1:8000/reload
# -> HTTP 200 {"ok": false, "stage": "assembly",
#              "reason": "插件主动拒绝配置: greeting 必须是非空字符串",
#              "failure_chain": ["clock", "greeter"]}   （不是 500）
curl -s http://127.0.0.1:8000/             # 旧装配照常服务
# 本次尝试中已启动的插件已被逆序回滚（日志可见“已回滚插件”）；
# 把 greeting 改回合法值后再 POST /reload 即可成功切换。
# 服务日志（或 pytest -o log_cli=true）能看到配置来源、装配顺序与失败链路。
```

测试覆盖（`tests/`）：

* `test_config.py`：来源优先级、逐键深度合并、env 类型推断、五类配置错误、结构冲突；
* `test_plugins.py`：重名/重复接口、缺依赖、版本不兼容、循环/自依赖、拓扑确定性、发现；
* `test_assembly.py`：装配顺序、注入、禁用过滤、选项拒绝、工厂/启动失败回滚、
  作用域约束、失败链路；
* `test_scopes.py`：并发请求共享单例、请求级隔离与不泄漏、释放保护、幂等释放；
* `test_api.py`：HTTP 端到端注入、20 并发单例共享、env 覆盖、各类启动期失败；
* `test_hotreload.py`：热更新成功切换与未变复用、依赖级联重建、失败整体回退
  （配置/装配两阶段，含插件主动拒绝配置时的回滚与旧装配保活）、
  并发/连续触发串行化与确定性、在途请求不被打断、HTTP 端到端热更新
  （配置文件与 overrides 两种来源触发）。
