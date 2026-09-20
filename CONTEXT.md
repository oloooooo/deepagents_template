# 领域词汇

两组容易混的词：**「空间」**（谁能看到同一份文件）和**「轮次」**（一次对话从开始到结束）。
混用会让鉴权判断和状态机判断出错，所以先把词定死。

## Language

### 空间与记忆

**Workspace（业务空间）**：
用户之间共享的私有工作区，成员按 admin / editor / viewer 三级权限访问。
_Avoid_: 工作区、team、project

**Public workspace（公共空间）**：
面向一组指定用户的共享空间，被授权的成员只读，只有 super user 能读写删。
与 Workspace 是**两类实体**，权限模型不同（二元成员关系 vs 三级权限）。
_Avoid_: 公开空间、公共工作区、shared workspace

**Default workspace（虚拟 default 空间）**：
每个登录用户自带、库里没有记录的虚拟空间，人人都是 admin。
它占用了 `default` 这个名字，真实空间不许叫它。
_Avoid_: 默认空间、个人空间、home

**Super user**：
`users.is_super = true` 的用户。只能通过数据库授权，没有任何 API 写入口。
对 Workspace 是「建 / 改 / 删 / 授权」的唯一人选。
_Avoid_: 管理员、admin —— admin 是 Workspace 内的权限等级，不是这个

**Member（成员）**：
在某个空间里有一条关联记录的用户。Workspace 的成员带三级权限；
Public workspace 的成员一律只读。
_Avoid_: 用户、参与者、subscriber

**Visibility（可见范围）**：
谁能读到某个空间的内容。Workspace 的可见范围**等于**成员关系；
Public workspace 的可见范围是「成员关系 ∪ super」—— super 不是成员也能读、能写、能删。
所以「我是不是成员」（`/mine`）和「我能读哪些」（`/list`、`/public/` 挂载）是两个不同的问题。
_Avoid_: 权限、access —— 权限是「能做什么」，可见范围是「能看到哪些」

**Memory（长期记忆）**：
跨会话保留的文件，落在 `/memories/`，按 (用户, 空间) 隔离，别人看不到。
_Avoid_: 记忆库、knowledge base、RAG

**Public mount（`/public/`）**：
agent 视角下的公共空间挂载点，形如 `/public/{公共空间名}/...`，把当前用户可访问的公共空间
各挂一个子目录。它对 agent 只读，不是存储位置，也不是 REST 路由前缀。
_Avoid_: 公共目录、public 文件夹、shared drive

### 对话轮次

**Turn（轮次）**：
从用户发出一条消息，到这一轮跑完（图到达终点）之间的整个过程。
一个轮次可以中途被中断再续跑，仍然算**同一个**轮次。
_Avoid_: 请求、对话、会话 —— 会话（thread）可以有很多轮次

**Interrupt（中断）**：
agent 在跑的过程中**主动**停下来等人批准，之后可以**续跑同一个轮次**。
触发点是文件权限规则（`MEMORY_PERMISSIONS` 的 `mode="interrupt"`），答复走 `/chat/approve`。
它是 agent 的动作，不是用户的动作。
_Avoid_: 暂停、停止、打断 —— 这几个词一律不要用来指它

**Stop（停止）**：
用户按暂停键**叫停**当前轮次：取消在跑的那一轮，把已经流出的文本写回历史，这一轮到此为止。
它是用户的动作，**不可续跑**（想接着跑就走 Interrupt / `/chat/approve`）。
停止也不回滚：工具已经写过的文件、已经落库的记忆都留着 —— 它是「到此为止」，不是「撤销」。
_Avoid_: 暂停、中断、取消 —— 暂停暗示能续跑，取消（cancel）是实现里的 asyncio 动作

**Turn running（在跑的轮次）**：
已经提交、还没跑到终点的轮次。一个会话同时最多有一个（重复提交返回 409）。
「停止」停的就是它，所以停哪一轮永远无歧义。
_Avoid_: 进行中、活跃会话
