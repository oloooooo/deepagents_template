# deepagents_template

FastAPI + PostgreSQL + loguru 的后端模板：配置文件（OmegaConf + pydantic）、loguru 日志、
用户注册登录与 JWT 鉴权、业务空间与成员权限（super 才能写）、alembic 数据库迁移。

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
│   ├── auth.py              #   get_current_user（Bearer → JWT → User）、CurrentUser / SuperUser 注入类型
│   ├── agent.py             #   AgentDep：从 app.state 取 lifespan 起好的 GeneralAgent（未就绪 503）
│   └── public_workspace.py  #   PublicWorkspaceDep：当轮可见的公共空间（名字→id），每轮查一次
├── models/                  # SQLAlchemy ORM
│   ├── __init__.py          #   Base（命名约定）/ BaseModel（id+时间戳）；末尾 import 各表模型
│   ├── user.py              #   users 表：account / email / hashed_password / refresh_token / is_super
│   ├── workspace.py         #   workspaces 表：name（唯一）/ path
│   ├── user_workspace.py    #   user_workspaces 关联表：多对多 + permission（默认 viewer）
│   ├── public_workspace.py  #   public_workspaces 表：name（唯一且不可变）/ description，没有 path
│   └── user_public_workspace.py # user_public_workspaces 关联表：多对多，**没有** permission 列
├── repositories/            # 数据库读写类（User / Workspace / UserWorkspace / PublicWorkspace / UserPublicWorkspace）
├── services/                # 路由功能实现（Auth / Workspace / PublicWorkspace / Memory / Chat）
│   ├── access.py            #   业务空间权限校验的唯一入口（非成员 404、viewer 写 403）
│   └── public_workspace.py  #   公共空间：super 全通 + 成员看关联记录；删空间时顺带清 store
├── routers/                 # FastAPI 路由（auth.py、workspace.py、public_workspace.py、memory.py、chat.py）
│   └── schemas/             #   请求/响应模型：auth.py、workspace.py、public_workspace.py、memory.py、chat.py、paths.py
├── main.py                  # 应用入口：`create_app()`（每实例一份 state，测试靠它模拟多 worker）/ lifespan / /health / uvicorn 参数
├── migrations/              # alembic 迁移（连接串来自 config.yaml 的 postgresql.user 段）
│   ├── env.py
│   └── versions/*.py        #   users、workspaces、user_workspaces、is_super、public_workspaces 等 5 个迁移
├── tests/                   # 端到端自检脚本（13 个，均无需 pytest，跑完自清理）
├── CONTEXT.md               # 领域术语表：Workspace / Public workspace / Visibility / Cell / Fan-out / Turn owner…
├── docs/adr/                # 架构决定记录（0001~0010：公共空间为什么不复用 Workspace、记忆为什么只写 default、停止为什么靠 LISTEN/NOTIFY…）
├── alembic.ini              # 只配 script_location / 日志，URL 由 env.py 注入
└── agents/                  # agent 本体
    ├── agent.py             #   GeneralAgent / AgentMemory（跑图 + 存取记忆）
    ├── fanout.py            #   多命名空间挂载：/{挂载根}/{格子名}/...（/memories/ 与 /public/ 共用）
    ├── turns.py             #   停止：TurnRegistry（本进程：找 task + 攒文本）+ RunningTurns（跨进程：表 + chat_drain 通道）
    ├── public_workspace.py  #   /public 挂载（只读）
    └── readme.md            #   模块约定（记忆隔离边界、两个挂载的差别、context 必传、停止的取舍）
```

分层约定：**routers 只收参/返回 → services 写业务逻辑 → repositories 只做数据库读写 → models 定义表**；
请求/响应模型放 `routers/schemas/`。配置一律 `from config import app_config`，日志一律 `from logger import logger`。

---

## 3. 首次运行

```bash
# 1) 装依赖（已有 .venv 可跳过）
uv sync --frozen

# 2) 改 config/config.yaml 里 postgresql.user / postgresql.deepagent 的连接信息
#    （本机就是 PostgreSQL 的话，通常只改 user/password/db_name）
#    并把 deepagent.api_key_env 指向存放模型 key 的环境变量（默认 DEEPSEEK_API_KEY）

# 3) 建业务库的表（users / workspaces / user_workspaces / public_workspaces / user_public_workspaces + alembic_version）
uv run alembic upgrade head

# 4) 启动（启动时智能体会自动建 checkpoints / checkpoint_blobs / checkpoint_writes / store 四张表）
uv run python main.py         # http://127.0.0.1:8000 ，Swagger 在 /docs

# 5) 自检（注册/登录/me/刷新/登出/禁用用户，共 18 项，跑完自动清理测试数据）
uv run python tests/test_auth.py

# 6) 把自己设成 super，否则建不了空间（super 只能在数据库里改，见 7.5）
psql -d <库名> -c "update users set is_super = true where account = 'admin'"
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

配置优先级：**系统环境变量 > 项目根目录 `.env` > `config/config.yaml` 里的默认值**。

yaml 里每个能从外部改的键都写成 `${oc.env:变量名,默认值}`，所以「哪些配置可以外部注入」在 yaml 里
一眼看得见；要加一个就照抄一行，**不用改 `config.py`**。本地把真值写进 `.env`（已 gitignore），
容器 / systemd / k8s 直接注同名环境变量即可（同名时环境变量优先）。

| 配置项（yaml 路径） | 生产要求 | 环境变量 |
| --- | --- | --- |
| `postgresql.user.host/port/user/password/db_name` | 指向生产业务库，**密码不要写进提交的 yaml** | `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_DB` |
| `auth.secret_key` | **必须替换**为 ≥32 字节随机串，泄露等于任何人都能签 token | `AUTH_SECRET_KEY` |
| `logger.level` | 生产用 `INFO` 或 `WARNING` | `LOG_LEVEL` |
| `logger.dir` | 绝对路径，例如 `/var/log/deepagents` | `LOG_DIR` |

其余键（token 有效期、日志切分策略、`logger.diagnose`、`auth.algorithm`）属于**策略而不是环境**，
直接写死在 yaml 里；确实要按环境改，就在 yaml 那行套一层 `${oc.env:名字,默认值}`。

生成密钥：

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

生产注入示例（systemd `EnvironmentFile` / Docker env / k8s Secret 均可）：

```bash
PG_HOST=10.0.0.12
PG_PASSWORD=<生产密码>
AUTH_SECRET_KEY=<上面生成的随机串>
LOG_DIR=/var/log/deepagents
LOG_LEVEL=INFO
```

> `.env` 在**项目根目录**（和 `config/` 同级），由 `config/config.py` 在 import 时用 `python-dotenv` 读入。
> `load_dotenv` 默认不覆盖已存在的变量，所以「本地用 `.env`、线上注环境变量」不会打架。
> 变量名要改就**两边一起改**（yaml 里的 `${oc.env:...}` 与 `.env` / 环境变量）。
> 模型密钥走的是同一个 `.env`，但由 `agents/config.py` 的 `ModelConfig` 直接读（`OPEN_MODEL` / `OPEN_BASE_URL` / `OPEN_API_KEY`）。

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
   另外停止功能每个 worker 还要**多占 2 条长连接**（一条 `chat_drain` 的 `LISTEN`、
   一条控制查询，都不进连接池 —— `LISTEN` 是连接级状态，借来的连接会污染）。
   算容量时把它加上：`workers × (pool_size + max_overflow + 2)`。
2. **多副本写日志**：loguru 的文件 sink 由同机多进程共写会有交错行。
   多副本部署时建议 `logger.dir` 每实例独立，或只保留控制台日志交给 journald / Docker logs 收集。

---

## 7. 接口

约定：**一个操作一条独立路径**，动作词写进路径（如 `/workspaces/create`、`/workspaces/grant/{id}`），
不用不同 HTTP 方法复用同一条路径；请求/响应模型在 `routers/schemas/`，路由参数顺序统一为「路径 → 请求体 → 当前用户 → session」。

### 7.1 认证

| 方法 | 路径 | 说明 | 需要鉴权 |
| --- | --- | --- | --- |
| POST | `/auth/register` | 注册（账号 3–50 位、邮箱、密码 ≥8 位） | 否 |
| POST | `/auth/login` | 登录，返回 access + refresh 双令牌（账号或邮箱都可） | 否 |
| POST | `/auth/refresh` | 用 refresh token 换新令牌（轮换，旧 refresh 立即失效） | 否 |
| POST | `/auth/logout` | 登出，清空库中 refresh token | Bearer |
| GET | `/auth/me` | 当前登录用户（含只读字段 `is_super`） | Bearer |
| GET | `/health` | 健康检查 | 否 |

### 7.2 业务空间

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/workspaces/create` | 建空间（name 3–100 位、path）；创建者自动成为该空间 admin，重名 409，`default` 是保留名也 409 | **super** |
| GET | `/workspaces/mine` | 我参与的空间列表，每项带 `permission`；**第一条永远是虚拟的 `default`**（`path` / 时间戳为 `null`，`permission` 恒为 `admin`） | 成员 |
| GET | `/workspaces/detail/{workspace_id}` | 空间详情（`default` 没有记录可给 -> 404） | 成员 |
| GET | `/workspaces/access/{workspace_id}` | **自查**：我有没有这个空间的权限。返回 `{workspace_id, has_access, permission}`，没权限与空间不存在都是 200 + `false`；`default` 恒为 `true` + `admin` | 登录即可 |
| PATCH | `/workspaces/update/{workspace_id}` | 改 name / path（只改传了的字段） | **super** |
| DELETE | `/workspaces/delete/{workspace_id}` | 删空间（成员关联级联清理，204） | **super** |
| GET | `/workspaces/members/{workspace_id}` | 成员列表（account / email / 权限） | **super** |
| POST | `/workspaces/grant/{workspace_id}` | 加成员或改权限（按**账号名**），body `{"user_name":"…","permission":"admin / editor / viewer"}`；重复授权即更新，**成功返回 `{"result": true}`** | **super** |
| DELETE | `/workspaces/revoke/{workspace_id}/{user_name}` | 按账号名移除成员（204） | **super** |

**虚拟的 `default` 空间**（`models.DEFAULT_WORKSPACE`）：每个登录用户都自带一个 id/name 为
`default` 的空间，用来放「不属于任何业务空间」的日常聊天与记忆，**库里没有这条记录**，因此：

- `/workspaces/mine` 把它排在第一条，`/workspaces/access/default` 恒为 `has_access: true` + `admin`；
- 它没有详情与成员（`/workspaces/detail/default`、`/workspaces/members/default` 都是 404），也删不掉；
- `default` 是保留名：建/改空间用它一律 409（否则库里那条记录会被虚拟空间永远遮蔽）；
- 权限规则只有一处实现：`services/access.py` 里直接返回 `ADMIN`，不查库。

所以 `/chat/*` 与 `/memories/*` 的 `workspace_id` 都可以不传（不传就是 `default`），
前端拿 `/workspaces/mine` 的第一条当日常聊天入口即可。

### 7.3 聊天（Agent）

```
POST /chat/send                 跑一轮（阻塞）
POST /chat/stream               跑一轮（SSE 流式）
POST /chat/approve              人工批准 / 拒绝后接着跑
POST /chat/stop                 停止这一轮（用户按暂停）
GET  /chat/mine                 我的会话列表
GET  /chat/state/{thread_id}    会话的短期记忆概况
GET  /chat/history/{thread_id}  会话的消息列表（带 message id）
POST /chat/messages/delete      删几条消息（短期记忆编辑，204）
POST /chat/files/delete         删会话内临时文件（短期记忆编辑，204）
DELETE /chat/delete/{thread_id} 删整条会话（只删短期记忆，不动 /memories/，204）
```

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/chat/send` | body `{"workspace_id"?, "message", "thread_id"?}`（`workspace_id` 不传就是虚拟的 `default`）；不传 `thread_id` 就新开会话并返回。返回 `{thread_id, answer, interrupt}`——`interrupt` 非空表示在等人批准 | 成员（viewer 也能聊；`default` 人人 admin） |
| POST | `/chat/stream` | 同样的 body，返回 SSE：`event: token / tool_call / interrupt / done` + 一行 JSON（统一 `{"text":…, "data":{…}}`）。新建的 thread_id 走响应头 `X-Thread-Id` | 成员 |
| POST | `/chat/approve` | body `{"thread_id", "decisions": [{"type": "approve"}]}`，`decisions` 原样透传给 langgraph（approve / edit / reject / respond）。**不接受 `workspace_id`**：空间取自会话绑定值 | 本人会话 |
| POST | `/chat/stop` | body `{"thread_id"}`，返回 `{thread_id, answer}`——`answer` 是这一轮最终留在历史里的文本。**幂等**：已经跑完的会话再按一次不报错也不改历史 | 本人会话 |
| GET | `/chat/mine` | 我的会话（`thread_id` / `workspace_id` / `updated_at`）。读的是 checkpoint metadata，不另建表（旧会话没记 `workspace_id` 时按 `default` 算） | 登录 |
| GET | `/chat/state/{thread_id}` | `{thread_id, workspace_id, messages, answer, files}` | 本人会话 |
| GET | `/chat/history/{thread_id}` | 消息列表（最旧→最新，带 `id` 与 `role`） | 本人会话 |
| POST | `/chat/messages/delete` | body `{"thread_id", "message_ids": [...]}`，删单条消息后还能继续聊 | 本人会话 |
| POST | `/chat/files/delete` | body `{"thread_id", "paths": ["/tmp.txt"]}`，删会话内临时文件 | 本人会话 |
| DELETE | `/chat/delete/{thread_id}` | 删整条会话（检查点），**长期记忆不受影响** | 本人会话 |

#### 中断（interrupt）与停止（stop）是两件事

名字相近但方向相反，`CONTEXT.md` 里把词定死了：

| | 中断 | 停止 |
| --- | --- | --- |
| 谁的动作 | agent（想写 `/memories/**`，等人点头） | 用户（按了暂停键） |
| 之后 | **可以续跑同一轮**，走 `/chat/approve` | **不可续跑**，只能发新消息开新一轮 |
| 实现 | `MEMORY_PERMISSIONS` 的 `mode="interrupt"` | `POST /chat/stop` 取消在跑的那一轮 |

停止的语义（见 `docs/adr/0007`）：

- **短期记忆保留**，并且**就地收尾**：已经流出去的文本会补成一条 AI 消息，一个字都没流出来时
  用 `（用户停止了本轮）` 占位。不收尾的话下一轮会把停掉的那条消息和新消息**合并成一次请求**；
- **不回滚已发生的副作用**：工具已经写过的文件、已经落库的 `/memories/` 都留着 ——
  停止是「到此为止」，不是「撤销」；
- 被停止时 SSE 流是**直接断的**（任务被取消，没有机会再 yield），结果从 `/chat/stop` 的响应体里取；
- 一个会话同时只允许一轮在跑，重复提交返回 **409**（`await` 完 `/chat/stop` 再发新消息就不会撞上）。

`/chat/stop` 不靠负载均衡器做会话粘性 —— 它把“要停这一轮”写进 `running_turns` 表，
再用 Postgres 的 `LISTEN/NOTIFY` **广播**给所有 worker；只有跑着那一轮的那个进程会动手。
所以停止请求落到哪台都行，轮询 / 最少连接随便配（实现与取舍见 `docs/adr/0008`）。

同一张表的 PK 兼任跨进程的 409 互斥：**一个会话同时最多一轮**，多 worker 下也成立。
worker 挂在轮次中途时，那行靠心跳回收（60 秒），下一轮开轮次前会先把留在半路的检查点
收尾（“孤儿恢复”，见 `docs/adr/0008`）。

### 7.4 长期记忆（`/memories/*`，agent 那边的 `/memories/` 挂载）

```
GET  /memories/all                      我全部空间的记忆清单（含虚拟 default 与空空间）
GET  /memories/mine?workspace_id=<id>   列出我在该空间的记忆文件
POST /memories/read                     读一份记忆
POST /memories/write                    写 / 覆盖一份记忆
POST /memories/upload                   上传文本文件（multipart，一次可多份）
POST /memories/delete                   删一份记忆（204）
```

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/memories/all` | 我参与的全部空间一格一条：`{workspaces: [{name, workspace_id, memories}]}`（空空间也在，`memories` 是空列表） | 登录用户 |
| GET | `/memories/mine` | query `workspace_id`（可省，默认 `default`）；返回 `{workspace_id, memories: ["/memories/{空间名}/…"]}` | 成员（viewer 起） |
| POST | `/memories/read` | body `{"workspace_id"?, "path"}`（`path` 带斜杠所以放 body），不存在 404 | 成员 |
| POST | `/memories/write` | body `{"workspace_id"?, "path", "content"}`，整份覆盖，返回 `{"path": "/memories/{空间名}/…"}` | **editor / admin** |
| POST | `/memories/upload` | `multipart/form-data`：`workspace_id`（可省）+ 一份或多份文本文件；逐份返回 `{file, path, error}`，某一份失败不影响其它份 | **editor / admin** |
| POST | `/memories/delete` | body `{"workspace_id"?, "path"}`，不存在 404 | **editor / admin** |

记忆按 **`(user_id, 空间)`** 隔离（store 命名空间）：同一个空间里，别人也看不到你的记忆文件。
**一个用户有多格**：他参与的每个业务空间各一格 + 虚拟 `default` 一格，**格子清单与 agent 看到的完全同源**
（`WorkspaceService.visible`：成员关系 + default），所以你在接口里列得出来的，agent 也看得见，反之一样。

**路径两个方向的对齐**：REST 按 `workspace_id` 寻址，但**响应里回的是 agent 眼里的路径**
（`/memories/{空间名}/notes/a.md`）—— 用户把路径原样丢给 agent 就能读到。请求里的 `path` 三种写法都收：
`notes/a.md`、`/memories/notes/a.md`、`/memories/{该空间名}/notes/a.md`。

**agent 侧只有 `default` 那一格可写**（写完要人工批准，`POST /chat/approve`）；其它格子是**只读资料**，
只能用上面的 `write` / `upload` 投喂（取舍见 `docs/adr/0010`）。用户侧的写入一律直写、不用批准。
这两条链路的细节（含权限规则的顺序与“裸路径也要单列一条”）见 `agents/readme.md`。

### 7.5 公共空间（`/public-workspaces/*` + agent 的 `/public/` 挂载）

**公共空间**是一组指定用户**只读**、只有 super 能读写删的共享空间。它和「业务空间」是**两类实体**
（权限模型不同：业务空间三级、公共空间二元），术语见根目录 `CONTEXT.md`，取舍见 `docs/adr/0001`。

```
POST   /public-workspaces/create               建公共空间
GET    /public-workspaces/list                 全部公共空间（管理视角）
GET    /public-workspaces/mine                 我被授权的
GET    /public-workspaces/detail/{id}          详情
PATCH  /public-workspaces/update/{id}          改说明（名字不可变）
DELETE /public-workspaces/delete/{id}          删空间 + 清内容
POST   /public-workspaces/grant/{id}           授权（body: user_name，幂等）
DELETE /public-workspaces/revoke/{id}/{user}   撤权
GET    /public-workspaces/members/{id}         成员列表
GET    /public-workspaces/files/list/{id}      列文件
POST   /public-workspaces/files/read/{id}      读一份（body: path）
POST   /public-workspaces/files/write/{id}     写一份（body: path + content，整份覆盖）
POST   /public-workspaces/files/delete/{id}    删一份（body: path）
```

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/public-workspaces/create` | body `{"name", "description"?}`；`name` 全局唯一、**不可变**（它是 agent 挂载路径的一段），重名 409 | **super** |
| GET | `/public-workspaces/list` | 全部公共空间 —— 「我能读的全部」 | **super** |
| GET | `/public-workspaces/mine` | 我被授权的 —— 「我是不是成员」。**super 的 `/mine` 也可能是空的**，这不是 bug | 登录 |
| GET | `/public-workspaces/detail/{id}` | 详情；不可见与不存在都 404（不泄露存在性） | 可见 |
| PATCH | `/public-workspaces/update/{id}` | body `{"description"}`；body 里塞 `name` 直接 422 | **super** |
| DELETE | `/public-workspaces/delete/{id}` | 204；先删表再清 store 内容，清理失败只记日志（`docs/adr/0005`） | **super** |
| POST | `/public-workspaces/grant/{id}` | body `{"user_name"}`（账号名），**成功返回 `{"result": true}`**，重复授权幂等 | **super** |
| DELETE | `/public-workspaces/revoke/{id}/{user_name}` | 204；本来就不是成员 404 | **super** |
| GET | `/public-workspaces/members/{id}` | 成员列表，**没有 `permission` 字段**（成员一律只读） | **super** |
| GET | `/public-workspaces/files/list/{id}` | `{public_workspace_id, files: ["notes/a.md"]}` | 可见 |
| POST | `/public-workspaces/files/read/{id}` | body `{"path"}`，不存在 404 | 可见 |
| POST | `/public-workspaces/files/write/{id}` | body `{"path", "content"}`，整份覆盖 | **super** |
| POST | `/public-workspaces/files/delete/{id}` | body `{"path"}`，不存在 404 | **super** |

**可见范围**（`CONTEXT.md` 的 Visibility）：**super 全部可见**（不是成员也能读、能写、能删）；
成员只看自己被授权的那些，一律只读。所以 `/list` 与 `/mine` 回答的是两个不同的问题。

**agent 侧的 `/public/` 与 `/memories/` 挂载**：内容都存在 langgraph store 里，两个挂载结构一样
（一格一个命名空间），方向相反：

```
/public/{公共空间名}/notes/a.md     # 命名空间 ("public", <id>, "filesystem")，不含 user_id
/memories/{业务空间名}/notes/a.md   # 命名空间 (user_id, <空间 id>, "filesystem")
```

- **`/public/` 对 agent 只读**：`/public/**` 的写操作被一条静态规则拒掉（`docs/adr/0003`）。静态规则区分不了用户，
  所以「只有 super 能写」不在 agent 这边表达 —— super 改内容走上面的 `files/write`、`files/delete`；
- **`/memories/` 只有 `default` 那格可写**（写要走 `interrupt` 人工批准），其它格子是只读资料（`docs/adr/0010`）。
  能用静态规则表达“只有这一格”，仅仅因为 `default` 是保留空间名（真实空间不许叫它）；
- 两个挂载的可见范围都**每轮重查**（`PublicWorkspaceDep` / `MemoryWorkspaceDep`），不写 checkpoint metadata
  —— 被移出空间立刻失效（`docs/adr/0004`）；
- `CompositeBackend` 的路由是启动时定死的静态前缀，所以「这个人能看哪几格」只能由
  `agents/fanout.py` 的 `FanoutMountBackend` 每次操作去读 `rt.context`，**不能**往
  `CompositeBackend.routes` 里塞；`ls` / `read_file` / `glob` / `grep` 都按当轮可见范围过滤
  （挂载根上的 `glob`/`grep` 会同时搜多个格子，命中路径带格子名前缀）；
- 公共空间的目录说明靠约定：谁建公共空间谁在里面放 `README.md`，模型按需自己读（不注入 system prompt）。

### 7.6 权限模型（重要）

- **`users.is_super` 只能直接改数据库**：没有 API、也没有 repository 写入口，注册/登录碰不到它
  （注册请求里塞 `is_super: true` 也无效）。
- **写操作和成员列表一律要求 super**（建/改/删空间、增删成员、查看成员列表），在路由层用 `SuperUser` 依赖拦成 403，
  早于任何数据库读写；空间内的 `admin` 也不能改空间、成员，也看不到成员列表（它可能误以为别人还有权限）。
- **读操作要求是成员**（空间详情、我参与的空间）；不是成员一律 404（不泄露空间是否存在）。
- **`user_workspaces.permission`（admin/editor/viewer，默认 viewer）参与空间内的鉴权**：
  `viewer` 能聊天（`/chat/*`）与读自己的记忆（`/memories/mine`、`/memories/read`、`/memories/all`）；
  写/删/上传记忆（`/memories/write`、`/memories/upload`、`/memories/delete`）要 `editor` 或 `admin`，`viewer` 403。
  改空间/成员仍然只认 `users.is_super`，跟空间内权限无关。
- **`default` 是虚拟空间**：库里没有记录，每个登录用户在里面都是 `admin`（`services/access.py` 里直接返回，
  不查库）；`/workspaces/mine` 永远带上它、`/workspaces/access/default` 恒为 `true`，但它没有详情/成员、
  也建不出来（保留名 409）。不传 `workspace_id` 的聊天与记忆都落在这里。
- **记忆与聊天都不从请求体取 `user_id`**：归属一律来自登录态，另有会话归属校验
  （`thread_id` 非本人 404）与 `(user_id, workspace_id)` 双维度存储隔离；
  请求体里多塞 `user_id` / `workspace_id`（在不该出现的地方）会被 Pydantic 挡成 422。
- **公共空间没有权限等级**：成员一律只读，写/删只认 `users.is_super`（`docs/adr/0001`）。
  它的**可见范围 = 成员 ∪ super** —— super 不是成员也能读、能写、能删，
  所以管理动作（建/改/删/授权/撤权/成员列表）走 `SuperUser`，读走 `CurrentUser` 再过一遍可见性，
  不可见与不存在一律 404（同样不泄露存在性）。

```sql
-- 授权 super（唯一途径）
update users set is_super = true where account = 'admin';
```

标志**不写进 JWT**，每个请求都回库现查，所以改完立刻生效，**升权/降权都不必重新登录**。

前端想决定「要不要给用户显示这个空间」时，用 `GET /workspaces/access/{workspace_id}`：
它只回答「我能不能进」——有权限时带 `permission`，没权限或空间不存在都返回
`has_access: false`（**不是 404**，因此不会泄露空间是否存在）。

### 7.7 curl 示例

```bash
BASE=http://127.0.0.1:8000
curl -s -X POST $BASE/auth/register -H 'Content-Type: application/json' \
     -d '{"account":"admin","email":"admin@example.com","password":"Passw0rd!123"}'
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
        -d '{"account":"admin","password":"Passw0rd!123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s $BASE/auth/me -H "Authorization: Bearer $TOKEN"

# 建空间（需已 update users set is_super = true）
WS=$(curl -s -X POST $BASE/workspaces/create -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"name":"proj-a","path":"/srv/ws/a"}' \
     | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 把另一个用户加成 viewer（user_name 就是对方的账号，注册时那个 account）——成功返回 true
curl -s -X POST $BASE/workspaces/grant/$WS -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"user_name":"someone","permission":"viewer"}'
# => true

# 我参与的空间 / 成员列表（成员列表需 super）
curl -s $BASE/workspaces/mine -H "Authorization: Bearer $TOKEN"
curl -s $BASE/workspaces/members/$WS -H "Authorization: Bearer $TOKEN"

# 自查：我有没有这个空间的权限（$WS 是 create 拿到的 id）
curl -s $BASE/workspaces/access/$WS -H "Authorization: Bearer $TOKEN"

# 日常聊天 / 日常记忆：不传 workspace_id 就落虚拟 default 空间（人人 admin，不用建空间）
curl -s -X POST $BASE/chat/send -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"message":"你好"}'

# 跑一轮流式，中途按停止：流会直接断，收尾结果从 stop 的响应体里拿
THREAD=$(curl -s -D - -o /dev/null -X POST $BASE/chat/stream -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"message":"讲个长的"}' \
     | grep -i '^x-thread-id:' | tr -d '\r' | awk '{print $2}')
curl -s -X POST $BASE/chat/stop -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d "{\"thread_id\":\"$THREAD\"}"
# => {"thread_id":"...","answer":"已经流出去的那部分文本"}
# 历史里这一轮已经收尾，接着聊就是新的一轮
curl -s -X POST $BASE/chat/send -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d "{\"message\":\"在吗\",\"thread_id\":\"$THREAD\"}"

# 公共空间（需 super）：建一个、写一份内容、授权给某个账号
PW=$(curl -s -X POST $BASE/public-workspaces/create -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"name":"handbook","description":"团队手册"}' \
     | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST $BASE/public-workspaces/files/write/$PW -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"path":"notes/a.md","content":"公共规范 v1"}'
curl -s -X POST $BASE/public-workspaces/grant/$PW -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"user_name":"someone"}'
# => {"result":true}

# 被授权的人：列 + 读（写 / 删是 403），agent 那边多一个只读的 /public/handbook/ 目录
curl -s $BASE/public-workspaces/mine -H "Authorization: Bearer $SOMEONE_TOKEN"
curl -s -X POST $BASE/public-workspaces/files/read/$PW -H "Authorization: Bearer $SOMEONE_TOKEN" \
     -H 'Content-Type: application/json' -d '{"path":"notes/a.md"}'
curl -s $BASE/memories/mine -H "Authorization: Bearer $TOKEN"
curl -s $BASE/memories/all -H "Authorization: Bearer $TOKEN"              # 我全部空间一格一条
curl -s -X POST $BASE/memories/upload -H "Authorization: Bearer $TOKEN" \
     -F workspace_id=$WS -F files=@./notes/a.md                            # 投喂项目资料（agent 只读）

# 踢出空间（真删关联行）
curl -s -X DELETE $BASE/workspaces/revoke/$WS/someone -H "Authorization: Bearer $TOKEN"
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

能从外部改的键在 yaml 里写成 `${oc.env:变量名,默认值}`（如 `password: ${oc.env:PG_PASSWORD,"1234"}`）：
`config/config.py` import 时先用 `python-dotenv` 读项目根目录的 `.env`，再解析这些插值，
所以优先级是**系统环境变量 > `.env` > yaml 默认值**。变量名与生产注入方式见 [6.1](#61-必须改的配置)。

其他模块读取方式：`from config import app_config, load_config`（`load_config()` 可重新加载）。

---

## 9. 测试

十个端到端自检脚本，都不需要 pytest，失败即非 0 退出，跑完自动清理测试数据：

```bash
uv run python tests/test_auth.py                      # 18 项，需 PostgreSQL
uv run python tests/test_workspace_api.py             # 30 项，需 PostgreSQL
uv run python tests/test_public_workspace_api.py      # 25 项，需业务库 + agents 库（不联网）
uv run python tests/test_public_workspace_mount.py    # 纯内存，无数据库、无模型 key
uv run python tests/test_memory_mount.py              # 纯内存，无数据库、无模型 key
uv run python tests/test_user_workspace_repository.py # 需 PostgreSQL
uv run python tests/test_user_workspace.py            # 内存 SQLite，无需数据库
uv run python tests/test_agent_memory_scope.py        # 纯内存，无数据库、无模型 key
uv run python tests/test_agent_chat_memory.py         # 需 PostgreSQL 的 agents 库，假模型
uv run python tests/test_agent_api.py                 # 47 项，需两个库，假模型（不联网）
```

| 脚本 | 覆盖 |
| --- | --- |
| `test_auth.py` | 注册 201 / 重复注册 409 / 非法输入 422 / 密码错误 401 / 双令牌 / 库中存 bcrypt 哈希 / `me` 200 / 无令牌·乱码·拿 refresh 当 access 401 / 刷新轮换且旧 refresh 失效 / 登出 204 且置空 / 禁用用户 403 |
| `test_workspace_api.py` | 非 super 建空间 403 / 未登录 401 / 非法入参 422 / 重名 409 / 创建者自动 admin（同一事务）/ 非成员看不见（空列表与 404）/ 成员可见 / admin 也改不了（403）/ 授权与改权限 upsert / 成员列表 / 移除成员 / 改名 / 级联删除；`is_super` 只用裸 SQL 改，顺便证明没有写入口 |
| `test_user_workspace_repository.py` | 仓储层：grant 改权限行数仍为 1 / 查权限 / 列我的空间 / 空结果 / 撤销 True·False / 删空间级联清关联 |
| `test_user_workspace.py` | 关联表：默认 viewer / 入库存小写 / CHECK 拒非法值 / 双向只读关系 / 重复授权被唯一约束拒 |
| `test_agent_memory_scope.py` | 纯内存验证记忆机制：`/memories/` 按用户命名空间隔离、`/memories/**` 只读（deny）、`interrupt → 批准 → 落库`、忘传 context 的失败模式 |
| `test_memory_mount.py` | 纯内存验证 `/memories/` 挂载：格子清单只列可见的、跨用户隔离、**只有 default 格能写**（其余 write/edit/delete 全拒且不落库）、静态规则顺序（default=interrupt、其余=deny）、根路径 `glob`/`grep` 扇出、批量下载按格子分发（同步/异步一致、看不见的一律 file_not_found）、批量上传一律拒、假模型端到端走 `ls`/`read_file`/`write_file` + `interrupt → 批准 → 落库` |
| `test_public_workspace_mount.py` | 纯内存验证 `/public/` 挂载：成员只看得到自己被授权的、同一公共空间的两个人读到同一份、super 全部可见、`glob`/`grep` 跨格子扇出且不越权、写操作被 deny、假模型端到端走 `ls`/`read_file`/`write_file` |
| `test_public_workspace_api.py` | HTTP 层：非 super 建空间 403、`/list` 与 `/mine` 对 super 的口径差异、授权前不可见（404）与授权后可读、成员写删 403、路径校验 422、撤权后立即 404、删空间把 store 内容一起清掉、关联记录级联删除 |
| `test_agent_chat_memory.py` | 真图 + 真检查点：agent 写记忆被拦下、批准后落库（**落 default**，即使会话在别的空间）、换用户/换空间看不见、用户侧直写、短期记忆按用户隔离、会话元数据（user_id / workspace_id）已进检查点 |
| `test_agent_api.py` | HTTP 层：`/memories/*` 写读列删 + `/memories/all` 全空间清单 + `/memories/upload` 逐份结果（二进制那份报错、其余照常落库）、viewer 只读（含上传 403）、非成员 404、跨空间隔离、路径带空间名能原样喂回；`/chat/*` 九端点契约、SSE 事件序列、接力聊天、借别人 thread_id 404、`interrupt → approve` 后记忆落 default、短期记忆的读/删消息/删文件/删会话 |
| `test_chat_stop.py` | 停止：四种入口的收尾（工具中途 / 生成中途 / 等人批准 / 已跑完）、已流出文本写回历史、续聊不合并、幂等、409 互斥、404 鉴权、流式跑到一半按停止 |
| `test_chat_stop_cross.py` | **跨进程**停止（两个裸 agent 当两个 worker）：停止请求落到没有那一轮的 worker、跨进程 409、心跳回收陈行、孤儿检查点恢复不合并、**通知丢了靠重连补扫兜底**（从服务器端踢掉 LISTEN 连接） |
| `test_chat_stop_cross_http.py` | **HTTP 层跨进程**停止（两个 `create_app()` 当两个 worker）：`/chat/stop` 打到没有那一轮的进程仍能停掉并拿回部分文本、跨进程 409、没人在跑时不靠超时、404 不泄露存在性 |

前三个业务脚本用 `tester_*` / `wsroot_*` / `wsuser_*` 前缀账号并在结束时清理；agent 相关脚本用 `agt_api_*` / `chat_stop_*` 前缀，
并额外清 `agents` 库里的 `checkpoints` / `store` 残留。`/chat/*` 的测试全部注入假模型（不联真实 LLM）。

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

**Q：建空间 / 改空间 / 看成员列表返回 403？**
这些操作只允许 super —— `update users set is_super = true where account = '你的账号'`，
改完立即生效、不用重新登录（见 [7.6](#76-权限模型重要)）。空间内的 admin 也不行，成员列表连 admin 都看不了。

**Q：空间详情返回 404，但我当然是管理员？**
读操作先看 `user_workspaces` 里的成员关系；不是成员就统一 404（不泄露空间是否存在）。
super 本身不会自动成为成员，但建空间时会被自动写成 admin。

**Q：怎么给用户加/改权限？**
`POST /workspaces/grant/{workspace_id}`，body `{"user_name":"对方的账号","permission":"admin / editor / viewer"}`，
同一个人重复提交就是改权限（内部是 PG `ON CONFLICT DO UPDATE`，并发下不会撞唯一约束）；
**成功返回 `true`（200）而不是 204 空响应**，前端不必处理空 body；失败仍是 404/403/422。
入参用**账号名**（account）而不是用户 id；账号不存在 404，账号格式不合法 422。
想“踢出空间”用 `DELETE /workspaces/revoke/{workspace_id}/{user_name}`——那是真删关联行，不是降级成 viewer。

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
