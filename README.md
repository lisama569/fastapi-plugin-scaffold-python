# FastAPI 插件脚手架（Python）

在 FastAPI 之上提供**可配置的插件装配与依赖注入**：同一套应用代码在不同
部署配置下，都能确定性地加载配置、注册/发现插件、解析依赖并完成装配，
应用级与请求级实例严格隔离；任何配置或装配问题都在**启动阶段**以可区分
的错误类型暴露，绝不静默套用默认值或推迟到运行时报错。

## 快速开始

```bash
uv sync                       # 安装运行期与测试依赖
uvicorn main:app --reload     # 使用 config.example.toml + 自动发现示例插件
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/counter/incr
```

## 目录结构

```
app_core/
  config/
    schema.py        # AppSettings / 来源优先级常量 / 溯源结构
    loader.py        # defaults < file < env < override 分层合并与校验
  plugins/
    contracts.py     # Plugin 协议、PluginMetadata、Requirement、版本规则
    registry.py      # 手动注册 + 包发现；重复注册立即拒绝
    container.py     # Container(应用级单例) / RequestContainer(请求级)
    assembler.py     # 拓扑排序 + 兼容性校验 + 装配/回滚
    lifespan.py      # FastAPI lifespan 接入
  app.py             # create_app 工厂、请求级 DI 依赖
  errors.py          # 全部错误分类
plugins_example/      # 可被自动发现的示例插件
tests/                # 配置 / 装配 / 生命周期 / HTTP 集成测试
config.example.toml
```

## 1. 配置来源与覆盖优先级

优先级**固定**（后者覆盖前者），在叶子键层面合并（嵌套表展平为点分键，
高优先级来源只覆盖同名叶子，不会整树替换）：

| 优先级 | 来源 | 来源标记 |
| ---: | --- | --- |
| 1（最低） | 内置默认（仅可选项有默认） | `defaults` |
| 2 | 配置文件 TOML/JSON | `file:<路径>` |
| 3 | 环境变量（前缀 `APP_`） | `env` |
| 4（最高） | 代码内 `overrides` | `override` |

环境变量映射：

- `APP_APP_NAME` / `APP_DEBUG` / `APP_LOG_LEVEL` 为标量；
- `APP_PLUGINS` 为逗号分隔数组；
- `APP_PLUGIN_CONFIG__CACHE__TTL=30` 按 `__` 分层映射到
  `plugin_config.cache.ttl`（只允许写入 `plugin_config.*` 命名空间，
  其余未知 `APP_*` 变量会被拒绝）。

每个配置键最终来自哪一层记录在 `LoadedConfig.provenance`，启动日志会逐键
打印，例如 `配置项 app_name <- env`。

**校验规则（不合法绝不静默回退）：**

- 必填项 `app_name` 缺失 → `MissingRequiredError`；
- 类型不匹配（如 `app_name = 123`、`debug = "maybe"`）→ `ConfigTypeError`；
- 取值非法（`log_level` 不在 DEBUG/INFO/WARNING/ERROR/CRITICAL、
  出现未知顶层键、插件名为空、未知环境变量）→ `InvalidValueError`；
- 文件不存在/无法解析/后缀不支持 → `ConfigFileError`。

## 2. 插件契约与兼容规则

插件需提供 `metadata: PluginMetadata` 与三个生命周期方法
（可继承 `BasePlugin` 获得默认实现）：

```python
from app_core.plugins.contracts import BasePlugin, PluginMetadata, Requirement
from app_core.plugins.container import Scope

class MyPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="my-plugin",
        version="1.4.0",
        requires=(Requirement("counter", min_version="1.0.0"),),
        scope=Scope.APP,          # 或 Scope.REQUEST
        api_level=1,
    )
```

- **接口级别**：宿主支持 `api_level ∈ [1, 1]`（见 contracts.py 常量）。
  超范围 → `IncompatiblePluginError`。
- **依赖版本**：`Requirement(name, min_version)` 按点分数字版本比较，
  被依赖方版本低于下限 → `IncompatiblePluginError`。
- **依赖存在性**：依赖未注册 → `PluginNotFoundError`（消息带引用方）。
- **重复注册**：同名插件再次注册（手动或发现冲突）→ `DuplicatePluginError`。
- **循环依赖**：DFS 检测环，错误中携带完整环路径（如 `a -> b -> c -> a`）
  → `CircularDependencyError`。
- **拓扑顺序确定**：依赖先于依赖方，同层按名称排序；启用集合取
  `plugins` 配置项及其依赖闭包，`plugins = ["*"]` 表示全部已注册插件。

### 作用域

| 作用域 | 实例创建时机 | 可见性 | 释放 |
| --- | --- | --- | --- |
| `Scope.APP` | 启动装配时一次 | 全部请求共享同一实例 | 应用关闭/装配失败回滚 |
| `Scope.REQUEST` | 请求内首次取用惰性创建 | 仅当前请求；不同请求实例不同 | 请求结束 `aclose()` |

请求内通过依赖注入获取 `RequestContainer`：应用级实例只读共享，
请求级实例惰性创建。并发请求下应用级实例**只初始化一次**，
请求级实例**不跨请求泄漏**。

### 注册与发现

```python
registry = PluginRegistry()
registry.register(MyPlugin())     # 手动
registry.discover("plugins_example")  # 自动发现包内模块的 PLUGINS 导出
```

发现约定：遍历目标包所有子模块，读取模块级 `PLUGINS = [...]` 序列；
若模块未定义 `PLUGINS`，则回退为收集该模块自身定义的插件实例成员。

## 3. 装配、回滚与失败链路

装配按拓扑序对每个插件执行 `configure(私有配置) -> startup(容器)`。
任一插件启动失败时：

1. 记录失败链路（到失败插件为止的装配顺序）；
2. 对已启动的应用级插件**逆序**调用 `shutdown`，并从容器撤销实例
   （回滚中再次出错只记日志，不掩盖原始失败）；
3. 抛出 `AssemblyError`，字段：
   - `plugin`：失败插件名；
   - `chain`：失败链路，如 `['counter', 'failing']`；
   - `rolled_back`：已回滚清单；
   - `cause`：插件抛出的原始异常。

通过 FastAPI lifespan 装配时，该错误会使应用**启动失败**（不会带病运行）。

## 4. 错误分类

所有错误继承 `ScaffoldError`，可按类型稳定区分：

| 错误 | 含义 |
| --- | --- |
| `MissingRequiredError` | 必填配置缺失 |
| `ConfigTypeError` | 配置类型不匹配 |
| `InvalidValueError` | 配置取值非法/未知键 |
| `ConfigFileError` | 配置文件缺失或解析失败 |
| `DuplicatePluginError` | 插件重复注册 |
| `PluginNotFoundError` | 插件/依赖未注册 |
| `CircularDependencyError` | 循环依赖（带环路径） |
| `IncompatiblePluginError` | api_level / 依赖版本不兼容 |
| `AssemblyError` | 装配中途失败（带失败链路与回滚清单） |

## 5. 本地验证

```bash
uv sync
uv run pytest                      # 全部单测（32 个）
uv run pytest --log-cli-level=INFO # 同时观察配置来源/装配顺序/失败链路日志
uv run pytest tests/test_config.py tests/test_assembly.py -v
uvicorn main:app                   # 真实启动，访问 /health 与 /counter/incr
```

测试覆盖重点：分层覆盖优先级、配置冲突/类型/非法值、插件重复注册、
循环依赖、版本与 api_level 不兼容、缺失依赖、装配中途失败的回滚链路、
200 并发请求共享同一单例、20 个并发请求的请求级实例互不泄漏。

更多设计细节见 [docs/architecture.md](docs/architecture.md)。
