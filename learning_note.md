# 让 agent「看见」用户的所有空间：原理与实现

这篇讲两件事，其实是同一件事：

1. **记忆树** —— agent 在 `/memories/{业务空间名}/...` 下看得见这个人参与的每个空间；
2. **公共空间** —— agent 在 `/public/{公共空间名}/...` 下看得见他有权看的每个公共空间（super 是全部）。

两个挂载点背后的机制**完全一样**，共用 `agents/fanout.py::FanoutMountBackend`。名字不同、命名空间不同、
可见范围不同、可写规则不同 —— 这四件事是**参数**，不是两套代码。

---

## 一、问题的形状

agent 眼里只有一棵虚拟文件树（由 `CompositeBackend` 按前缀路由）；存储侧是 langgraph store 的
**一格一个命名空间**：

```
agent 眼里的路径                       store 里的落点
/memories/default/notes/a.md    →     namespace ("u1", "default", "filesystem")   key "/notes/a.md"
/memories/proj-a/notes/a.md     →     namespace ("u1", "wsa",     "filesystem")   key "/notes/a.md"
/public/proj-1/readme.md        →     namespace ("public", "pw1", "filesystem")   key "/readme.md"
```

要求是：**同一个人能看到哪几格，是每轮现算的**（他今天被加进 proj-b，今天就得看得见；被移出 proj-a，
今天就看不见）。而且模型要能自己 `ls` 出这份清单、按需 `read` 其中任意一格。

---

## 二、三条拦路的约束（这是原理的核心）

### 约束 1：路由是启动时定死的

```python
backend=CompositeBackend(default=StateBackend(), routes={MEMORY_ROUTE: ..., PUBLIC_ROUTE: ...})
```

`routes` 的 key 是**静态前缀**，`create_deep_agent()` 建图时传进去就不变了。所以：

- ✅ 能做到：`/memories/` 这个前缀整体交给我们的 backend；
- ❌ 做不到：「这个人的 `/memories/` 下面挂哪几个子目录」—— 那时还没有请求，更没有用户。

### 约束 2：一个 `StoreBackend` 只有一个命名空间

```python
StoreBackend(namespace=lambda rt: (rt.context.user_id, "default", "filesystem"), store=store)
```

命名空间工厂能读当轮 `rt.context`（这是它比静态路由强的地方），但**它只能返回一个元组**。
如果所有用户都指向同一个命名空间，那每个人都会看见**全部**公共空间 —— 这正好是我们最不能要的。

### 约束 3：权限规则也是静态的

```python
permissions=[FilesystemPermission(operations=["write"], paths=["/memories/**"], mode="deny")]
```

`FilesystemPermission` 在编译期传入，规则里写不了「按人按轮判定」。它只能表达
「**这个前缀**上的**这类操作**一律如何」。

### 结论：中间加一层

需要一层**每次文件操作时现算「这个人现在能看哪几格」、再把请求委托给对应命名空间**的 backend。
这层就是 `agents/fanout.py::FanoutMountBackend`：

```
CompositeBackend ──(静态前缀 /memories/)──▶ FanoutMountBackend ──(按路径首段选格子)──▶ StoreBackend(每个格子一个)
                                                    ▲                                        │
                                                    └── rt.context.memory_workspaces ─────────┘ 委托给它自己算出来的命名空间
```

`FanoutMountBackend` 继承了 `StoreBackend`（满足 backend 协议），但**从不使用自己那个命名空间**：
它把每一个方法都改成「按路径选格子，再委托给这个格子自己的 `StoreBackend`」。
占位命名空间故意写成会抛异常的 `_unreachable()` —— 漏接管某个方法时**立刻炸**，
绝不静默读写一个不存在的地方。

---

## 三、数据模型：格子名 → id → 命名空间

| | `/memories/` | `/public/` |
|---|---|---|
| 格子名（agent 眼里的目录名） | `Workspace.name`（含虚拟 `default`） | `PublicWorkspace.name` |
| 格子 id（REST 与存储用的键） | `Workspace.id` | `PublicWorkspace.id` |
| 命名空间 | `(user_id, 空间 id, "filesystem")` | `("public", 公共空间 id, "filesystem")` |
| 可见范围函数 | `WorkspaceService.visible` = 成员关系 + 虚拟 `default` | `PublicWorkspaceService.visible` = 被授权 + super 全部 |
| 哪一格可写 | 只有 `default` | 一格都不能（改内容走 REST） |
| 上下文字段 | `AgentContext.memory_workspaces` | `AgentContext.public_workspaces` |
| 权限规则 | `MEMORY_PERMISSIONS` | `PUBLIC_PERMISSIONS` |

**为什么路径里放名字、不放 id**：模型要自己决定读哪个目录，uuid 对它是纯噪音（`docs/adr/0002`）。
代价是这个名字成了地址的一部分：

- 公共空间 → 名字**不可变**（改名等于重建），`docs/adr/0002`；
- 业务空间 → 名字**可改**（记忆按 id 存，改名只让旧会话里的字符串失效），`docs/adr/0009`。

两次结论相反是有意的，对着读。

映射本身由构造参数里的回调负责，一次调用算一次：

```python
# 记忆：context 提供 user_id，id 来自清单
namespace=lambda context, workspace_id: _memory_ns(context.user_id, workspace_id)
# 公共空间：命名空间里不含 user_id
namespace=lambda _context, workspace_id: public_namespace(workspace_id)
```

---

## 四、可见范围从哪来（每轮重算）

```
HTTP 请求
  ├─ /chat/send /chat/stream /chat/approve
  │     依赖注入：MemoryWorkspaceDep  → WorkspaceService.visible(user)      → 名字→id
  │                PublicWorkspaceDep → PublicWorkspaceService.visible(user) → 名字→id
  │     往下传：router → ChatService → GeneralAgent.ainvoke(memory_workspaces=..., public_workspaces=...)
  │
  └─ 图执行期：AgentContext(user_id, workspace_id, public_workspaces, memory_workspaces)
                   │
                   └─ backend 每次操作：get_runtime().context → _allowed() → {名字: id}
```

三条设计决定：

1. **不落 checkpoint metadata**：metadata 在会话开始时定死，而「被移出空间」必须立刻生效，
   包括 `/chat/approve` 续跑的那一轮。所以清单只活在当轮 context 里（`docs/adr/0004`）。
2. **拿不到清单就什么都不给看**（`_allowed()` 返回空字典）。这是 fail-closed：忘了传不是"看得更多"，
   而是"一格都看不见"。新增调用路径（子图、后台任务、直接 `graph.ainvoke`）必须自己传。
3. **清单与 REST 同源**：`GET /memories/all` 用的就是 `WorkspaceService.visible`，
   所以「用户在接口里列得出来的」和「agent 看得见的」永远是同一份。

---

## 五、分发逻辑（读）

路径首段一律当**格子名**，其余是**格子内路径**（`_split`；挂载根本身返回 `None`）：

| 操作 | 行为 |
|---|---|
| `ls("/")` | **不查 store**：直接用清单里的名字合成目录项。所以空空间也列得出来 —— 那是地址清单，不是内容清单 |
| `ls("/{格子名}")` | 委托给该格子的 `StoreBackend.ls("/")`；看不见的格子报"不存在" |
| `read` / `write` / `edit` / `delete` | **从不扇出**：路径必须点名一个格子 |
| `glob` / `grep`（`_fanout`） | 路径落在某格 → 只搜那一格；路径是挂载根（或没给）→ 搜**全部可见格**，命中路径补回 `/{格子名}` 前缀 |

`glob`/`grep` 是唯一"一次覆盖多格"的地方，结果之所以还能指回来源，全靠补前缀。

两个已知语义，别当成 bug：

- `max_count` 是**逐格子**生效的：N 个格子最多返回 N 倍命中，`truncated` 只是"某一格截断了"。
- 看不见的格子与不存在的路径给出**同一种**结果（`read` 报"不存在"，`glob`/`grep` 空列表），
  所以没法拿它做存在性探测。

---

## 六、写策略：静态规则 + backend 兜底，两道防线

`FilesystemPermission` 的判定是**按顺序第一条命中即生效，一条都不命中 = 允许**（实测
`_check_fs_permission`）。于是规则顺序本身是语义：

```python
MEMORY_PERMISSIONS = [
    # 1) 只有 default 那一格：写要人工批准
    FilesystemPermission(operations=["write"],
        paths=["/memories/default/**", "/memories/default"], mode="interrupt"),
    # 2) 其余格子：一律拒绝（上面没命中才会走到这里）
    FilesystemPermission(operations=["write"],
        paths=["/memories/**", "/memories"], mode="deny"),
]

PUBLIC_PERMISSIONS = [
    FilesystemPermission(operations=["write"], paths=["/public/**", "/public"], mode="deny"),
]
```

细节：

- **裸路径必须单列一条**（`"/memories"`、`"/memories/default"`、`"/public"`）：不带尾斜杠的前缀
  匹配不上 `/**`，实测能绕过去写到路由根。
- **为什么记忆能表达"只写某一格"、公共空间不能表达"只有 super 能写"**：
  `default` 是**保留空间名**（真实空间不许叫它），所以 `/memories/default/**` 是个稳定无歧义的静态前缀；
  而"谁是 super"是用户维度的事，静态规则看不见（`docs/adr/0003`）—— 公共内容只能从 REST 改。
- **为什么 agent 只写 `default`**：写入口唯一，模型就不会把 A 项目的结论写进 B 项目；
  项目格子是**用户投喂的只读资料**（`/memories/write`、`/memories/upload`），`docs/adr/0010`。
- **backend 里的写方法仍然返回错误**（`denied=...`）：静态规则在 middleware 层，这里是第二道防线 ——
  规则写漏时宁可直接报错，也不能静默写进共享空间。
- **批量上传（`upload_files` / `aupload_files`）一律拒**：它是写，但走中间件的内部调用、不经文件工具，
  也就**绕开了人工批准那一层**。不能给"写记忆要批准"开这个例外。
- **批量下载（`download_files`）照 `read` 实现**：读不受写策略约束，按路径选格子即可。

---

## 七、实现上的坑（都是踩出来的，写下来省下一次）

1. **同步与异步必须成对覆盖**。`StoreBackend` 里只有 `als` / `aglob` / `agrep` 是转调同步版
   （`asyncio.to_thread(self.ls, ...)`），而 `aread` / `awrite` / `aedit` / `adelete` 是**另一套原生实现**。
   只覆盖同步方法：中间件走的异步写会直接绕开挂载层的写策略。
2. **`AsyncPostgresStore` 在主事件循环里调同步 API 直接抛 `InvalidStateError`**（langgraph 的死锁护栏）。
   所以生产的读全走 `a*`；同步版只在 `to_thread` 出来的工作线程里用（框架自己那三个包了一层）。
3. **不要用 `StoreBackend` 的批量下载**。实测它在这套壳下会把**存在**的文件报成 `file_not_found`
   （`/default/a.md` 明明在库里）。照 `read`/`aread` 自己拼一段，够用而且行为一致。
4. **缓存键必须是命名空间，不能是格子 id**。早期版本按 id 缓存委托 backend ——
   记忆的命名空间是 `(user_id, 空间 id)`，两个用户在**同一个空间**里会撞成同一个 key 而串台。
   后来干脆取消缓存：委托对象只是两个字段的壳，`StoreBackend.__init__` 就是两次赋值。
5. **`FileDownloadResponse` / `FileUploadResponse` 是 dataclass，不是 TypedDict**，
   要构造实例（`FileUploadResponse(path=..., error=...)`），不能塞 dict。
6. **占位命名空间要炸得响**：`_unreachable()` 抛 `NotImplementedError`。
   这正是第 1 条被发现的途径 —— 漏了 `aread` 时探针立刻炸，而不是安静地读了一个空命名空间。

---

## 八、走一遍完整链路（记忆树）

```
用户：「项目A 里有什么约定？」
  │
  ├─ POST /chat/send  {workspace_id: wsa}
  │     MemoryWorkspaceDep  → SELECT ... user_workspaces ─┐
  │                                                       └─ {default: default, proj-a: wsa, proj-b: wsb}
  ├─ ChatService.send → agent.ainvoke(..., memory_workspaces={...})
  │     AgentContext(user_id=u1, workspace_id=wsa, memory_workspaces=..., public_workspaces=...)
  │
  ├─ 模型调 ls("/memories/")
  │     FilesystemMiddleware → CompositeBackend（前缀命中 /memories/，剥掉）→ FanoutMountBackend.ls("/")
  │       → _split("/") = None → 挂载根 → 读 rt.context.memory_workspaces → 合成 ["/default/", "/proj-a/", "/proj-b/"]
  │
  ├─ 模型调 glob("*.md")            # 或者 grep
  │     _fanout(None) → 三格各搜一遍 → 命中路径补回 /{格子名}
  │
  └─ 模型调 read_file("/memories/proj-a/notes.md")
        aread("/proj-a/notes.md") → _backend("proj-a") → StoreBackend(namespace=("u1","wsa","filesystem"))
          → store.aget(("u1","wsa","filesystem"), "/notes.md") → 内容返回给模型
```

公共空间那条链一模一样，只是清单来自 `PublicWorkspaceService.visible`（授权 + super 全部），
命名空间是 `("public", id, "filesystem")`（不含 `user_id`，所以成员读到的是**同一份**）。

---

## 九、照这个模式再加一个挂载点

1. `FanoutMountBackend(store, context_attr=..., namespace=..., writable=..., denied=...)` 建一个实例；
2. `AgentContext` 加一个 `dict[str, str]` 字段（名字 → id）；
3. 写一个 `dependencies/xxx.py`：每轮查库、返回这份清单（`Depends`）；
4. 路由把它传进 `ChatService` → `agent.ainvoke/astream`；
5. `routes={"/新前缀/": 那个 backend}` + 写 `FilesystemPermission`（**裸路径单列一条**）；
6. 在 `tests/test_*_mount.py` 里照 `tests/test_memory_mount.py` 补一段纯内存的断言。

要小心的：新前缀别和已有的撞（`_split` 只切第一段）；新挂载点如果可写，先想清楚**谁能写、要不要批准**。

---

## 十、文件地图

| 文件 | 干什么 |
|---|---|
| `agents/fanout.py` | `FanoutMountBackend`：扇出、可见范围过滤、写策略、批量下载/上传 |
| `agents/agent.py` | `memory_mount()`（记忆挂载配置）、`MEMORY_PERMISSIONS`、`AgentContext` |
| `agents/public_workspace.py` | `PublicMountBackend`（十几行配置）、`PUBLIC_PERMISSIONS`、`PublicWorkspaceStore` |
| `dependencies/memory_workspace.py` | 每轮查「我参与的空间」 |
| `dependencies/public_workspace.py` | 每轮查「我有权看的公共空间」 |
| `services/workspace.py::visible` | 清单的来源之一（成员关系 + 虚拟 default） |
| `services/public_workspace.py::visible` | 清单的另一来源（授权 + super 全部） |
| `routers/chat.py` / `services/chat.py` | 把清单从请求带到图里 |
| `tests/test_memory_mount.py` | 纯内存验证记忆树：清单、隔离、只写 default、规则顺序、扇出、批量下载/上传、端到端 interrupt |
| `tests/test_public_workspace_mount.py` | 纯内存验证公共空间挂载：可见性、共享、只读、扇出、端到端 |
| `docs/adr/0002` `0003` `0004` `0009` `0010` | 名字进路径 / 公共只读 / 每轮重查 / 业务空间可改名 / 只写 default |

术语（`CONTEXT.md`）：**Cell（格子）** = 挂载点下的一个子目录 = 一个命名空间；
**Fan-out（扇出）** = 挂载根上的一次 `ls`/`glob`/`grep` 覆盖多格。别用"合并" —— 各格内容从不合并。
