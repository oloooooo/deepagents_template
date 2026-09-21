# agents 模块约定

`agents/` 里是 agent 本体，**鉴权与 HTTP 一律不管**：

- `agent.py` —— `GeneralAgent`（跑图）/ `AgentMemory`（存取记忆）；
- `fanout.py` —— 多命名空间挂载：`/{挂载根}/{格子名}/...`，`/memories/` 与 `/public/` 共用；
- `turns.py` —— 停止：`TurnRegistry`（本进程的 task 与已流出文本）+ `RunningTurns`（跨进程的意图与归属）；
- `public_workspace.py` —— `/public/` 只读挂载（配置 + REST 侧的 store 读写）。

这份文档写清记忆的隔离边界与停止的分工，免得后面加路由时把它写漏。

## 一、记忆分三层，各自的 user 来源

| 层 | 落点 | user_id / workspace_id 谁给 | 强度 |
|---|---|---|---|
| 短期记忆 | `checkpoints` 表，thread_id = `{user_id}:{thread_id}` | 服务端，`_conversation_id()` 拼接 | 硬：前端传 `bob:t1` 也会被拼成 `alice:bob:t1`，拿不到别人的会话 |
| 长期记忆 | store 命名空间 `(user_id, workspace_id, "filesystem")` | 服务端，`AgentContext` → `StoreBackend(namespace=...)` | 硬：模型改不了命名空间，写 `/memories/别人的名字/x.md` 也只落在自己空间 |
| 记忆读写 API | `AgentMemory` 的四个方法 | **调用方传参** | 靠纪律：路由层只能传 `CurrentUser.id`，不许从 body / query 取 |

长期记忆的路径与命名空间是同一件事的两种写法：

```
agent 眼里的路径      /memories/{业务空间名}/<任意目录>/x.md   # 名字只是格子名，不是 id
store 里实际存的      namespace = (user_id, 空间 id, "filesystem")
                     key       = /<任意目录>/x.md            # /memories/{格子名} 被挂载层剥掉
```

**一个用户有多个格子**（他自己参与的每个业务空间各一格 + 虚拟 `default`），格子清单每轮由
`dependencies/memory_workspace.py` 查出来填进 `AgentContext.memory_workspaces`（名字 -> id）。
格子的内容是只读资料（用户投喂）还是 agent 写的，取决于**哪一格** —— 见第二节。

## 二、谁能写长期记忆

| 写方 | 通道 | 约束 |
|---|---|---|
| agent（模型） | 文件工具 `write_file` / `edit_file` / `delete` | 只能写 `default` 那一格：`MEMORY_PERMISSIONS` 第一条让 `/memories/default/**` 走 `interrupt`（**必须人工批准才落库**），第二条把其余格子 `deny`。规则**按顺序第一条命中即生效**，所以 interrupt 必须排在 deny 前面 |
| 用户 | `AgentMemory.awrite_memory` / `adelete_memory`（REST：`/memories/write`、`/memories/upload`） | 直写 store，不经模型、不需批准；**任何**自己的格子都能写（`editor` 起），项目资料就是这么投喂进去的 |

两条规矩：

1. **`/memories/**` 的规则必须同时写 `"/memories"`**：裸路径匹配不上 `**`，实测能绕过去写到路由根。
   `default` 那一格同理，裸 `"/memories/default"` 也要单列一条。
2. **写入 API 的 `user_id` / `workspace_id` 只能来自登录态**（`dependencies.auth.CurrentUser`）
   加 `UserWorkspace.permission` 校验（`viewer` 只读）。存储层保证「写不进别人的库」，
   唯一会漏的是「把别人的 id 当自己的 id 用」——这一条只在路由层能防。

用户对短期记忆目前只有读（`aget_state`）。要支持「清空/删除自己的会话」，
把 `AsyncPostgresSaver` 传进 `AgentMemory` 后调 `adelete_thread(conversation_id)` 即可，
别忘了 `conversation_id` 同样要用 `{user_id}:{thread_id}` 拼。

## 三、跑起来要带 context

`AgentContext`（`user_id` 必填 + `workspace_id` 默认虚拟的 `default` 空间 + `public_workspaces` +
`memory_workspaces`）是长期记忆与两个挂载可见范围的唯一来源：

- `ainvoke` / `astream` 内部已经传好了；**新增调用路径（子图、后台任务、直接 `graph.ainvoke`）必须自己传**；
- `public_workspaces` / `memory_workspaces`（都是名字 → id）**每轮重新给**，包括 `/chat/approve`
  续跑那一轮 —— 不落 metadata（`docs/adr/0004`）；漏传就是空字典，模型在那个挂载下什么都看不到
  （不是报错，也不会串号），**记忆的读写也一并停掉**（拿不到清单就没格子可写）；
- 忘了传 `user_id` 时命名空间工厂会 `AttributeError: 'NoneType' object has no attribute 'user_id'`
  —— 报错难看但不会静默串号；
- `AgentMemory.aget_state` 不走后端，可以不传 context。

## 四、两个挂载：`/memories/` 与 `/public/`

两者结构一样（`/{挂载根}/{格子名}/...`，一个格子一个命名空间），方向相反，别混：

| | `/memories/` | `/public/` |
|---|---|---|
| 格子名 | 业务空间名（含虚拟 `default`） | 公共空间名 |
| 命名空间 | `(user_id, 空间 id, "filesystem")` | `("public", 公共空间 id, "filesystem")` |
| 谁看得见 | 只有本人 | 被授权的成员 + super |
| agent 能写吗 | **只有 `default` 那一格**，且要走 `interrupt` 等人批准 | **哪一格都不能**，静态 `deny` 挡掉所有 write |
| 格子里的内容谁改 | 用户自己（`/memories/write`、`/memories/upload`） | 只有 super（`/public-workspaces/files/write`） |
| 格子清单从哪来 | `dependencies/memory_workspace.py`（成员关系 + default） | `dependencies/public_workspace.py`（授权 + super 全部） |

agent 看到的路径是 `/memories/{业务空间名}/x.md` 与 `/public/{公共空间名}/x.md`，**名字不是 id**。
两类实体的名字策略不同：公共空间名**不可变**（改名等于重建，`docs/adr/0002`），业务空间名**可改**
（记忆内容按 id 存，改名只让旧会话里的字符串失效，`docs/adr/0009`）；agent 自己永远改不了名字。

`/memories/` 变成格子树之后，**agent 写记忆永远写 `default`**（在哪个空间聊都一样），项目格子只由
用户投喂 —— 取舍见 `docs/adr/0010`。

实现上的四个要点：

1. **路由静态、可见范围动态**：`CompositeBackend.routes` 在 `create_deep_agent` 时定死，
   所以每个挂载根只挂一个 `FanoutMountBackend`；它每次操作都去读 `rt.context`（
   `public_workspaces` / `memory_workspaces`），不查库（backend 是同步的，拿不到数据库 session），
   所以**每轮都要重新填**（`dependencies/*`），不写 checkpoint metadata（`docs/adr/0004`）。
2. **`permissions=` 也是静态的**，区分不了用户，所以 `/public/**` 对**所有人**只读（`PUBLIC_PERMISSIONS`）；
   super 的写权限不在这里表达，走 REST（`docs/adr/0003`）。记忆那边能用静态规则表达“只有 default 可写”，
   仅仅因为 `default` 是**保留空间名**（真实空间不许叫它），那一格是稳定无歧义的静态前缀。
3. **同步与异步必须成对覆盖**：`StoreBackend` 里 `als` / `aglob` / `agrep` 是转调同步版，
   而 `aread` / `awrite` / `aedit` / `adelete` 是**另一套原生实现** —— 只覆盖同步方法，
   中间件走的异步写就会绕开挂载层的写策略。没接管的方法会落到一个会抛异常的占位命名空间上（宁可直接炸）。
   批量下载（`download_files` / `adownload_files`）也接管了：按路径选格子、照 `read` 自己拼
   （中间件要的是**原始内容**，`read` 返回的文本带行号它用不了）；**批量上传一律拒** ——
   它是写，却绕开文件工具那层的人工批准，不能给“写记忆要批准”开例外。
4. **写方法仍然返回错误**（`PublicMountBackend` / `memory_mount` 的 `denied`）：静态规则在 middleware 层，
   这里是第二道防线 —— 规则写漏时宁可直接报错，也不能静默写进不该写的地方。

删公共空间要清空它的命名空间，顺序是**先删表再清内容**，清理失败只记日志（`docs/adr/0005`）。

## 五、停止一轮（用户的动作，不是 agent 的）

与「中断」严格区分（见 `CONTEXT.md`）：**中断**是 agent 主动停下来等人批准、之后可以续跑；
**停止**是用户按了暂停键、这一轮到此为止。两条路不要混。

实现分两块（见 `docs/adr/0006` / `0008`）：

- `running_turns` 表 + `chat_drain` 通道（`agents/turns.py`）—— **跨进程的意图与归属**：
  谁在跑、要不要停、结果是什么。投递走 Postgres 的 `LISTEN/NOTIFY`，所以停止请求
  落到哪个 worker 都行，**不需要负载均衡器做粘性**；表的 PK 兼任跨进程的 409 互斥。
- `GeneralAgent.turns`（`TurnRegistry`）—— **本进程的执行能力**：那个 `Task` 和已流出的文本。
  这两个都进不了数据库，所以它不能被“优化掉”（见 `CONTEXT.md` 的 Turn owner）。

三个容易踩的点：

1. **已流出的文本不落检查点。** AI 消息只在超步结束时才写，所以停止时必须靠服务层实时攒的
   `Turn.text` 写回去，否则用户已经看到的字在历史里根本不存在。收到 `tool_call` 时要把
   攒的清零，只保留“正在生成的那一段”（否则一轮里多次工具往返会把几段回答粘成一条）。
   跨进程时这份文本也得从 owner 那里回传 —— 走表的 `answer` 列，**不走通知**
   （NOTIFY payload 上限 7999 字节，实测 8000 就报错）。
2. **不收尾 = 下一轮出错。** 停在模型生成中途时，检查点里只有一条没有回答的 human 消息，
   下一轮的新消息会和它**合并成一次请求**。收尾用 `aupdate_state(..., as_node="model")`
   （让 langgraph 从 model 重新路由，没有 tool_calls 就走到终点），再 `ainvoke(None)` 排空
   剩下的节点 —— 正等人批准时 `next` 停在 `HumanInTheLoopMiddleware.after_model`，
   上一步只清掉了待批准请求。取舍见 `docs/adr/0007`。
   这个坑单进程也有：owner 跑到一半被杀死，那个会话就停在那里了 —— 所以
   `ChatService.open_turn` 每次开轮次前先看一眼检查点（“孤儿恢复”）。
3. **两条 `LISTEN` 相关的约束。** 通知是**即发即弃**的（不在监听时发出的会丢），
   所以重连后要补扫一遍表；`LISTEN` 是**连接级状态**，不能借连接池里的连接，
   也不能和 `notifies()` 共用一条连接（它会阻塞住）。

另外：`Turn.task` 在 `ChatService._tracked` 第一次迭代时会被换成**子任务** ——
流的迭代跑在 Starlette 的子任务里，取消子任务只结束这条流，取消请求任务是把整个 HTTP
请求连根拔掉。

另外：一个会话同时只允许一轮在跑（`ChatService.open_turn` 里同步占位，重复 409），
所以“停哪一轮”永远无歧义；流的迭代跑在 Starlette 的**子任务**里，
所以 `Turn.task` 在 `_tracked` 第一次迭代时会被换成子任务 —— 取消子任务只结束这条流，
取消请求任务是把整个 HTTP 请求连根拔掉。

## 六、验证脚本

| 文件 | 覆盖 | 依赖 |
|---|---|---|
| `tests/test_agent_memory_scope.py` | 跨用户隔离、`deny` 只读、`interrupt → 批准 → 落库` | 纯内存，无 DB / 无模型 key |
| `tests/test_memory_mount.py` | `/memories/` 挂载：格子清单只列可见的、跨用户隔离、**只有 default 能写**、静态规则顺序（default=interrupt 其余=deny）、根路径 `glob`/`grep` 扇出、假模型端到端（含 interrupt → 批准 → 落库） | 纯内存，无 DB / 无模型 key |
| `tests/test_agent_chat_memory.py` | 真图 + 真检查点：拦下、批准落库（**落 default**）、换用户/换空间看不见、用户侧直写、短期隔离、流式 interrupt 事件 | 本机 PostgreSQL 的 `agents` 库，假模型，无模型 key |
| `tests/test_public_workspace_mount.py` | `/public/` 挂载：成员只看得到自己被授权的、同一公共空间的两人读到同一份、super 全部可见、`glob`/`grep` 扇出且不越权、写被 deny、假模型端到端 | 纯内存，无 DB / 无模型 key |
| `tests/test_chat_stop.py` | 停止：四种入口的收尾、已流出文本写回历史、续聊不合并、幂等、409 互斥、404 鉴权 | 本机 PostgreSQL 的 `agents` 库，假模型，无模型 key |
| `tests/test_chat_stop_cross.py` | 跨进程停止：两个 agent 实例 = 两个 worker | 同上 |

## 七、还没做

1. `/memories/*`、`/chat/*` 路由与 service（鉴权落点见第二节）；
2. `conversations(thread_id, user_id, workspace_id)` 归属表 —— 现在 thread_id 只是拼字符串，建议显式登记 + 每次请求校验；
3. 批准闭环的前端部分：把 `run.interrupt["action_requests"]` 展示出来，收集 decisions 再 `resume`。
