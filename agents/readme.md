# agents 模块约定

`agents/agent.py` 里的 `GeneralAgent` / `AgentMemory` 只负责「跑图 + 存取记忆」，
**鉴权与 HTTP 一律不管**。这份文档写清记忆的隔离边界，免得后面加路由时把它写漏。

## 一、记忆分三层，各自的 user 来源

| 层 | 落点 | user_id / workspace_id 谁给 | 强度 |
|---|---|---|---|
| 短期记忆 | `checkpoints` 表，thread_id = `{user_id}:{thread_id}` | 服务端，`_conversation_id()` 拼接 | 硬：前端传 `bob:t1` 也会被拼成 `alice:bob:t1`，拿不到别人的会话 |
| 长期记忆 | store 命名空间 `(user_id, workspace_id, "filesystem")` | 服务端，`AgentContext` → `StoreBackend(namespace=...)` | 硬：模型改不了命名空间，写 `/memories/别人的名字/x.md` 也只落在自己空间 |
| 记忆读写 API | `AgentMemory` 的四个方法 | **调用方传参** | 靠纪律：路由层只能传 `CurrentUser.id`，不许从 body / query 取 |

长期记忆的路径与命名空间是同一件事的两种写法：

```
工具/用户眼里的路径   /memories/<任意目录>/x.md
store 里实际存的      namespace = (user_id, workspace_id, "filesystem")
                     key       = /<任意目录>/x.md      # /memories/ 前缀被 CompositeBackend 剥掉
等价的目录树          /memories/{user_id}/{workspace_id}/<任意目录>/x.md
```

## 二、谁能写长期记忆

| 写方 | 通道 | 约束 |
|---|---|---|
| agent（模型） | 文件工具 `write_file` / `edit_file` / `delete` | `MEMORY_PERMISSIONS` 让 `/memories/**` 与裸 `/memories` 的 write 走 `interrupt`：**必须人工批准才落库**（`run.interrupt` → `resume={"decisions": [...]}`） |
| 用户 | `AgentMemory.awrite_memory` / `adelete_memory` | 直写 store，不经模型、不需批准 |

两条规矩：

1. **`/memories/**` 的规则必须同时写 `"/memories"`**：裸路径匹配不上 `**`，实测能绕过去写到路由根。
2. **写入 API 的 `user_id` / `workspace_id` 只能来自登录态**（`dependencies.auth.CurrentUser`）
   加 `UserWorkspace.permission` 校验（`viewer` 只读）。存储层保证「写不进别人的库」，
   唯一会漏的是「把别人的 id 当自己的 id 用」——这一条只在路由层能防。

用户对短期记忆目前只有读（`aget_state`）。要支持「清空/删除自己的会话」，
把 `AsyncPostgresSaver` 传进 `AgentMemory` 后调 `adelete_thread(conversation_id)` 即可，
别忘了 `conversation_id` 同样要用 `{user_id}:{thread_id}` 拼。

## 三、跑起来要带 context

`AgentContext`（`user_id` 必填 + `workspace_id` 默认虚拟的 `default` 空间 + `public_workspaces`）
是长期记忆命名空间与 `/public/` 可见范围的唯一来源：

- `ainvoke` / `astream` 内部已经传好了；**新增调用路径（子图、后台任务、直接 `graph.ainvoke`）必须自己传**；
- `public_workspaces`（名字 → id）**每轮重新给**，包括 `/chat/approve` 续跑那一轮 —— 不落 metadata；
  漏传就是空字典，模型在 `/public/` 下什么都看不到（不是报错，也不会串号）；
- 忘了传 `user_id` 时命名空间工厂会 `AttributeError: 'NoneType' object has no attribute 'user_id'`
  —— 报错难看但不会静默串号；
- `AgentMemory.aget_state` 不走后端，可以不传 context。

## 四、公共空间挂载 `/public/`（只读）

`/public/` 和 `/memories/` 是**两个方向相反**的挂载，别混：

| | `/memories/` | `/public/` |
|---|---|---|
| 命名空间 | `(user_id, workspace_id, "filesystem")` | `("public", public_workspace_id, "filesystem")` |
| 谁看得见 | 只有本人 | 被授权的成员 + super |
| agent 能写吗 | 能，但走 `interrupt` 等人批准 | **不能**，静态 `deny` 挡掉所有 write |
| 内容谁改 | 用户自己（`/memories/write`） | 只有 super（`/public-workspaces/files/write`） |

agent 看到的路径是 `/public/{公共空间名}/x.md`，**名字不是 id**（模型读 uuid 没意义，
所以公共空间名不可变，见 `docs/adr/0002`）。

实现上的三个要点：

1. **路由静态、可见范围动态**：`CompositeBackend.routes` 在 `create_deep_agent` 时定死，
   所以 `/public/` 只挂一个 `PublicMountBackend`；它每次操作都去读 `rt.context.public_workspaces`，
   不查库（backend 是同步的，拿不到数据库 session）。所以**每轮都要重新填** `AgentContext.public_workspaces`
   （`dependencies/public_workspace.py`），不写 checkpoint metadata（`docs/adr/0004`）。
2. **`permissions=` 也是静态的**，区分不了用户，所以 `/public/**` 对**所有人**只读（`PUBLIC_PERMISSIONS`）。
   super 的写权限不在这里表达，走 REST（`docs/adr/0003`）。
3. **写方法仍然返回错误**（`PublicMountBackend.DENIED`）：静态规则在 middleware 层，
   这里是第二道防线 —— 规则写漏时宁可直接报错，也不能静默写进共享空间。

删公共空间要清空它的命名空间，顺序是**先删表再清内容**，清理失败只记日志（`docs/adr/0005`）。

## 五、停止一轮（用户的动作，不是 agent 的）

与「中断」严格区分（见 `CONTEXT.md`）：**中断**是 agent 主动停下来等人批准、之后可以续跑；
**停止**是用户按了暂停键、这一轮到此为止。两条路不要混。

实现分两块：

- `GeneralAgent.turns`（`TurnRegistry`）—— 本进程正在跑的轮次，`thread_id -> Turn`。
  `Turn.text` 攒的是**已流出的增量文本**；
- `AgentMemory.astop(thread_id, user_id, workspace_id, text=...)` —— 取消之后把检查点收尾。

三个容易踩的点：

1. **已流出的文本不落检查点。** AI 消息只在超步结束时才写，所以停止时必须靠服务层实时攒的
   `Turn.text` 写回去，否则用户已经看到的字在历史里根本不存在。收到 `tool_call` 时要把
   攒的清零，只保留“正在生成的那一段”（否则一轮里多次工具往返会把几段回答粘成一条）。
2. **不收尾 = 下一轮出错。** 停在模型生成中途时，检查点里只有一条没有回答的 human 消息，
   下一轮的新消息会和它**合并成一次请求**。收尾用 `aupdate_state(..., as_node="model")`
   （让 langgraph 从 model 重新路由，没有 tool_calls 就走到终点），再 `ainvoke(None)` 排空
   剩下的节点 —— 正等人批准时 `next` 停在 `HumanInTheLoopMiddleware.after_model`，
   上一步只清掉了待批准请求。取舍见 `docs/adr/0007`。
3. **注册表是进程内的。** 多 worker / 多实例时 `/chat/stop` 找不到别的进程里的轮次，
   水平扩容前必须换实现，见 `docs/adr/0006`。

另外：一个会话同时只允许一轮在跑（`ChatService.open_turn` 里同步占位，重复 409），
所以“停哪一轮”永远无歧义；流的迭代跑在 Starlette 的**子任务**里，
所以 `Turn.task` 在 `_tracked` 第一次迭代时会被换成子任务 —— 取消子任务只结束这条流，
取消请求任务是把整个 HTTP 请求连根拔掉。

## 六、验证脚本

| 文件 | 覆盖 | 依赖 |
|---|---|---|
| `tests/test_agent_memory_scope.py` | 跨用户隔离、`deny` 只读、`interrupt → 批准 → 落库` | 纯内存，无 DB / 无模型 key |
| `tests/test_agent_chat_memory.py` | 真图 + 真检查点：拦下、批准落库、换用户/换空间看不见、用户侧直写、短期隔离、流式 interrupt 事件 | 本机 PostgreSQL 的 `agents` 库，假模型，无模型 key |
| `tests/test_public_workspace_mount.py` | `/public/` 挂载：成员只看得到自己被授权的、同一公共空间的两人读到同一份、super 全部可见、`glob`/`grep` 合并且不越权、写被 deny、假模型端到端 | 纯内存，无 DB / 无模型 key |
| `tests/test_chat_stop.py` | 停止：四种入口的收尾、已流出文本写回历史、续聊不合并、幂等、409 互斥、404 鉴权 | 本机 PostgreSQL 的 `agents` 库，假模型，无模型 key |

## 七、还没做

1. `/memories/*`、`/chat/*` 路由与 service（鉴权落点见第二节）；
2. `conversations(thread_id, user_id, workspace_id)` 归属表 —— 现在 thread_id 只是拼字符串，建议显式登记 + 每次请求校验；
3. 批准闭环的前端部分：把 `run.interrupt["action_requests"]` 展示出来，收集 decisions 再 `resume`。
