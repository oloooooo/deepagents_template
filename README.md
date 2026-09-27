# deepagents_template

FastAPI + PostgreSQL + loguru 的后端模板：配置文件（OmegaConf + pydantic）、loguru 日志、
用户注册登录与 JWT 鉴权、**按微服务划分的知识库空间**（super 建删授权、成员只读、agent 只读挂载）、
聊天与两层记忆、alembic 数据库迁移。

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
| 数据库 | PostgreSQL（默认连 `127.0.0.1:5432`：业务库 `user_related`，另有 `agents` 库放检查点与 store） |
| 操作系统 | 开发机 Windows/Linux 均可；生产建议 Linux（见 [6](#6-生产环境部署)） |

```bash
uv sync --frozen      # 按 uv.lock 精确安装依赖（新增依赖用 uv add）
```

---

## 2. 项目结构

````
.
├── config/                  # 配置：OmegaConf 读 yaml → pydantic 校验 → 单例 app_config
│   ├── config.yaml          #   postgresql / logger / auth / kb 四段，改配置先看这里
│   ├── config.py            #   AppConfig 等模型 + load_config()（extra="forbid"，写错键直接报错）
│   └── __init__.py          #   对外只暴露 app_config、load_config 与各段模型
├── logger/__init__.py       # loguru 控制台 + 滚动文件 sink；接管标准库 logging
├── dependencies/
│   ├── database.py          #   异步 engine / AsyncSessionFactory / get_session（请求级会话）
│   ├── auth.py              #   get_current_user（Bearer → JWT → User）、CurrentUser / SuperUser 注入类型
│   ├── agent.py             #   AgentDep：从 app.state 取 lifespan 起好的 GeneralAgent（未就绪 503）
│   └── kb.py                #   KbCellsDep：当轮可见的微服务（名字→id），每轮查一次填进 AgentContext
├── models/                  # SQLAlchemy ORM
│   ├── __init__.py          #   Base（命名约定）/ BaseModel（id+时间戳）；末尾 import 各表模型
│   ├── user.py              #   users 表：account / email / hashed_password / refresh_token / is_super
│   ├── microservice.py      #   microservices 表：name（唯一）/ description —— 微服务即知识库空间
│   └── user_microservice.py #   user_microservices 关联表：二元成员关系，**没有** permission 列
├── repositories/            # 数据库读写类（User / Microservice / UserMicroservice）
├── services/                # 路由功能实现（Auth / Memory / Chat / Microservice / Kb）
│   ├── microservice.py      #   建删授权；删库顺序：先删表记录再清存储内容
│   └── kb.py                #   知识库文件读写；可见范围校验的唯一入口（不可见=不存在，同一个 404）
├── routers/                 # FastAPI 路由（auth.py、chat.py、memory.py、microservice.py、kb.py）
│   └── schemas/             #   请求/响应模型：auth / chat / memory / microservice / kb / paths
├── main.py                  # 应用入口：`create_app()`（每实例一份 state，测试靠它模拟多 worker）/ lifespan / /health / uvicorn 参数
├── migrations/              # alembic 迁移（连接串来自 config.yaml 的 postgresql.user 段）
│   ├── env.py
│   └── versions/*.py        #   users → 旧 workspace 系列（已被 b4e7f1a92c35 清掉）→ microservices 等
├── tests/                   # 端到端自检脚本（11 个，均无需 pytest，跑完自清理）
├── CONTEXT.md               # 领域术语表：微服务 / 知识库 / 共享知识 / 私有文档 / Visibility / Turn owner…
├── docs/adr/                # 架构决定记录（0001~0006：空间为什么等于微服务、backend 为什么可切换、为什么只读、停止为什么靠 LISTEN/NOTIFY…）
├── alembic.ini              # 只配 script_location / 日志，URL 由 env.py 注入
└── agents/                  # agent 本体（模块约定见 agents/readme.md）
    ├── agent.py             #   GeneralAgent / AgentMemory（跑图 + 存取记忆）
    ├── turns.py             #   停止：TurnRegistry（本进程：找 task + 攒文本）+ RunningTurns（跨进程：表 + chat_drain 通道）
    ├── kb/                  #   知识库：storage.py（backend 工厂，store/local 可切换）/ mount.py（/kb/ 挂载）/ permissions.py（静态只读）
    ├── prompt/              #   系统提示词 .md（system.md + kb.md，langchain PromptTemplate.from_file 加载）
    ├── mcp/  skills/        #   MCP / Skill 占位包（注册表等第一个真实接入再建，挂点 microservices.id）
    └── readme.md            #   模块约定（记忆与知识库的隔离边界、context 必传、停止的取舍）
````

分层约定：**routers 只收参/返回 → services 写业务逻辑 → repositories 只做数据库读写 → models 定义表**；
请求/响应模型放 `routers/schemas/`。配置一律 `from config import app_config`，日志一律 `from logger import logger`。
知识库的**管理面**（建删授权、文件读写）走同一套分层；`agents/` 里只放 agent 侧的挂载与权限
（约定见 `AGENTS.md` 与 `agents/readme.md`）。

---

## 3. 首次运行

```bash
# 1) 装依赖（已有 .venv 可跳过）
uv sync --frozen

# 2) 改 config/config.yaml 里 postgresql.user 的连接信息
#    （本机就是 PostgreSQL 的话，通常只改 user/password/db_name）
#    模型参数不走 yaml：在 .env 里设 OPEN_MODEL / OPEN_BASE_URL / OPEN_API_KEY（见 .env_example）

# 3) 建业务库的表（users / microservices / user_microservices + alembic_version）
uv run alembic upgrade head

# 4) 启动（启动时智能体会自动建 checkpoints / checkpoint_blobs / checkpoint_writes / store 四张表）
uv run python main.py         # http://127.0.0.1:8000 ，Swagger 在 /docs

# 5) 自检（注册/登录/me/刷新/登出/禁用用户，共 18 项，跑完自动清理测试数据）
uv run python tests/test_auth.py

# 6) 把自己设成 super，否则建不了微服务（super 只能在数据库里改，见 7.6）
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
所以**不用**改 `alembic.ini` 里的 `sqlalchemy.url`；迁移只作用于业务用户库。
检查点与 store 的表由 langgraph 自建自管（库连接见 `agents/config.py` 的 `AgentPostgreConfig`，
默认 `agents` 库），不归 alembic 管。

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
> 模型参数走的是同一个 `.env`，但由 `agents/config.py` 的 `ModelConfig` 直接读
> （`OPEN_MODEL` / `OPEN_BASE_URL` / `OPEN_API_KEY`）。

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

约定：**一个操作一条独立路径**，动作词写进路径（如 `/microservices/create`、`/microservices/grant/{id}`），
不用不同 HTTP 方法复用同一条路径；请求/响应模型在 `routers/schemas/`，路由参数顺序统一为
「路径 → 请求体 → 当前用户（或 super）→ agent → session」。

### 7.1 认证

| 方法 | 路径 | 说明 | 需要鉴权 |
| --- | --- | --- | --- |
| POST | `/auth/register` | 注册（账号 3–50 位、邮箱、密码 ≥8 位） | 否 |
| POST | `/auth/login` | 登录，返回 access + refresh 双令牌（账号或邮箱都可） | 否 |
| POST | `/auth/refresh` | 用 refresh token 换新令牌（轮换，旧 refresh 立即失效） | 否 |
| POST | `/auth/logout` | 登出，清空库中 refresh token | Bearer |
| GET | `/auth/me` | 当前登录用户（含只读字段 `is_super`） | Bearer |
| GET | `/health` | 健康检查 | 否 |

### 7.2 微服务（知识库空间）

**微服务就是它的知识库空间**，一一对应、没有独立的空间实体（`docs/adr/0001`）：
「创建知识库空间」= 创建微服务，「删除知识库空间」= 删除微服务（内容级联清理）。
权限**只有一个维度 = 成员关系**（二元，没有 admin/editor/viewer 等级）。

```
POST /microservices/create              建微服务（= 建知识库空间）
POST /microservices/delete/{id}         删微服务（= 删知识库，级联清内容）
POST /microservices/list                我能读哪些（super 全部 / 普通用户 = 我成员的）
POST /microservices/mine                我是不是成员（纯成员关系，super 可能是空的）
POST /microservices/grant/{id}          授权（按账号名，幂等）
POST /microservices/revoke/{id}         收权（按账号名，幂等）
```

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/microservices/create` | body `{"name", "description"?}`；`name` 全局唯一（它是 agent 挂载路径的一段：字母数字 `_ . -`），重名 409 | **super** |
| POST | `/microservices/delete/{id}` | 204；**先删表记录、再清存储内容**（store 命名空间与磁盘目录都清，失败只记日志）；删完名字可复用 | **super** |
| POST | `/microservices/list` | 我能读到的微服务 —— super 返回全部，普通用户返回我作为成员的那些 | 登录 |
| POST | `/microservices/mine` | 我的成员关系 —— **与 `list` 是两个问题**：super 的 `mine` 可以是空的（他不是成员，但可见范围含 super） | 登录 |
| POST | `/microservices/grant/{id}` | body `{"account":"对方的账号"}`；重复授权幂等（204）；账号不存在 404 | **super** |
| POST | `/microservices/revoke/{id}` | 204；本来就不是成员也 204（幂等） | **super** |

删除要连 store 一起清内容，所以路由带 `AgentDep`：agent 没启动时删除返回 503
（宁可不让删，也不留下清不掉的孤儿数据）。

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
| POST | `/chat/send` | body `{"message", "thread_id"?}`；不传 `thread_id` 就新开会话并返回。返回 `{thread_id, answer, interrupt}`——`interrupt` 非空表示在等人批准 | 登录 |
| POST | `/chat/stream` | 同样的 body，返回 SSE：`event: token / tool_call / interrupt / done` + 一行 JSON（统一 `{"text":…, "data":{…}}`）。新建的 thread_id 走响应头 `X-Thread-Id` | 登录 |
| POST | `/chat/approve` | body `{"thread_id", "decisions": [{"type": "approve"}]}`，`decisions` 原样透传给 langgraph（approve / edit / reject / respond）。可见清单（`kb_cells`）与首轮一样每轮重查 | 本人会话 |
| POST | `/chat/stop` | body `{"thread_id"}`，返回 `{thread_id, answer}`——`answer` 是这一轮最终留在历史里的文本。**幂等**：已经跑完的会话再按一次不报错也不改历史 | 本人会话 |
| GET | `/chat/mine` | 我的会话（`thread_id` / `updated_at`）。读的是 checkpoint metadata，不另建表 | 登录 |
| GET | `/chat/state/{thread_id}` | `{thread_id, messages, answer, files}` | 本人会话 |
| GET | `/chat/history/{thread_id}` | 消息列表（最旧→最新，带 `id` 与 `role`） | 本人会话 |
| POST | `/chat/messages/delete` | body `{"thread_id", "message_ids": [...]}`，删单条消息后还能继续聊 | 本人会话 |
| POST | `/chat/files/delete` | body `{"thread_id", "paths": ["/tmp.txt"]}`，删会话内临时文件 | 本人会话 |
| DELETE | `/chat/delete/{thread_id}` | 删整条会话（检查点），**长期记忆不受影响** | 本人会话 |

`thread_id` 非本人 / 不存在一律 **404**（不泄露存在性）；归属不查表——落库时 thread_id 会拼成
`{user_id}:{thread_id}`，拿到别人的 id 也读不到。

#### 中断（interrupt）与停止（stop）是两件事

名字相近但方向相反，`CONTEXT.md` 里把词定死了：

| | 中断 | 停止 |
| --- | --- | --- |
| 谁的动作 | agent（想写 `/memories/**`，等人点头） | 用户（按了暂停键） |
| 之后 | **可以续跑同一轮**，走 `/chat/approve` | **不可续跑**，只能发新消息开新一轮 |
| 实现 | `MEMORY_PERMISSIONS` 的 `mode="interrupt"` | `POST /chat/stop` 取消在跑的那一轮 |

停止的语义（见 `docs/adr/0005`）：

- **短期记忆保留**，并且**就地收尾**：已经流出去的文本会补成一条 AI 消息，一个字都没流出来时
  用 `（用户停止了本轮）` 占位。不收尾的话下一轮会把停掉的那条消息和新消息**合并成一次请求**；
- **不回滚已发生的副作用**：工具已经写过的文件、已经落库的 `/memories/` 都留着 ——
  停止是「到此为止」，不是「撤销」；
- 被停止时 SSE 流是**直接断的**（任务被取消，没有机会再 yield），结果从 `/chat/stop` 的响应体里取；
- 一个会话同时只允许一轮在跑，重复提交返回 **409**（`await` 完 `/chat/stop` 再发新消息就不会撞上）。

`/chat/stop` 不靠负载均衡器做会话粘性 —— 它把“要停这一轮”写进 `running_turns` 表，
再用 Postgres 的 `LISTEN/NOTIFY` **广播**给所有 worker；只有跑着那一轮的那个进程会动手。
所以停止请求落到哪台都行，轮询 / 最少连接随便配（实现与取舍见 `docs/adr/0006`）。

同一张表的 PK 兼任跨进程的 409 互斥：**一个会话同时最多一轮**，多 worker 下也成立。
worker 挂在轮次中途时，那行靠心跳回收（60 秒），下一轮开轮次前会先把留在半路的检查点
收尾（“孤儿恢复”，见 `docs/adr/0006`）。

### 7.4 长期记忆（`/memories/*`，agent 那边的 `/memories/` 挂载）

**一人一份**：store 命名空间是 `(user_id, "filesystem")`，没有格子层，
`/memories/` 下就是这个人的根，用来存个人偏好这类东西（与知识库是两个词，见 `CONTEXT.md`）。

```
GET  /memories/all                我全部记忆文件的清单
POST /memories/read               读一份记忆
POST /memories/write              写 / 覆盖一份记忆
POST /memories/upload             上传文本文件（multipart，一次可多份）
POST /memories/delete             删一份记忆（204）
```

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/memories/all` | `{memories: ["/memories/notes/a.md", …]}` | 登录（只有自己的） |
| POST | `/memories/read` | body `{"path"}`（`path` 带斜杠所以放 body），不存在 404 | 登录 |
| POST | `/memories/write` | body `{"path", "content"}`，整份覆盖，返回 `{"path": "/memories/…"}` | 登录（用户直写，不经模型、不需批准） |
| POST | `/memories/upload` | `multipart/form-data`：一份或多份 **UTF-8 文本**文件；逐份返回 `{file, path, error}`，某一份失败（二进制 / 超 10 万字符 / 脏文件名）不影响其它份 | 登录 |
| POST | `/memories/delete` | body `{"path"}`，不存在 404 | 登录 |

**路径两个方向的对齐**：REST 按相对路径寻址，但**响应里回的是 agent 眼里的路径**
（`/memories/notes/a.md`）—— 用户把路径原样丢给 agent 就能读到。请求里的 `path` 两种写法都收：
`notes/a.md`、`/memories/notes/a.md`。

**agent 侧写 `/memories/**` 必须人工批准**（`MEMORY_PERMISSIONS` 的 interrupt，`POST /chat/approve`）；
用户侧的写入一律直写、不用批准。规则的顺序与「裸路径也要单列一条」的坑见 `agents/readme.md`。

### 7.5 知识库（`/kb/files/*` + agent 的 `/kb/` 挂载）

每个微服务一个知识库，格子里固定两层（`CONTEXT.md` 的 KB mount，`docs/adr/0003`）：

- **`shared`** —— 共享知识（如 `概念.md`）：**成员 ∪ super** 可读，只有 super 能写；
- **`private`** —— 某个用户的私有文档（如 `xlsx`）：**本人 ∪ super** 可读，只有 super 能写
  （`account` 指定主人，缺省是自己）。

```
POST /kb/files/list       列某层目录          登录（成员 ∪ super）
POST /kb/files/read       读文本文件          登录（二进制 422，提示走 download）
POST /kb/files/download   下载原始字节        登录（xlsx 等）
POST /kb/files/write      写文本文件（覆盖）   super
POST /kb/files/upload     上传文件（multipart）super，逐份互不影响
POST /kb/files/delete     删一份文件          super
```

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/kb/files/list` | body `{"microservice", "layer": "shared\|private", "path"?（默认层根）, "account"?}`；返回 `entries`，每条的 `path` 是 **agent 完整路径**（`/kb/{微服务名}/{层}/…`） | 可见 |
| POST | `/kb/files/read` | 同上定位；二进制文件 422（`二进制文件请用 /kb/files/download 取原始内容`） | 可见 |
| POST | `/kb/files/download` | 返回 `application/octet-stream` 原始字节，`Content-Disposition` 带 RFC 5987 文件名 | 可见 |
| POST | `/kb/files/write` | body 加 `{"content"}`（≤10 万字符，整份覆盖），返回 agent 完整路径 | **super** |
| POST | `/kb/files/upload` | `multipart`：`files`（可多份）+ `microservice` + `layer` + `account?`；逐份返回 `{file, path, error}` | **super** |
| POST | `/kb/files/delete` | body 同 read；不存在 404 | **super** |

要点：

- **不可见 = 不存在**：不是成员（或根本没有这个微服务）一律回同一个 404，不泄露存在性；
  路径穿越（`..` / `~`）在 schema 层与存储层各挡一道，422；
- **存储介质可切换**（`docs/adr/0002`）：`config.yaml` 的 `kb.backend` 全局选 `store`（postgres）
  或 `local`（磁盘 `kb.local_root`），`kb.overrides` 按微服务名覆盖；`s3` 枚举预留、未实现。
  换 backend 是**运维动作**，内容不自动迁移；
- **agent 侧的 `/kb/` 挂载**（`agents/kb/`）：

```
/kb/                                  我能看见的微服务清单（每轮按成员关系重查）
/kb/{微服务名}/shared/概念.md          共享知识
/kb/{微服务名}/private/我的.xlsx       我自己的私有文档（别人的永远看不到）
```

  - **对 agent 恒只读**：一条静态 `deny` 规则（`KB_PERMISSIONS`）挡掉所有写，挂载层的写方法
    还会再拒一次（第二道防线）；super 的写走上面的 REST，agent 帮不上笔；
  - 可见清单每轮注入 `AgentContext.kb_cells`（不落 checkpoint metadata，被移出成员立刻失效）；
  - 根上的 `ls` / `glob` / `grep` 会扇出到所有可见格子，命中路径带格子名前缀；
  - 二进制文件 agent 读不出文本（存取不解析），需要内容时走 `/kb/files/download`。

### 7.6 权限模型（重要）

- **`users.is_super` 只能直接改数据库**：没有 API、也没有 repository 写入口，注册/登录碰不到它
  （注册请求里塞 `is_super: true` 也无效）。
- **写操作一律要求 super**，在路由层用 `SuperUser` 依赖拦成 403，早于任何数据库读写：
  建 / 删微服务、授权 / 收权，以及知识库的 `write` / `upload` / `delete`。
  **用户对空间一律只读**——公有的不能改，私有的暂时也不能改（`docs/adr/0003`）。
- **读操作要求可见**（成员 ∪ super）：不可见与不存在返回**同一个 404**（不泄露存在性）。
  `list`（我能读哪些）与 `mine`（我是不是成员）回答两个不同的问题——super 的 `mine` 可以是空的。
- **成员没有等级**：`user_microservices` 没有 `permission` 列，关系是二元的（`docs/adr/0001`）；
  旧体系的 admin / editor / viewer 三级权限已随 workspace 体系一起移除。
- **聊天与记忆都不从请求体取 `user_id`**：归属一律来自登录态，另有会话归属校验
  （`thread_id` 非本人 404）与 `{user_id}:{thread_id}` 拼接隔离；
  请求体里多塞 `user_id` 会被 Pydantic 挡成 422（`extra="forbid"`）。
- **agent 对知识库恒只读**（`KB_PERMISSIONS` 静态 deny，`docs/adr/0003`）；对 `/memories/**` 可写但
  必须人工批准（interrupt）。两条规则静态编译进图，区分不了用户——所以 super 想写知识库也走 REST。

```sql
-- 授权 super（唯一途径）
update users set is_super = true where account = 'admin';
```

标志**不写进 JWT**，每个请求都回库现查，所以改完立刻生效，**升权/降权都不必重新登录**。

### 7.7 curl 示例

```bash
BASE=http://127.0.0.1:8000
curl -s -X POST $BASE/auth/register -H 'Content-Type: application/json' \
     -d '{"account":"admin","email":"admin@example.com","password":"Passw0rd!123"}'
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
        -d '{"account":"admin","password":"Passw0rd!123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s $BASE/auth/me -H "Authorization: Bearer $TOKEN"

# 建微服务 = 建知识库空间（需已 update users set is_super = true）
MS=$(curl -s -X POST $BASE/microservices/create -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"name":"order-svc","description":"订单微服务"}' \
     | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 授权给某个账号（account 是对方的账号名）——重复提交也返回 204（幂等）
curl -s -X POST $BASE/microservices/grant/$MS -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"account":"someone"}'

# 我能读哪些 / 我是不是成员
curl -s -X POST $BASE/microservices/list -H "Authorization: Bearer $TOKEN"
curl -s -X POST $BASE/microservices/mine -H "Authorization: Bearer $TOKEN"

# super 往共享层写一份知识，再授权的人就能读到（agent 那边是只读的 /kb/order-svc/shared/）
curl -s -X POST $BASE/kb/files/write -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"microservice":"order-svc","layer":"shared","path":"概念.md","content":"订单=先扣库存"}'
curl -s -X POST $BASE/kb/files/read -H "Authorization: Bearer $SOMEONE_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"microservice":"order-svc","layer":"shared","path":"概念.md"}'

# super 把一份 xlsx 放进某人的私有层；本人原样下载（read 会 422 提示走 download）
curl -s -X POST $BASE/kb/files/upload -H "Authorization: Bearer $TOKEN" \
     -F files=@./报表.xlsx -F microservice=order-svc -F layer=private -F account=someone
curl -s -X POST $BASE/kb/files/download -H "Authorization: Bearer $SOMEONE_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"microservice":"order-svc","layer":"private","path":"报表.xlsx"}' -o 表.xlsx

# 聊天：不传 thread_id 就新开会话；agent 每轮自动拿到可见的微服务清单
curl -s -X POST $BASE/chat/send -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"message":"看看订单知识库的 shared 层有什么"}'

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

# 个人记忆：一人一份，用户直写不需批准（agent 写 /memories/** 才要人工批准）
curl -s $BASE/memories/all -H "Authorization: Bearer $TOKEN"
curl -s -X POST $BASE/memories/write -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"path":"prefs.md","content":"喜欢简短回答"}'
curl -s -X POST $BASE/memories/upload -H "Authorization: Bearer $TOKEN" \
     -F files=@./notes.txt

# 收权 / 删微服务（删微服务会把知识库内容一起清掉）
curl -s -X POST $BASE/microservices/revoke/$MS -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"account":"someone"}'
curl -s -X POST $BASE/microservices/delete/$MS -H "Authorization: Bearer $TOKEN"
```

启动后 Swagger 文档在 `/docs`，OpenAPI JSON 在 `/openapi.json`。

---

## 8. 配置项

`config/config.yaml` 的字段与 `config/config.py` 的 pydantic 模型一一对应
（`extra="forbid"`：yaml 里写错键名会**直接报错**，不会被静默忽略）：

| 段 | 用途 |
| --- | --- |
| `postgresql.user` | 业务用户库（users / microservices / user_microservices）：应用异步引擎、alembic 迁移都用它 |
| `logger` | loguru：`level` / `dir`（相对路径按项目根解析）/ `rotation` / `retention` / `backtrace` / `diagnose`，文件写到 `<dir>/app_YYYY-MM-DD.log` |
| `auth` | JWT：`secret_key`（≥32 字节）/ `algorithm` / `access_token_expire_minutes` / `refresh_token_expire_days` |
| `kb` | 知识库存储后端：`backend`（`store` \| `local`，`s3` 枚举预留未实现）、`local_root`（相对项目根）、`overrides`（微服务名 → backend，没配的用全局值） |
| `deepagent` | 模型参数的 yaml 段，**当前未接线**：agent 实际读 `OPEN_MODEL` / `OPEN_BASE_URL` / `OPEN_API_KEY`（见 `agents/config.py`） |

`PostgreConfig` 暴露两个连接串：`.uri`（`postgresql://…`，给 psycopg/连接池/checkpointer 用）与
`.sqlalchemy_uri`（`postgresql+psycopg://…`，同步/异步引擎通用）。检查点 / store 用的库连接在
`agents/config.py` 的 `AgentPostgreConfig`（默认 `agents` 库）。

能从外部改的键在 yaml 里写成 `${oc.env:变量名,默认值}`（如 `password: ${oc.env:PG_PASSWORD,"1234"}`）：
`config/config.py` import 时先用 `python-dotenv` 读项目根目录的 `.env`，再解析这些插值，
所以优先级是**系统环境变量 > `.env` > yaml 默认值**。变量名与生产注入方式见 [6.1](#61-必须改的配置)。

其他模块读取方式：`from config import app_config, load_config`（`load_config()` 可重新加载）。

---

## 9. 测试

十一个端到端自检脚本，都不需要 pytest，失败即非 0 退出，跑完自动清理测试数据：

```bash
uv run python tests/test_auth.py                  # 18 项，需 PostgreSQL
uv run python tests/test_memory_mount.py          # 纯内存，无数据库、无模型 key
uv run python tests/test_agent_memory_scope.py    # 纯内存，无数据库、无模型 key
uv run python tests/test_kb_mount.py              # 纯内存，无数据库、无模型 key
uv run python tests/test_kb_backend.py            # 临时目录 + 内存 store，无数据库、无模型 key
uv run python tests/test_microservice_api.py      # 29 项，需两个库，假模型（不联网）
uv run python tests/test_agent_api.py             # 39 项，需两个库，假模型（不联网）
uv run python tests/test_agent_chat_memory.py     # 需 PostgreSQL 的 agents 库，假模型
uv run python tests/test_chat_stop.py             # 停止，需两个库
uv run python tests/test_chat_stop_cross.py       # 跨进程停止（两个裸 agent），需两个库
uv run python tests/test_chat_stop_cross_http.py  # 跨进程停止（两个 app），需两个库
```

| 脚本 | 覆盖 |
| --- | --- |
| `test_auth.py` | 注册 201 / 重复注册 409 / 非法输入 422 / 密码错误 401 / 双令牌 / 库中存 bcrypt 哈希 / `me` 200 / 无令牌·乱码·拿 refresh 当 access 401 / 刷新轮换且旧 refresh 失效 / 登出 204 且置空 / 禁用用户 403 |
| `test_memory_mount.py` | `/memories/` 挂载：跨用户隔离（命名空间 `(user_id, "filesystem")`）、`/memories/**` interrupt 且裸路径也拦、根路径 `glob` 扇出、批量上传一律拒、假模型端到端走 `ls`/`read_file`/`write_file` + `interrupt → 批准 → 落库` |
| `test_agent_memory_scope.py` | 记忆机制的纯内存面：跨用户隔离、`deny` 只读、`interrupt → 批准 → 落库`、忘传 context 的失败模式 |
| `test_kb_mount.py` | `/kb/` 挂载：可见清单来自 `kb_cells`（空=什么都看不见）、格子恒为两层、private 按 user_id 隔离、写全拒（同步/异步/批量）且**不触发** interrupt、根上 `glob` 扇出不越权、不可见格子与不存在的层同为 404 口径、假模型端到端 |
| `test_kb_backend.py` | backend 切换：`backend_for` 全局/覆盖/s3 报错、local 落盘读删与 `../` 穿越防护、private 目录带 user_id、purge 把 store 前缀与磁盘目录都清、同一挂载跟着配置走 |
| `test_microservice_api.py` | HTTP 层：非 super 建删授权 403、重名 409、非法名 422、grant/revoke 幂等 204、`list` 与 `mine` 口径差异、成员只读（写 403）、不可见与不存在同 404、private `account` 越权 404、二进制 read 422 / download 逐位一致、删微服务后 store 清零且名字可复用、重建后内容是空的 |
| `test_agent_api.py` | `/memories/*` 写读列删 + `/chat/*` 九端点契约、SSE 事件序列、接力聊天、借别人 thread_id 404、`interrupt → approve` 闭环、短期记忆的读/删消息/删文件/删会话 |
| `test_agent_chat_memory.py` | 真图 + 真检查点：agent 写记忆被拦下、批准后落库、换用户看不见、用户侧直写、短期记忆按用户隔离、流式 interrupt 事件 |
| `test_chat_stop.py` | 停止：四种入口的收尾（工具中途 / 生成中途 / 等人批准 / 已跑完）、已流出文本写回历史、续聊不合并、幂等、409 互斥、404 鉴权、流式跑到一半按停止 |
| `test_chat_stop_cross.py` | **跨进程**停止（两个裸 agent 当两个 worker）：停止请求落到没有那一轮的 worker、跨进程 409、心跳回收陈行、孤儿检查点恢复不合并、**通知丢了靠重连补扫兜底**（从服务器端踢掉 LISTEN 连接） |
| `test_chat_stop_cross_http.py` | **HTTP 层跨进程**停止（两个 `create_app()` 当两个 worker）：`/chat/stop` 打到没有那一轮的进程仍能停掉并拿回部分文本、跨进程 409、没人在跑时不靠超时、404 不泄露存在性 |

业务脚本用 `kb_api_*` / `agt_api_*` / `chat_stop_*` 等前缀账号并在结束时清理；
agent 相关脚本额外清 `agents` 库里的 `checkpoints` / `store` 残留，`/chat/*` 全部注入假模型（不联真实 LLM）。

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

**Q：建微服务 / 授权 / 删除返回 403？**
这些操作只允许 super —— `update users set is_super = true where account = '你的账号'`，
改完立即生效、不用重新登录（见 [7.6](#76-权限模型重要)）。

**Q：读知识库返回 404，但我确定这个微服务存在？**
读操作先看可见范围（成员 ∪ super）；**不可见与不存在返回同一个 404**，不泄露微服务是否存在。
super 不是成员也能读，但普通用户必须先被 `grant`（`POST /microservices/grant/{id}`）。
注意 `/microservices/list` 与 `/microservices/mine` 是两个口径：super 的 `mine` 可以是空的。

**Q：知识库写入返回 403？**
用户对空间一律只读（公有、私有都一样，`docs/adr/0003`），写入口只有 super 的
`/kb/files/write`、`/kb/files/upload`、`/kb/files/delete`；agent 对 `/kb/**` 也是只读
（静态 deny），别让模型去写。

**Q：怎么给用户加/改访问？**
`POST /microservices/grant/{id}`，body `{"account":"对方的账号"}`——重复提交就是幂等的 204；
踢出用 `POST /microservices/revoke/{id}`，同样幂等。入参用**账号名**（account）而不是用户 id；
账号不存在 404。成员关系没有等级（只有在/不在），要撤权就是 revoke，不是降级。

**Q：怎么切到另一个数据库？**
只改 `config/config.yaml`（或对应环境变量），应用与 alembic 都用同一份配置，无需改代码。
知识库换存储介质（store ↔ local）改 `kb.backend`（或 `kb.overrides`），**内容不自动迁移**，
要搬自己搬（`docs/adr/0002`）。

---

## 11. deepagents 智能体

`agents/agent.py` 里的 `GeneralAgent` 把 deepagents 包成一个异步类，用 PostgreSQL 做两类持久化：

| 记忆类型 | 载体 | 生命周期 | 对应表 |
| --- | --- | --- | --- |
| 短时记忆（state） | `AsyncPostgresSaver` 检查点 | 单个 `thread_id` 内，**跨进程/重启**保留 | `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` |
| 长期记忆（store） | `AsyncPostgresStore` + `StoreBackend` | 跨会话、跨进程；`/memories/` 与 store 模式的 `/kb/` 走它 | `store` |

- 除 `/memories/`、`/kb/` 外的路径仍由 `StateBackend` 管理（线程内临时文件），`ThreadState.files` 随检查点一起落库；
- **用户隔离**：`thread_id` 落库时加用户前缀（`{user_id}:{thread_id}`），`/memories/` 用 `{user_id}`
  作 store 命名空间，`/kb/` 的 private 层命名空间带 `user_id`——拿到别人的 thread_id 也读不到；
- **`AgentContext` 每轮必传**：`user_id`（记忆与 private 层的命名空间）+ `kb_cells`（可见微服务清单，
  由 `dependencies/kb.py` 查库填入）。`ainvoke` / `astream` 内部已传好；**新增调用路径
  （子图、后台任务）必须自己传**，漏传 `kb_cells` 就是空清单（`/kb/` 看不见东西，不报错不串号）；
- 生命周期由 `main.py` 的 lifespan 管理：`async with GeneralAgent() as agent: app.state.agent = agent`，
  启动即建表（`setup()` 幂等）并预热连接池；缺模型配置时不启动 agent，相关端点返回 503（auth 照常）。

```python
from agents import GeneralAgent

async with GeneralAgent() as agent:
    # 跑一轮：返回 AgentRun（answer / interrupt）
    run = await agent.ainvoke("记住：我的代号是夜枭", thread_id="chat-1", user_id="u1")
    if run.interrupt:                       # 被拦下等人批准
        run = await agent.ainvoke(
            thread_id="chat-1", user_id="u1",
            resume={"decisions": [{"type": "approve"}]},
        )
    print(run.answer)

    # 流式：token → 增量文本，tool_call → 工具名，interrupt → 待批请求，done → 完整回答
    async for event in agent.astream("我的代号是什么？", thread_id="chat-1", user_id="u1"):
        if event.kind == "token":
            print(event.text, end="")

    # 记忆读写（user_id 必须来自登录态）
    await agent.memory.awrite_memory("u1", "prefs.md", "喜欢简短回答")
```

方法一览：

| 方法 | 说明 |
| --- | --- |
| `ainvoke(message, *, thread_id, user_id, kb_cells?, resume?)` | 跑一轮，返回 `AgentRun{answer, interrupt}`（state 自动落检查点） |
| `astream(message, *, thread_id, user_id, kb_cells?, resume?)` | 异步迭代 `AgentEvent`：`token` / `tool_call` / `interrupt` / `done` |
| `astop_turn(turn)` | 取消在跑的那一轮并收尾（`/chat/stop` 用，见 `docs/adr/0004`~`0006`） |
| `agent.memory.aget_state / alist_memories / awrite_memory / adelete_memory` | 短期记忆概况 / 长期记忆读写 |
| `store` | langgraph store 实例（知识库 REST 与删库清内容用；未启动时为 `None`） |

HTTP 层的入口全部带鉴权与归属校验，见 [第 7 节](#7-接口)（`/chat/*`、`/memories/*`、`/kb/files/*`、`/microservices/*`）。

系统提示词在 `agents/prompt/*.md`（`system.md` 基础人格 + `kb.md` 知识库用法），
由 `agents/prompt.load_system_prompt()` 用 langchain 的 `PromptTemplate.from_file` 加载拼接——
**文件里不要写花括号**（f-string 模板会把它当变量）。

模型走 OpenAI 兼容端点，所以只装 `langchain-openai` 一个包，换厂商只改 `.env`：

```bash
OPEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 百炼的「兼容 OpenAI」地址
OPEN_MODEL=qwen3-max                                              # 想用 deepseek：deepseek-chat
OPEN_API_KEY=sk-...
```

> 智能体相关表由 langgraph 的 `setup()` 自己创建/升级（带 *_migrations 版本表），
> 不归 alembic 管：`migrations/env.py` 里的 `include_object` 已把它们排除，
> 否则 `alembic check` 会认为这些表是「多余的表」并想删掉。
