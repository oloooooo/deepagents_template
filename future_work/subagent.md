# 子 agent 与 store / `/public/` 挂载的关系（待办）

**现状：项目里还没有用子 agent。** 这份记录是为了将来真要用时不必重新查一遍——
下面的结论是实测出来的，而且**依赖 deepagents 的内部实现**，升级后要重新验证。

- 验证版本：`deepagents 0.7.13`
- 结论日期：2026-09-20
- 复现脚本：`/tmp` 下的一次性 probe（没提交）；要长期钉住请写成测试，见文末

---

## 一、结论：子 agent 默认**共享**父 agent 的 backend 与 `rt.context`

不是"可以共享"，是"必然共享"——`create_deep_agent` 在整张图里只建一个 backend，然后交给
每个子 agent 的 `FilesystemMiddleware`：

| 位置 | 代码 |
|---|---|
| `deepagents/graph.py:637` | `backend = backend if backend is not None else StateBackend()` —— **只建一次** |
| `deepagents/graph.py:694` | `FilesystemMiddleware(backend=backend, ...)` —— 自定义子 agent |
| `deepagents/graph.py:799` | `FilesystemMiddleware(backend=backend, ...)` —— 自动加的 `general-purpose` |
| `deepagents/graph.py:964` | `create_agent(..., store=store)` —— store 也是整张图级的 |
| `deepagents/middleware/subagents.py:786-793` | 只塞 `{"configurable": {"ls_agent_type": "subagent"}}`，父 config 由 langgraph 的 `ensure_config` 逐键合并下去 |

所以子 agent 里 `get_runtime().context` **就是父 agent 那个 `AgentContext`**。

### 实测（在子 agent 内部直接调 `PublicMountBackend`）

```
===== 子 agent 内（alice 只被授权 pa）=====
  ls('/')           -> ['/pa/']
  read('/pa/a.md')  -> OK
  read('/pb/b.md')  -> 文件 '/pb/b.md' 不存在      ← 越权被挡

===== 子 agent 内（super 全部可见）=====
  ls('/')           -> ['/pa/', '/pb/']
  read('/pb/b.md')  -> OK
```

另一组：子 agent 往 `/memories/` 写文件，落进了 `("alice", "filesystem")` —— 命名空间工厂
读到了正确的 `user_id`。

### 共享与不共享

| | 子 agent |
|---|---|
| `backend` 实例（含 `CompositeBackend` 的 `/memories/`、`/public/` 路由） | **共享同一个实例** |
| `Runtime.context`（`AgentContext`） | **共享同一份** |
| `store` / `checkpointer` | 共享（整张图级） |
| `permissions` | 不写就**继承父的**（`spec.get("permissions", permissions)`）；写了则**整体替换** |
| 消息历史 / 上下文窗口 | **隔离**（这正是子 agent 的意义） |
| `AsyncSubAgent`（跑在远程 Agent Protocol server 上） | **不共享** —— 另一个进程、另一张图 |

注意最后几行的区别：**上下文隔离是消息层面的，不是存储层面的**。子 agent 和父 agent 读写同一份
store，所以子 agent 写坏了 `/memories/`，父 agent 下一轮立刻看得见。

---

## 二、三个坑（将来用子 agent 前先看这里）

### 1. 不带 context 时，炸点在子 agent 内部

实测：`graph.invoke(...)` 不传 `context=` 时，父 agent 自己那轮可能没事，等 `task` 把活派下去才
`AttributeError: 'NoneType' object has no attribute 'user_id'`。报错位置离调用点很远，不好排查。

`GeneralAgent.ainvoke` / `astream` 已经在内部固定传好了 `AgentContext`（含
`public_workspaces`），但**新增调用路径（子图、后台任务、直接 `graph.ainvoke`）必须自己传**。
这一条 `agents/readme.md` 第三节也写了。

### 2. `backend=` 是图级别的，没有 per-subagent 参数

想让某个子 agent 用**不同**的后端，只有两个口子：

- 在 spec 的 `middleware` 里塞一个 `FilesystemMiddleware(backend=...)` —— `_apply_custom_middleware`
  （`deepagents/graph.py:204-229`）按 `.name` 把它替换掉默认那个；
- 用 `CompiledSubAgent`：runnable 是你自己编的，后端当然也由你定。

### 3. `permissions` 写了就是整体替换，不是叠加 ← **最需要小心的一条**

子 agent 一旦给了自己的 `permissions`，父 agent 的规则**全部失效**。也就是说：

> 将来给某个子 agent 配 `permissions` 时，**必须把 `PUBLIC_PERMISSIONS` 一起带上**，
> 否则那个子 agent 就能往 `/public/` 里写 —— 而 `/public/` 对 agent 只读是
> `docs/adr/0003` 明确定下的不变量。

同理，`/memories/**` 的 `interrupt` 规则（`MEMORY_PERMISSIONS`）也会一起丢。

---

## 三、待办

1. **真要用子 agent 时，先补一条测试**：现在"子 agent 继承挂载与只读规则"是**隐式**行为，
   deepagents 改版会静默失效。可以照 `tests/test_public_workspace_mount.py` 的假模型套路写：
   让主 agent 用 `task` 派活，子 agent 内部 `ls /public/` + 读越权文件 + 尝试 `write_file`，
   断言前两者受可见范围约束、第三者被 deny。
2. **配子 agent 的 `permissions` 时，检查是否带上了 `PUBLIC_PERMISSIONS` 与 `MEMORY_PERMISSIONS`**
   （见坑 3）。如果这种重复开始变多，就抽一个 `subagent_permissions()` 帮助函数，
   而不是每处手抄一遍。
3. **升级 deepagents 后重新验证第一节的源码位置**。`graph.py` / `subagents.py` 的行号是
   0.7.13 的，不保证长期有效；`ensure_config` 逐键合并那条注释引用的是 langgraph#7926，
   属于会变的行为。
