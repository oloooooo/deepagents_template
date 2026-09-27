# 子 agent 与 store / `/kb/` 挂载的关系（待办）

**现状：项目里还没有用子 agent。** 这份记录是为了将来真要用时不必重新查一遍——
下面的结论是实测出来的，而且**依赖 deepagents 的内部实现**，升级后要重新验证。

- 验证版本：`deepagents 0.7.13`
- 结论日期：2026-09-20（实测时挂载还是已删除的 `/public/`；现体系是 `/kb/`，
  挂载在 `CompositeBackend` 里的位置与读 `rt.context` 的方式同构，结论应当照样成立，
  但**尚未对 `/kb/` 重新实测** —— 见文末待办 1）
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

### 当时的实测（在子 agent 内部直接调挂载 backend）

2026-09-20 在旧的 `/public/` 挂载上量到的行为，换成 `/kb/` 后的预期形状（**待复现**，见待办 1）：

```
===== 子 agent 内（alice 是 order-svc 成员，不是 pay-svc 成员）=====
  ls('/')                     -> ['/order-svc/']
  read('/order-svc/shared/概念.md') -> OK
  read('/pay-svc/shared/支付.md')    -> 文件不存在          ← 越权被挡
  write('/order-svc/shared/x.md')    -> 只读拒绝（KB_PERMISSIONS 静态 deny）

===== 子 agent 内（super：kb_cells 是全部微服务）=====
  ls('/')                     -> ['/order-svc/', '/pay-svc/']
```

另一组（当时已实测、且 `/memories/` 至今没变）：子 agent 往 `/memories/` 写文件，
落进了 `("alice", "filesystem")` —— 命名空间工厂读到了正确的 `user_id`。

### 共享与不共享

| | 子 agent |
|---|---|
| `backend` 实例（含 `CompositeBackend` 的 `/memories/`、`/kb/` 路由） | **共享同一个实例** |
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
`kb_cells`），但**新增调用路径（子图、后台任务、直接 `graph.ainvoke`）必须自己传**。
这一条 `agents/readme.md` 第三节也写了。

### 2. `backend=` 是图级别的，没有 per-subagent 参数

想让某个子 agent 用**不同**的后端，只有两个口子：

- 在 spec 的 `middleware` 里塞一个 `FilesystemMiddleware(backend=...)` —— `_apply_custom_middleware`
  （`deepagents/graph.py:204-229`）按 `.name` 把它替换掉默认那个；
- 用 `CompiledSubAgent`：runnable 是你自己编的，后端当然也由你定。

### 3. `permissions` 写了就是整体替换，不是叠加 ← **最需要小心的一条**

子 agent 一旦给了自己的 `permissions`，父 agent 的规则**全部失效**。也就是说：

> 将来给某个子 agent 配 `permissions` 时，**必须把 `KB_PERMISSIONS` 一起带上**，
> 否则那个子 agent 就能往 `/kb/` 里写 —— 而 `/kb/` 对 agent 只读是
> `docs/adr/0003` 明确定下的不变量。

同理，`/memories/**` 的 `interrupt` 规则（`MEMORY_PERMISSIONS`）也会一起丢。

---

## 三、待办

1. **真要用子 agent 时，先补一条测试**：现在“子 agent 继承挂载与只读规则”是**隐式**行为，
   deepagents 改版会静默失效。可以照 `tests/test_kb_mount.py` 的假模型套路写：
   让主 agent 用 `task` 派活，子 agent 内部 `ls /kb/` + 读越权格子 + 尝试 `write_file`，
   断言前两者受 `kb_cells` 可见范围约束、第三者被 deny —— 这同时把第一节的预期输出也复现了。
2. **配子 agent 的 `permissions` 时，检查是否带上了 `KB_PERMISSIONS` 与 `MEMORY_PERMISSIONS`**
   （见坑 3）。如果这种重复开始变多，就抽一个 `subagent_permissions()` 帮助函数，
   而不是每处手抄一遍。
3. **升级 deepagents 后重新验证第一节的源码位置**。`graph.py` / `subagents.py` 的行号是
   0.7.13 的，不保证长期有效；`ensure_config` 逐键合并那条注释引用的是 langgraph#7926，
   属于会变的行为。
