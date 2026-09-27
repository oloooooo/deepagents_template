# agents 模块约定

`agents/` 里是 agent 本体，**鉴权与 HTTP 一律不管**（管理面在 `models/repositories/routers/services`，见 `AGENTS.md`）：

- `agent.py` —— `GeneralAgent`（跑图）/ `AgentMemory`（存取记忆）；
- `turns.py` —— 停止：`TurnRegistry`（本进程的 task 与已流出文本）+ `RunningTurns`（跨进程的意图与归属）；
- `kb/` —— 知识库：`storage.py`（backend 工厂，store/local 可切换）、`mount.py`（`/kb/` 挂载）、`permissions.py`（静态只读）；
- `prompt/` —— 系统提示词 `.md`（`system.md` + `kb.md`），用 langchain 的 `PromptTemplate.from_file` 加载；
- `mcp/`、`skills/` —— 空包占位，注册表等第一个 MCP/Skill 进来再建（挂点是 `microservices.id`）。

这份文档写清**记忆与知识库的隔离边界**、**context 的每轮注入**与**停止的分工**，免得后面加路由时把它写漏。
领域词汇见根目录 `CONTEXT.md`，决策见 `docs/adr/`。

## 一、记忆分三层，各自的 user 来源

| 层 | 落点 | user_id 谁给 | 强度 |
|---|---|---|---|
| 短期记忆 | `checkpoints` 表，thread_id = `{user_id}:{thread_id}` | 服务端，`_conversation_id()` 拼接 | 硬：前端传 `bob:t1` 也会被拼成 `alice:bob:t1` |
| 长期记忆 | store 命名空间 `(user_id, "filesystem")` | 服务端，`AgentContext` → `StoreBackend(namespace=...)` | 硬：模型改不了命名空间 |
| 记忆读写 API | `AgentMemory` 的四个方法 | **调用方传参** | 靠纪律：路由层只能传 `CurrentUser.id` |

长期记忆**一人一份、不分格子**：`/memories/` 下就是这个人的根，用来存个人偏好。

## 二、谁能写长期记忆

| 写方 | 通道 | 约束 |
|---|---|---|
| agent（模型） | 文件工具 `write_file` / `edit_file` / `delete` | 只能写 `/memories/**`：`MEMORY_PERMISSIONS` 第一条让 `/memories/**` 与裸 `/memories` 走 `interrupt`（**必须人工批准才落库**）；**知识库 `/kb/**` 是另一条规则，直接 `deny`**（见第四节）。规则**按顺序第一条命中即生效** |
| 用户 | `AgentMemory.awrite_memory` / `adelete_memory`（REST：`/memories/write`、`/memories/upload`） | 直写 store，不经模型、不需批准 |

两条规矩：

1. **裸路径必须单列**：`"/memories/**"` 匹配不上 `/memories`，`"/kb/**"` 同理——不单列就能绕过去写到挂载根，实测过。
2. **写入 API 的 `user_id` 只能来自登录态**（`dependencies.auth.CurrentUser`）。存储层保证「写不进别人的库」，唯一会漏的是「把别人的 id 当自己的 id 用」——这一条只在路由层能防。

## 三、跑起来要带 context

`AgentContext`（`user_id` 必填 + `kb_cells` 默认空）是两个挂载可见范围的唯一来源：

- `ainvoke` / `astream` 内部已经传好了；**新增调用路径（子图、后台任务、直接 `graph.ainvoke`）必须自己传**；
- `kb_cells`（微服务名 → id，见 `dependencies/kb.py`）**每轮重新给**，包括 `/chat/approve` 续跑那一轮——**不落 checkpoint metadata**（成员关系会变，落了就是过期清单）；漏传 = 空字典 = `/kb/` 下什么都看不见（不是报错，也不串号）；
- 忘了传 `user_id` 时命名空间工厂会 `AttributeError`——报错难看但不会静默串号；
- `AgentMemory.aget_state` 不走后端，可以不传 context。

## 四、两个挂载：`/memories/` 与 `/kb/`

结构一样（一个挂载根，按名字寻址），方向不同，别混：

| | `/memories/` | `/kb/` |
|---|---|---|
| 格子 | 没有格子，一人一份根 | 一格一微服务：`/kb/{微服务名}/{shared\|private}/...` |
| 命名空间 / 目录 | `(user_id, "filesystem")` | store：`("kb", 微服务 id, "shared")`、`("kb", 微服务 id, "private", 用户 id)`；local：`{local_root}/{微服务名}/{层}/...` |
| 谁看得见 | 只有本人 | shared = 成员 ∪ super；private = 本人（super 经 REST 带 `account`） |
| agent 能写吗 | 只写 `/memories/**`，且必须 `interrupt` 人工批准 | **一概不能**：`KB_PERMISSIONS` 静态 `deny` |
| 内容谁改 | 用户自己（`/memories/write`、`/memories/upload`） | super（`/kb/files/write`、`/upload`、`/delete`）；用户 v1 连 REST 写都没有 |
| 可见清单从哪来 | 不需要（user_id 就是全部） | `AgentContext.kb_cells`，`dependencies/kb.py` 每轮查库（super 全部 / 成员关系） |

实现上的四个要点：

1. **路由静态、可见范围动态**：`CompositeBackend.routes` 在 `create_deep_agent` 时定死，`/kb/` 只挂一个 `KbMountBackend`；它每次操作读 `rt.context.kb_cells` 过滤格子——backend 是同步的，拿不到数据库 session，所以**清单每轮由路由层查好传进来**，不写 metadata。
2. **`KB_PERMISSIONS` 是静态的，区分不了用户**——但知识库对所有人（含 super 的 agent 会话）都只读，一条 `deny` 就是完整表达；super 的写权限走 REST（`docs/adr/0003`）。对比 `/memories/` 的 `interrupt`：那边有「agent 自己的地盘」，知识库没有。
3. **同步与异步成对覆盖**：中间件异步写走 `awrite`/`aedit`/`adelete` 原生实现，不覆盖就绕开写策略；store 的同步读在事件循环线程里会 `run_coroutine_threadsafe` 卡死自己，所以挂载的异步方法一律委托 cell 的异步原生实现。批量上传一律拒（写，且绕开规则），批量下载放行（读）。
4. **写方法仍然返回错误**：静态 deny 在 middleware 层，挂载层的 `DENIED` 是第二道防线——规则写漏时宁可直接报错，也不能静默写进不该写的地方。

**backend 可切换（`docs/adr/0002`）**：store / local 由 `config.yaml` 的 `kb.backend` 全局决定、`kb.overrides` 按微服务名覆盖，`s3` 枚举预留未实现；挂载与 REST 共用 `kb/storage.py` 的同一个 `cell_backend` 工厂，换介质两边同时生效。删微服务**先删表记录再清内容**（`storage.purge` 把 store 前缀与磁盘目录都清，失败只记日志，`docs/adr/0001`）。

## 五、停止一轮（用户的动作，不是 agent 的）

与「中断」严格区分（见 `CONTEXT.md`）：**中断**是 agent 主动停下来等人批准、之后可以续跑；**停止**是用户按了暂停键、这一轮到此为止。两条路不要混。

实现分两块（见 `docs/adr/0004` / `0005` / `0006`）：

- `running_turns` 表 + `chat_drain` 通道（`agents/turns.py`）—— **跨进程的意图与归属**：谁在跑、要不要停、结果是什么。投递走 Postgres 的 `LISTEN/NOTIFY`，停止请求落到哪个 worker 都行，**不需要负载均衡器做粘性**；表的 PK 兼任跨进程的 409 互斥。
- `GeneralAgent.turns`（`TurnRegistry`）—— **本进程的执行能力**：那个 `Task` 和已流出的文本。这两个都进不了数据库，不能被「优化掉」。

三个容易踩的点：

1. **已流出的文本不落检查点。** AI 消息只在超步结束时才写，停止时必须靠服务层实时攒的 `Turn.text` 写回去；收到 `tool_call` 时清零，只保留正在生成的那一段。跨进程时文本走表的 `answer` 列，**不走通知**（NOTIFY payload 上限 7999 字节）。
2. **不收尾 = 下一轮出错。** 收尾用 `aupdate_state(..., as_node="model")` 让 langgraph 从 model 重新路由，再 `ainvoke(None)` 排空（`docs/adr/0005`）。owner 挂掉留下的孤儿轮次由 `ChatService.open_turn` 开轮前补做收尾。
3. **两条 `LISTEN` 约束。** 通知即发即弃，重连后要补扫表；`LISTEN` 是连接级状态，不能借连接池，也不能和 `notifies()` 共用一条连接。

## 六、提示词

系统提示词在 `agents/prompt/*.md`，由 `load_system_prompt()` 用 langchain 的 `PromptTemplate.from_file` 加载、按顺序拼接：

- `system.md` —— 基础人格与记忆约定；
- `kb.md` —— `/kb/` 挂载的用法（只读、两层、二进制走 download）。

**文件里不要出现花括号**（f-string 模板会把它当变量解析），占位用 `<...>`。加新的挂载点就加一个 `.md` 并登记进 `_PARTS`。

## 七、验证脚本

| 文件 | 覆盖 | 依赖 |
|---|---|---|
| `tests/test_agent_memory_scope.py` | 跨用户隔离、`deny` 只读、`interrupt → 批准 → 落库` | 纯内存，无 DB / 无模型 key |
| `tests/test_memory_mount.py` | `/memories/` 挂载：跨用户隔离、静态规则顺序、根路径扇出、假模型端到端 | 纯内存 |
| `tests/test_kb_mount.py` | `/kb/` 挂载：可见清单、两层结构、private 按人隔离、写全拒（同步/异步/批量）、静态 deny 不触发 interrupt、扇出不越权、假模型端到端 | 纯内存 |
| `tests/test_kb_backend.py` | backend 切换：全局/覆盖/s3 报错、local 落盘读删与穿越防护、purge 双介质、挂载跟着配置走 | 临时目录 + InMemoryStore |
| `tests/test_microservice_api.py` | `/microservices/*` 与 `/kb/files/*`：建删授权幂等、成员只读可见范围、private `account` 越权 404、二进制 422/download、删库清内容 | 本机 PostgreSQL + 假模型 |
| `tests/test_agent_api.py` | `/memories/*` 与 `/chat/*` 契约、批准闭环、SSE、短期编辑 | 本机 PostgreSQL + 假模型 |
| `tests/test_agent_chat_memory.py` | 真图 + 真检查点：拦截批准落库、换用户看不见、流式 interrupt | 本机 PostgreSQL + 假模型 |
| `tests/test_chat_stop.py` / `_cross.py` / `_cross_http.py` | 停止：四入口收尾、文本写回、409 互斥、跨进程 | 本机 PostgreSQL + 假模型 |

## 八、还没做

1. **MCP / SKILL 注册**：`agents/mcp/`、`agents/skills/` 只是占位包，注册表等第一个真实接入按实际字段建（挂点 `microservices.id`）；
2. **成员写自己的 private**：v1 连 super 之外的 REST 写都没有（`docs/adr/0003`），要解锁 owner 直写时在 `/kb/files/*` 加成员写规则 + 挂载层不放行（agent 仍只读）；
3. **s3 backend**：枚举已预留，实现时补 `cell_backend` 分支 + boto3 依赖（先问用户装不装）；
4. **xlsx 解析**：agent 读二进制只会得到 base64（Q9=a），要「帮我总结这张表」再加工具；
5. `conversations(thread_id, user_id)` 归属表 —— 现在 thread_id 只是拼字符串，建议显式登记 + 每次请求校验；
6. 批准闭环的前端部分：把 `run.interrupt["action_requests"]` 展示出来，收集 decisions 再 `resume`。
