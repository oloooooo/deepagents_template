# `/public/` 对 agent 只读，公共内容的写入口只有 REST API

`FilesystemPermission` 是编译期定死的（`create_deep_agent` 时传入），规则里区分不了用户：
`deny` 会把 super 一起挡掉，`interrupt` 也没法只对 super 生效。所以「只有 super 能写 `/public/**`」
如果交给 agent 来做，就必须额外写一个自定义 backend 去读 `rt.context.is_super` 做动态判定。

我们决定 v1 不做：`/public/` 对 agent 完全只读（一条静态 `deny` 规则挡掉所有 `write` 类工具），
super 想改公共内容走 `/public-workspaces/files/write/{id}`。等真出现「super 要 agent 代笔」的需求，
再上自定义 backend —— 那时它由需求支撑，不是预防性的。

**Consequences**：模型无法帮 super 归档公共内容，只能读。`/public/` 仍然需要一个自定义 backend，
但只是为了按用户过滤「我能看到哪几个公共空间」（只读），不是为了写权限 —— 别把这两件事混起来。
