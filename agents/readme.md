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

`AgentContext`（`user_id` + `workspace_id`，两个都必填）是长期记忆命名空间的唯一来源：

- `ainvoke` / `astream` 内部已经传好了；**新增调用路径（子图、后台任务、直接 `graph.ainvoke`）必须自己传**；
- 忘了传时命名空间工厂会 `AttributeError: 'NoneType' object has no attribute 'user_id'` —— 报错难看但不会静默串号；
- `AgentMemory.aget_state` 不走后端，可以不传 context。

## 四、验证脚本

| 文件 | 覆盖 | 依赖 |
|---|---|---|
| `tests/test_agent_memory_scope.py` | 跨用户隔离、`deny` 只读、`interrupt → 批准 → 落库` | 纯内存，无 DB / 无模型 key |
| `tests/test_agent_chat_memory.py` | 真图 + 真检查点：拦下、批准落库、换用户/换空间看不见、用户侧直写、短期隔离、流式 interrupt 事件 | 本机 PostgreSQL 的 `agents` 库，假模型，无模型 key |

## 五、还没做

1. `/memories/*`、`/chat/*` 路由与 service（鉴权落点见第二节）；
2. `conversations(thread_id, user_id, workspace_id)` 归属表 —— 现在 thread_id 只是拼字符串，建议显式登记 + 每次请求校验；
3. 批准闭环的前端部分：把 `run.interrupt["action_requests"]` 展示出来，收集 decisions 再 `resume`。
