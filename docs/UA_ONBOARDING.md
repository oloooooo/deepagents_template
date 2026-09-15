# 新人上手：deepagents_template

> 本指南由 `/understand-onboard` 依据 `.ua/knowledge-graph.json` 生成
> （图谱 commit `295775ef`，145 节点 / 309 边 / 7 图层 / 15 导览步）。
> 图谱更新后建议重新生成本文。

## 1. 这是什么

FastAPI + PostgreSQL 的后端模板：OmegaConf + pydantic 配置、loguru 日志、用户注册登录与 JWT 鉴权、
**业务空间与成员权限（super 才能写）**、alembic 迁移，并为 deepagents（LangGraph 检查点 / 长期记忆）预留装配位。

| 项 | 内容 |
| --- | --- |
| 语言 | Python 3.14（`.venv` 由 uv 管理） |
| 框架 | FastAPI、SQLAlchemy(async)、Alembic、Pydantic、Loguru、OmegaConf、uvicorn |
| 数据库 | PostgreSQL（业务库 `postgresql.user`；deepagents 预留 `postgresql.deepagent`） |
| 入口 | `main.py` → `app`（Swagger 在 `/docs`） |

**5 分钟跑起来**

```bash
uv sync --frozen
# 改 config/config.yaml 里 postgresql.user 的连接信息
uv run alembic upgrade head        # 建 users / workspaces / user_workspaces
uv run python main.py              # http://127.0.0.1:8000
# 注册一个账号后，把自己设成 super（否则建不了空间，见第 4 节）
psql -d <库名> -c "update users set is_super = true where account = '你的账号'"
uv run python tests/test_auth.py   # 18 项自检
```

## 2. 架构分层

调用方向自上而下：**routers（收参/返回）→ services（业务规则）→ repositories（唯一写 SQL）→ models（表定义）**。

| 图层 | 说明 | 关键文件 |
| --- | --- | --- |
| **接口层：路由与依赖注入** | FastAPI 路由与依赖注入（请求级会话、Bearer → 当前用户、super 校验） | `routers/auth.py`、`routers/workspace.py`、`routers/schemas/*`、`dependencies/auth.py`、`dependencies/database.py` |
| **服务层：业务逻辑** | 鉴权规则与空间规则的唯一落点，不感知 HTTP | `services/auth.py`、`services/workspace.py` |
| **数据层：模型、仓库与迁移** | ORM 基类与表模型、仓储类、alembic 迁移 | `models/*`、`repositories/*`、`migrations/*`、`alembic.ini` |
| **基础设施：配置与日志** | 配置加载（OmegaConf + pydantic + `APP_*` 覆盖）与 loguru 日志基础设施 | `config/config.py`、`config/config.yaml`、`logger/__init__.py`、`pyproject.toml` |
| **应用入口与装配** | FastAPI 实例、lifespan、`/health`、uvicorn 参数；deepagents 装配位 | `main.py`、`agents/__init__.py` |
| **测试与自检** | 四个端到端脚本，无需 pytest，跑完自清理 | `tests/*.py` |
| **文档与协作规范** | 使用文档与给 AI 协作者的目录职责约定 | `README.md`、`AGENTS.md` |

## 3. 关键概念（读代码前先知道这几条）

1. **配置只有一份源**：`config/config.yaml`。`config/config.py` 负责「OmegaConf 读 → `APP_*` 环境变量覆盖 → pydantic 校验 → `app_config` 单例」，
   `extra="forbid"` 让写错键名在启动瞬间报错。其它模块统一 `from config import app_config`。
2. **四层职责不越界**：routers 不写 SQL，services 不碰 HTTP 细节，repositories 只做增删改查（`repositories/user.py` 是「唯一写 SQL 的地方」这一约定的范例）。
3. **请求级会话**：`SessionDep`（`Annotated[AsyncSession, Depends(get_session)]`）注入，禁止在路由里自建 engine。
4. **异步 + commit 后必 refresh**：`commit()` 会让 ORM 对象过期，异步下再读属性会 `MissingGreenlet`；仓库方法都 `commit()` 后 `refresh()` 返回。
5. **鉴权是两层**：`get_current_user`（Bearer → JWT → User，401/403）与 `get_super_user`（再要求 `users.is_super`）。
   **super 标志不写进 JWT**，每次请求回库现查 → 改库立刻生效、升降权都不用重新登录。
6. **`is_super` 只能改数据库**：没有 API、也没有 repository 写入口（注册请求里塞 `is_super: true` 无效）。
7. **路由约定**：一个操作一条独立路径，动作词进路径（`/workspaces/create`、`/workspaces/grant/{id}`），不用不同 HTTP 方法复用同一路径；
   参数顺序统一为「路径 → 请求体 → 当前用户 → session」；请求/响应模型放 `routers/schemas/`。
8. **多对多带权限用关联对象**：`UserWorkspace` 既是被映射的表，也是权限的载体；`User.workspaces` / `Workspace.users` 是 `viewonly=True` 的只读视图（`append` 不会写库，写入一律走 `UserWorkspaceRepository.grant`）。
9. **幂等授权用 PG upsert**：`grant` 是 `INSERT ... ON CONFLICT (user_id, workspace_id) DO UPDATE`，并发下不会撞唯一约束；`revoke` 是**删关联行**（不是降级成 viewer）。
10. **Windows 事件循环坑**：psycopg 异步驱动不兼容 `ProactorEventLoop`，在 `dependencies/database.py` 与 `main.py` 里统一换成 Selector（Linux 无此问题）。

## 4. 权限模型（最容易踩）

| 操作 | 谁能做 | 失败码 |
| --- | --- | --- |
| 建 / 改 / 删空间、加成员 / 改权限 / 移除成员 | **只有 `users.is_super = true`** | 403（未登录 401） |
| 空间详情 / 我参与的空间 / 成员列表 | 该空间成员 | 非成员 404（不泄露空间是否存在） |
| 注册 / 登录 / 刷新 | 任何人 | — |

- `user_workspaces.permission`（`admin` / `editor` / `viewer`，默认 `viewer`，库侧有默认值 + CHECK）
  **目前只描述成员身份，不参与鉴权**；空间内的 `admin` 同样不能改空间或成员。留给以后空间内的功能（跑 agent、写文件等）。
- 建空间时会在**同一个事务**里把创建者写成该空间的 `admin`，所以不会出现「没人管的空间」。
- 授权 super 的唯一途径是数据库：`update users set is_super = true where account = '...'`。

## 5. 导览路线（建议按顺序读）

| # | 站点 | 要点 |
| --- | --- | --- |
| 1 | 项目总览 | README 的目录结构与分层约定 + AGENTS.md 的目录职责，建立「配置在 yaml、业务分四层」的印象 |
| 2 | 配置：OmegaConf + pydantic | `config.yaml` 是唯一配置源；`extra="forbid"` 让写错键名启动即报错 |
| 3 | 日志：loguru 接管全部输出 | 控制台 + 滚动文件两个 sink；`InterceptHandler` 转发标准库日志（uvicorn/sqlalchemy/alembic） |
| 4 | 数据模型：Base 与 BaseModel | 统一 `pk_/ix_/uq_/fk_` 命名约定；业务表继承 `BaseModel` 自带 32 位 uuid 主键 + 带时区时间戳 |
| 5 | 数据库会话 | 导入时建异步 engine（池 10+20、pre_ping、recycle）与 `AsyncSessionFactory`，`SessionDep` 供路由使用 |
| 6 | 仓储层 | 唯一写 SQL 的地方；上层只调方法 |
| 7 | 业务逻辑：bcrypt + JWT 双令牌 | 注册查重、cost=12 哈希、登录签发双令牌并落库、刷新轮换（旧令牌立即失效）、登出清空 |
| 8 | 接口层：`/auth` 五端点 | 只收参/调 service/返回；schema 已抽到 `routers/schemas/` |
| 9 | 鉴权依赖 | `get_current_user` 解析当前用户，`get_super_user` 再要求 super（写接口靠它 403） |
| 10 | 应用入口与启动 | lifespan 管理连接池、`/health`、Windows 下显式 SelectorEventLoop |
| 11 | 数据库迁移 | 连接串由 `migrations/env.py` 从 `config.yaml` 注入，应用与迁移共用同一份配置 |
| 12 | 端到端自检 | 不需要 pytest：`tests/test_auth.py` 18 项断言 + 直连数据库核对落库 |
| 13 | 下一个装配位 | `agents/` 为空但依赖与 `postgresql.deepagent` 段已就绪 |
| 14 | 业务空间与成员权限 | 模型 → 仓储（grant upsert）→ 服务（同一事务写 admin）→ 路由（SuperUser 依赖）→ schema |
| 15 | 权限的验证方式 | 三个自建自清脚本：关联表约束 / 仓储 upsert·撤销·级联 / 21 项接口检查 |

## 6. 文件地图（按图层）

**入口与装配**
- `main.py` — FastAPI 实例、lifespan（打点 + 释放连接池）、挂载 auth / workspace router、`/health`、uvicorn 启动参数
- `agents/__init__.py` — deepagents 装配占位（待接 `create_deep_agent` 与 `AsyncPostgresSaver` / `AsyncPostgresStore`）

**接口层**
- `routers/auth.py` — 注册 / 登录 / 刷新 / 登出 / me 五个端点
- `routers/workspace.py` — `/workspaces/*` 八个端点（一操作一路径）
- `routers/schemas/auth.py`、`routers/schemas/workspace.py` — 请求/响应模型（含 `MyWorkspaceOut.of()`、`MemberOut.of()` 组装工厂）
- `dependencies/database.py` — 异步 engine / `AsyncSessionFactory` / `get_session`
- `dependencies/auth.py` — `get_current_user`、`get_super_user` 与 `CurrentUser` / `SuperUser` 注入别名

**服务层**
- `services/auth.py` — bcrypt 哈希、JWT 签发解码、注册/登录/刷新/登出规则
- `services/workspace.py` — 建空间（事务内写 admin）、读操作的成员校验、空间/用户存在性校验

**数据层**
- `models/__init__.py` — `Base`（命名约定）+ `BaseModel`（id + 时间戳）；末尾导入各表模型注册 metadata
- `models/user.py` — `users`：account / email / hashed_password / refresh_token / is_active / **is_super**
- `models/workspace.py` — `workspaces`：name（唯一）/ path
- `models/user_workspace.py` — `user_workspaces`：多对多 + `WorkspacePermission`（默认 viewer + CHECK + 唯一约束）
- `repositories/user.py`、`repositories/workspace.py`、`repositories/user_workspace.py` — 三个仓储（成员仓储的 `grant` 是 PG upsert）
- `migrations/env.py` + `versions/*.py` — 4 条迁移：建 users → 建 workspaces/user_workspaces → permission 默认值与 CHECK → users.is_super
- `alembic.ini` — 只配 `script_location` 与日志，URL 由 env.py 注入

**基础设施**
- `config/config.yaml` — 唯一配置源（postgresql / logger / auth 三段，另有 `postgresql.user` 业务库）
- `config/config.py` — 加载 → 环境变量覆盖 → pydantic 校验 → `app_config`
- `logger/__init__.py` — loguru sink + 标准库日志接管

**测试**
- `tests/test_auth.py` — 18 项鉴权端到端（TestClient + 直连库核对）
- `tests/test_workspace_api.py` — 21 项空间接口（super 规则、错误码、级联删除）
- `tests/test_user_workspace_repository.py` — 成员仓储（upsert / 撤销 / 级联）
- `tests/test_user_workspace.py` — 关联表约束（内存 SQLite，无需 PG）

## 7. 复杂度热点（改动前请谨慎）

图谱里标为 `moderate` 的文件：

| 文件 | 为什么容易踩 |
| --- | --- |
| `models/user_workspace.py` | 三件事缠在一起：枚举入库小写（`values_callable`）、库侧默认值 + CHECK、`viewonly` 双向关系 |
| `repositories/user_workspace.py` | PG 专用 upsert（`on_conflict_do_update`），且要手动维护 `updated_at`（Core 语句不走 ORM `onupdate`） |
| `services/workspace.py` | 建空间必须**一个事务**写完空间与 admin 关联；`commit` 后必须 `refresh` 否则异步读属性炸 |
| `routers/workspace.py` | 路由约定集中地：一操作一路径、参数顺序、写接口 `SuperUser` |
| `dependencies/database.py` | 连接池与事件循环策略（Windows 坑） |
| `dependencies/auth.py` | 两层鉴权链，401/403 的分界（未登录 vs 被禁用 vs 非 super） |
| `migrations/env.py` | 连接串来自 `config.yaml` 而非 `alembic.ini`，改错地方会"迁移跑错库" |
| `main.py` | 事件循环 + lifespan 资源释放 |
| `config/config.py` | `extra="forbid"` + 环境变量只能覆盖已存在的键 |

## 8. 常见错误对照

| 现象 | 原因 / 处理 |
| --- | --- |
| 建/改空间返回 403 | 你不是 super：`update users set is_super = true where account = '...'`（改完立即生效） |
| 空间详情 404 | 你不是该空间成员（读操作按成员关系判定，super 也不会自动获得读权限） |
| `grant` 返回 404 | 入参 `user_name` 是**账号**（注册时的 account），不是用户 id、也不是邮箱；写错了就 404 |
| `Psycopg cannot use the 'ProactorEventLoop'` | 用 `uv run python main.py`，或加 `--reload` / `--workers N` / `--loop asyncio:SelectorEventLoop` |
| `MissingGreenlet` | 在异步上下文里读了 `commit()` 之后未 `refresh` 的过期属性 |
| `alembic check` 有输出 | 模型与库不一致：漏了 `revision --autogenerate` 或忘了 `upgrade head` |
| 启动报 `ValidationError` | `config.yaml` 里写了模型没定义的键（`extra="forbid"`），或环境变量覆盖了 yaml 中不存在的键 |
| `/auth/me` 返回 403 | 令牌有效但用户被禁用（`is_active = false`）；401 才是令牌无效 |

---

### 维护方式

```bash
/understand            # 重新分析（图谱更新后本指南建议重生成）
/understand-dashboard  # 打开可视化看板
```
