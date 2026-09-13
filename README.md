# deepagents_template

FastAPI + PostgreSQL + loguru 的后端模板：配置文件（OmegaConf + pydantic）、loguru 日志、
用户注册登录与 JWT 鉴权、alembic 数据库迁移，以及用 PostgreSQL 做持久化记忆的 deepagents 智能体。

目录：[1 环境要求](#1-环境要求) · [2 项目结构](#2-项目结构) · [3 首次运行](#3-首次运行) ·
[4 运行项目](#4-运行项目) · [5 数据库迁移与新增表](#5-数据库迁移与新增表) ·
[6 生产环境部署](#6-生产环境部署) · [7 接口](#7-接口) · [8 配置项](#8-配置项) ·
[9 测试](#9-测试) · [10 常见问题](#10-常见问题) · [11 deepagents 智能体](#11-deepagents-智能体)

---

## 1. 环境要求

| 项 | 要求 |
| --- | --- |
| Python | **3.14**（`pyproject.toml` 锁 `>=3.14,<3.15`，`.venv` 由 uv 管理） |
| 依赖管理 | [uv](https://docs.astral.sh/uv/)（`uv.lock` 已提交，安装请用 `uv sync`） |
| 数据库 | PostgreSQL（默认连 `127.0.0.1:5432`，库名 `deepagent`） |
| 操作系统 | 开发机 Windows/Linux 均可；生产建议 Linux（见 [6](#6-生产环境部署)） |

```bash
uv sync --frozen      # 按 uv.lock 精确安装依赖（新增依赖用 uv add）
```

---

## 2. 项目结构

```
.
├── config/                  # 配置：OmegaConf 读 yaml → pydantic 校验 → 单例 app_config
│   ├── config.yaml          #   postgresql / logger / auth 三段，改配置先看这里
│   ├── config.py            #   AppConfig 等模型 + load_config() + _apply_env_overrides()
│   └── __init__.py          #   对外只暴露 app_config、load_config 与各段模型
├── logger/__init__.py       # loguru 控制台 + 滚动文件 sink；接管标准库 logging
├── dependencies/
│   ├── database.py          #   异步 engine / AsyncSessionFactory / get_session（请求级会话）
│   ├── auth.py              #   get_current_user（Bearer → JWT → User），CurrentUser 注入类型
│   └── agent.py             #   AgentDep：拿应用级 DeepAgent 实例
├── models/                  # SQLAlchemy ORM
│   ├── __init__.py          #   Base（命名约定）/ BaseModel（id+时间戳）；末尾 import 各表模型
│   └── user.py              #   users 表：account / email / hashed_password / refresh_token
├── repositories/            # 数据库读写类（UserRepository）
├── services/                # 路由功能实现（AuthService：bcrypt + JWT；AgentService：智能体生命周期）
├── routers/                 # FastAPI 路由（auth.py → /auth/*，agent.py → /agent/*）
├── agents/agent.py          # DeepAgent：deepagents + PostgreSQL state/store 的异步外壳
├── main.py                  # 应用入口：app / lifespan / /health / 事件循环与 uvicorn 启动参数
├── migrations/              # alembic 迁移（连接串来自 config.yaml 的 postgresql.user 段）
│   ├── env.py               #   排除 langgraph 自建的 checkpoints/store 表
│   └── versions/*.py        #   已包含 ce9e484f27fb 建 users 表
├── tests/test_auth.py       # 鉴权端到端自检（18 项，无需 pytest）
├── tests/test_agent.py      # 智能体端到端自检（18 项，真实调用模型，需 DEEPSEEK_API_KEY）
├── alembic.ini              # 只配 script_location / 日志，URL 由 env.py 注入
└── uv.lock                  # 依赖锁文件
```

分层约定：**routers 只收参/返回 → services 写业务逻辑 → repositories 只做数据库读写 → models 定义表**。
配置一律 `from config import app_config`，日志一律 `from logger import logger`。

---

## 3. 首次运行

```bash
# 1) 装依赖（已有 .venv 可跳过）
uv sync --frozen

# 2) 改 config/config.yaml 里 postgresql.user / postgresql.deepagent 的连接信息
#    （本机就是 PostgreSQL 的话，通常只改 user/password/db_name）
#    并把 deepagent.api_key_env 指向存放模型 key 的环境变量（默认 DEEPSEEK_API_KEY）

# 3) 建业务库的表（users + alembic_version）
uv run alembic upgrade head

# 4) 启动（启动时智能体会自动建 checkpoints / checkpoint_blobs / checkpoint_writes / store 四张表）
uv run python main.py         # http://127.0.0.1:8000 ，Swagger 在 /docs

# 5) 自检（注册/登录/me/刷新/登出/禁用用户，共 18 项，跑完自动清理测试数据）
uv run python tests/test_auth.py

# 6) 智能体自检（18 项，会真实调用模型：短时记忆跨实例续聊、长期记忆跨会话读回、SSE 流式）
uv run python tests/test_agent.py
```

---

## 4. 运行项目

| 场景 | 命令 | 说明 |
| --- | --- | --- |
| 开发（推荐） | `uv run python main.py` | reload 热更新；`log_config=None`，日志全部走 loguru；监听 `127.0.0.1:8000`（改 `main.py` 里 `uvicorn.run` 的 host/port） |
| 生产（Linux） | `uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips=<反向代理 IP>` | 多进程；用 `uv run` 保证走项目 `.venv`；`--forwarded-allow-ips` 填 nginx 地址，别用 `*` |
| Windows 单进程 | `uv run uvicorn main:app --loop asyncio:SelectorEventLoop` | 见下方说明 |
| Windows 多进程 / reload | `uv run uvicorn main:app --workers 2` 或 `--reload` | 子进程模式，uvicorn 自己选 SelectorEventLoop，**不用**加 `--loop` |
| 只跑迁移 | `uv run alembic upgrade head` | 见第 5 节 |

> **Windows 为什么需要 `--loop`**：psycopg 异步驱动不支持 Windows 默认的 `ProactorEventLoop`，
> 而 `uvicorn` 在「单进程且非 reload」时恰好选 Proactor，于是任何走数据库的请求都会
> `sqlalchemy.exc.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`。
> 三种规避方式：① `python main.py`（参数已固化在代码里，零参数）；② 加 `--reload` 或 `--workers N`；
> ③ 显式 `--loop asyncio:SelectorEventLoop`。**Linux 上不存在这个问题，无需任何额外参数。**

健康检查：`GET /health`（返回 `{"status":"ok"}`），可直接作为容器/负载均衡探针。

---

## 5. 数据库迁移与新增表

迁移工具是 alembic，连接串由 `migrations/env.py` 从 `config.yaml` 的 **`postgresql.user`** 段注入，
所以**不用**改 `alembic.ini` 里的 `sqlalchemy.url`；迁移只作用于业务用户库，`postgresql.deepagent` 段不受影响。

### 5.1 新增一张表

```bash
# 1) 写模型：models/article.py
#    from models import BaseModel
#    class Article(BaseModel):            # 自带 id / created_at / updated_at
#        __tablename__ = "articles"
#        title: Mapped[str] = mapped_column(String(200), index=True)

# 2) 在 models/__init__.py 末尾补一行（漏了 autogenerate 会“看不见”这张表）
#    from .article import Article

# 3) 自动生成迁移脚本
uv run alembic revision --autogenerate -m "create articles table"

# 4) 打开 migrations/versions/xxxx_create_articles_table.py 人工复核：
#    - 是否只包含你期望的变更（列、索引、server_default、注释）
#    - downgrade() 是否真的能回滚（列重命名会被当成 drop+add，必须手工改脚本）

# 5) 应用迁移
uv run alembic upgrade head

# 6) 校验模型与库是否一致（应输出 No new upgrade operations detected）
uv run alembic check

# 7) 推荐再验证一次回滚：downgrade -1 → upgrade head
uv run alembic downgrade -1 && uv run alembic upgrade head
```

改字段、加索引同理：改模型 → `revision --autogenerate` → 复核 → `upgrade head` → `check`。

### 5.2 常用命令

| 命令 | 作用 |
| --- | --- |
| `uv run alembic upgrade head` | 升到最新（部署时执行它） |
| `uv run alembic downgrade -1` / `downgrade base` | 回退一个版本 / 全部回退 |
| `uv run alembic current` | 当前库所在版本 |
| `uv run alembic history --verbose` | 迁移历史 |
| `uv run alembic check` | 比对模型与库，检查漏生成的迁移 |
| `uv run alembic revision --autogenerate -m "描述"` | 依据当前模型生成迁移脚本 |
| `uv run alembic stamp head` | 只改版本号、不执行 DDL（特殊情况用） |

注意事项：

- 迁移脚本要一起提交进 git；**不要在应用启动时自动建表**（多副本会并发迁移）。
- 需要手写 SQL 时用 `op.execute("...")`，并保留可用的 `downgrade()`。
- 修改已有迁移文件只在「尚未上生产」时允许，已发布的历史迁移不要再改。

---

## 6. 生产环境部署

### 6.1 必须改的配置

配置优先级：**环境变量 > `config/config.yaml`**。生产建议密码/密钥只走环境变量，
但环境变量只能覆盖 yaml 中**已存在**的键（键名必须保留在 yaml 里），命名规则：`APP_` + 大写路径 + `__` 分隔层级。

| 配置项（yaml 路径） | 生产要求 | 环境变量 |
| --- | --- | --- |
| `postgresql.user.host/port/user/password/db_name` | 指向生产业务库，**密码不要写进提交的 yaml** | `APP_POSTGRESQL__USER__HOST` / `__PORT` / `__USER` / `__PASSWORD` / `__DB_NAME` |
| `postgresql.deepagent.*` | 指向 deepagents 的检查点/长期记忆库 | `APP_POSTGRESQL__DEEPAGENT__HOST` / `__PORT` / `__USER` / `__PASSWORD` / `__DB_NAME` |
| `deepagent.model` / `base_url` | 模型名与 OpenAI 兼容端点（当前默认 DeepSeek） | `APP_DEEPAGENT__MODEL` / `APP_DEEPAGENT__BASE_URL` |
| `deepagent.api_key_env` | **不要**把 key 写进 yaml：这里只写「到哪个环境变量去取」 | `APP_DEEPAGENT__API_KEY_ENV` |
| `deepagent.temperature` | 0–1，越大越发散 | `APP_DEEPAGENT__TEMPERATURE` |
| `auth.secret_key` | **必须替换**为 ≥32 字节随机串，泄露等于任何人都能签 token | `APP_AUTH__SECRET_KEY` |
| `auth.access_token_expire_minutes` | 15–60 分钟（越短越安全，越大越省刷新） | `APP_AUTH__ACCESS_TOKEN_EXPIRE_MINUTES` |
| `auth.refresh_token_expire_days` | 7–30 天 | `APP_AUTH__REFRESH_TOKEN_EXPIRE_DAYS` |
| `logger.level` | 生产用 `INFO` 或 `WARNING` | `APP_LOGGER__LEVEL` |
| `logger.dir` | 绝对路径，例如 `/var/log/deepagents` | `APP_LOGGER__DIR` |
| `logger.rotation` / `retention` | 按磁盘策略，如 `100 MB` / `30 days` | `APP_LOGGER__ROTATION` / `APP_LOGGER__RETENTION` |
| `logger.diagnose` | 保持 `false`（打印异常时附带变量值，可能泄漏密码/令牌） | `APP_LOGGER__DIAGNOSE` |

生成密钥：

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

生产环境变量示例（systemd `EnvironmentFile` / Docker env / k8s Secret 均可）：

```bash
APP_POSTGRESQL__USER__HOST=10.0.0.12
APP_POSTGRESQL__USER__PASSWORD=<生产密码>
APP_AUTH__SECRET_KEY=<上面生成的随机串>
APP_LOGGER__DIR=/var/log/deepagents
APP_LOGGER__LEVEL=INFO
```

> 本项目**不读取 `.env` 文件**（`.env` 仅被 gitignore）。环境变量请通过进程环境注入；
> 也可以在 yaml 里用 OmegaConf 插值：`password: ${oc.env:PG_PASSWORD,1234}`。

### 6.2 启动与进程管理

```bash
# 发布阶段（只执行一次，单实例！多副本并发迁移会互相锁表）
uv sync --frozen
uv run alembic upgrade head

# 启动多进程服务
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 \
    --proxy-headers --forwarded-allow-ips=127.0.0.1   # 填反向代理的 IP，不要用 *
```

systemd 示例（`/etc/systemd/system/deepagents.service`）：

```ini
[Unit]
Description=deepagents api
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/opt/deepagents
EnvironmentFile=/etc/deepagents.env
ExecStart=/opt/deepagents/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 \
          --workers 4 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

反向代理（nginx 等）监听 443 并转发到 127.0.0.1:8000，同时把
`X-Forwarded-For` / `X-Forwarded-Proto` 传给应用（配合 `--proxy-headers`）。
容器环境把 `uvicorn` 作为 `CMD`，并把上面的环境变量作为 Secret 注入，探针指向 `/health`。

### 6.3 两个容易踩的生产坑

1. **数据库连接数**：`dependencies/database.py` 里 `pool_size=10`、`max_overflow=20`，
   即**每个 worker 最多 30 条连接**。`--workers 4` 就是最多 120 条，
   而 PostgreSQL 默认 `max_connections=100` → 容易连接被打满。
   规则：`workers × (pool_size + max_overflow) ≤ max_connections 的 80%`，
   要么调小 `pool_size`/`max_overflow`，要么调大 `max_connections`，要么上 PgBouncer。
2. **多副本写日志**：loguru 的文件 sink 由同机多进程共写会有交错行。
   多副本部署时建议 `logger.dir` 每实例独立，或只保留控制台日志交给 journald / Docker logs 收集。

---

## 7. 接口

| 方法 | 路径 | 说明 | 需要鉴权 |
| --- | --- | --- | --- |
| POST | `/auth/register` | 注册（账号 3–50 位、邮箱、密码 ≥8 位） | 否 |
| POST | `/auth/login` | 登录，返回 access + refresh 双令牌（账号或邮箱都可） | 否 |
| POST | `/auth/refresh` | 用 refresh token 换新令牌（轮换，旧 refresh 立即失效） | 否 |
| POST | `/auth/logout` | 登出，清空库中 refresh token | Bearer |
| GET | `/auth/me` | 当前登录用户 | Bearer |
| GET | `/health` | 健康检查 | 否 |

```bash
BASE=http://127.0.0.1:8000
curl -s -X POST $BASE/auth/register -H 'Content-Type: application/json' \
     -d '{"account":"admin","email":"admin@example.com","password":"Passw0rd!123"}'
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
        -d '{"account":"admin","password":"Passw0rd!123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s $BASE/auth/me -H "Authorization: Bearer $TOKEN"
```

启动后 Swagger 文档在 `/docs`，OpenAPI JSON 在 `/openapi.json`。

---

## 8. 配置项

`config/config.yaml` 四段（字段与 `config/config.py` 的 pydantic 模型一一对应，`extra="forbid"`：
yaml 里写错键名会**直接报错**，不会被静默忽略）：

| 段 | 用途 |
| --- | --- |
| `postgresql.user` | 业务用户库：应用异步引擎、alembic 迁移都用它 |
| `postgresql.deepagent` | deepagents 的 state（检查点）/ store（长期记忆）库；代码里用 `app_config.postgresql.deepagent.uri` |
| `logger` | loguru：`level` / `dir`（相对路径按项目根解析）/ `rotation` / `retention` / `backtrace` / `diagnose`，文件写到 `<dir>/app_YYYY-MM-DD.log` |
| `auth` | JWT：`secret_key`（≥32 字节）/ `algorithm` / `access_token_expire_minutes` / `refresh_token_expire_days` |

`PostgreConfig` 暴露两个连接串：`.uri`（`postgresql://…`，给 psycopg/连接池/checkpointer 用）与
`.sqlalchemy_uri`（`postgresql+psycopg://…`，同步/异步引擎通用）。

其他模块读取方式：`from config import app_config, load_config`（`load_config()` 可重新加载）。

---

## 9. 测试

```bash
uv run python tests/test_auth.py     # 需要 PostgreSQL 可用且已 alembic upgrade head
```

覆盖：注册 201 / 重复注册 409 / 非法输入 422 / 密码错误 401 / 登录返回双令牌 /
库中存 bcrypt 哈希而非明文 / `me` 200 / 无令牌·乱码令牌·拿 refresh 当 access 401 /
刷新轮换且旧 refresh 失效 401 / 登出 204 且库中 refresh_token 置空 / 禁用用户 403。
脚本用 `tester_*` 前缀账号并在结束时清理，不影响其他数据。

---

## 10. 常见问题

**Q：Windows 上 `uvicorn main:app` 报 `Psycopg cannot use the 'ProactorEventLoop'`？**
见 [第 4 节](#4-运行项目)：用 `python main.py`、加 `--reload`/`--workers N`，或加 `--loop asyncio:SelectorEventLoop`。
`alembic` 用的是同步引擎，不受影响。

**Q：登出后 access token 还能用？**
是有意为之：access token 无状态，登出只清库里的 refresh token，access token 到期（默认 30 分钟）自然失效。
需要「立即失效」就得引入黑名单/版本号（本项目未实现）。

**Q：设置密码报 422？**
bcrypt 只处理前 72 字节，本项目对超长密码直接拒绝（不静默截断），另要求 ≥8 位。
非 ASCII 密码按 UTF-8 字节数计算，例如 25 个汉字就会超限。

**Q：改了 yaml 里的键名，启动直接报 ValidationError？**
`extra="forbid"` 的保护：顶层与各段都会拒绝未知键。同时环境变量只能覆盖 yaml 中已存在的键，
新增配置项要先在 `config/config.py` 的模型和 `config.yaml` 里同时加。

**Q：`/auth/me` 返回 403 而不是 401？**
401 = 没有/无效令牌；403 = 令牌有效但用户被禁用（`users.is_active = false`）。

**Q：怎么切到另一个数据库？**
只改 `config/config.yaml`（或对应环境变量），应用与 alembic 都用同一份配置，无需改代码。

---

## 11. deepagents 智能体

`agents/agent.py` 里的 `DeepAgent` 把 deepagents 包成一个异步类，用 PostgreSQL 做两类持久化：

| 记忆类型 | 载体 | 生命周期 | 对应表 |
| --- | --- | --- | --- |
| 短时记忆（state） | `AsyncPostgresSaver` 检查点 | 单个 `thread_id` 内，**跨进程/重启**保留 | `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` |
| 长期记忆（store） | `AsyncPostgresStore` + `StoreBackend` | 跨会话、跨进程；`/memories/` 下的文件走它 | `store` |

- 除 `/memories/` 外的路径仍由 `StateBackend` 管理（线程内临时文件），`ThreadState.files` 随检查点一起落库；
- **用户隔离**：`thread_id` 落库时会加用户前缀（`{user_id}:{thread_id}`），`/memories/` 用 `{user_id}` 作为 store 命名空间，
  所以拿到别人的 thread_id 也读不到内容，两个用户的文件互不可见；
- 上层用 `AgentService`（`services/agent.py`）持有全局实例，`main.py` 的 lifespan 里 `start()` / `stop()`，
  启动即建表（`setup()` 幂等）并预热连接池，缺 API key 或库不可用会在启动时就报错（fail fast）。

```python
from agents import DeepAgent

# async with 写法
async with DeepAgent() as agent:
    answer = await agent.ainvoke("记住：我的代号是夜枭", thread_id="chat-1", user_id="u1")
    async for event in agent.astream("我的代号是什么？", thread_id="chat-1", user_id="u1"):
        if event.kind == "token":
            print(event.text, end="")      # 逐 token
        elif event.kind == "done":
            print("\n最终:", event.text)     # 完整回答

# 显式 aenter / aexit（等价，适合在 lifespan 里手动管理）
agent = DeepAgent()
await agent.aenter()
await agent.ainvoke("你好", thread_id="chat-1", user_id="u1")
await agent.aexit()
```

方法一览：

| 方法 | 说明 |
| --- | --- |
| `ainvoke(message, *, thread_id, user_id)` | 跑一轮，返回最终回答（state 自动落检查点） |
| `astream(message, *, thread_id, user_id)` | 异步迭代 `AgentEvent`：`token` → 增量文本，`tool_call` → 工具名，`done` → 完整回答 |
| `aget_state(thread_id, user_id)` | 读短时记忆：消息条数、最后一条回答、会话内临时文件 |
| `alist_memories(user_id)` | 读长期记忆：`/memories/` 下的文件路径 |
| `aenter()` / `aexit()` | 建立/释放 checkpointer + store 连接池并编译图（`async with` 亦可） |
| `graph` / `store` | 编译好的 LangGraph 图 / store 实例（未启动时访问会抛 `RuntimeError`） |

HTTP 接口（都要 `Authorization: Bearer <access_token>`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/agent/chat` | body `{"message": "...", "thread_id": "chat"}` → `{"thread_id", "answer"}` |
| POST | `/agent/stream` | 同一个 body，SSE 流：`event: token` / `event: done` / `event: end`，出错推 `event: error` |
| GET | `/agent/state/{thread_id}` | 短时记忆：`messages` / `answer` / `files` |
| GET | `/agent/memories` | 长期记忆文件列表 |

模型走 OpenAI 兼容端点，所以只装 `langchain-openai` 一个包，换厂商只改配置：

```yaml
deepagent:
  model: deepseek-flash                                   # 想用 qwen：model: qwen-plus
  base_url: https://api.deepseek.com                      # 百炼：https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: DEEPSEEK_API_KEY                           # 只写环境变量名，不写 key 本身
  temperature: 0.0
```

> 智能体相关表由 langgraph 的 `setup()` 自己创建/升级（带 *_migrations 版本表），
> 不归 alembic 管：`migrations/env.py` 里的 `include_object` 已把它们排除，
> 否则 `alembic check` 会认为这些表是「多余的表」并想删掉。
