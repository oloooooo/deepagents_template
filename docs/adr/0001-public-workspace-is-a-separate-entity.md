# 公共空间是独立实体，不复用 Workspace

`Workspace` + `UserWorkspace` 已经是「空间 + 多对多 + 级联删除 + 权限列」，形状和公共空间一模一样，
所以本可以加一个 `visibility` 标志位复用。我们决定新建 `PublicWorkspace` + `UserPublicWorkspace`，
因为权限模型不同：Workspace 是三级（admin / editor / viewer），公共空间是二元的（成员只读，
super 读写删）。复用会让 `WorkspaceAccess.permission()` 返回一个对私有空间无意义的权限值，
而那个入口被 chat / memory / workspace 三处共用，blast radius 远大于新建两张小表。

**Considered Options**：复用 `Workspace` + `visibility` 列（少两张表，但污染共用的鉴权入口，
也会把公共空间混进 `/workspaces/mine`）。
