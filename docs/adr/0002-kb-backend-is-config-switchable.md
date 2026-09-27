# 知识库 backend 按配置切换（store / local），S3 预留不实现

知识库内容存哪由 `config.yaml` 决定：全局默认 `kb.backend`（`store` = postgres langgraph store，
与记忆同一套库；`local` = 磁盘目录，`kb.local_root` 指根），并允许 `kb.overrides` 按微服务名
单独覆盖——由于空间与微服务一一对应（`docs/adr/0001`），「按微服务配」和「按空间配」是同一件事。
S3 在枚举里**预留但不实现**：boto3 依赖先不装，真有远端存储需求时补一个 backend 实现即可。

选两个实现而不是一个：`store` 让知识库跟着应用已有的 postgres 走（免运维、随应用备份），
`local` 让文档落在磁盘上人可以直接看、直接搬（便宜直观）。粒度到微服务的覆盖是为了
不同服务的存储约束不同——比如要频繁给人交接的文档落 local，其余走 store。

**Considered Options**：

- 现在就实现 S3——枚举留位、实现留白，依赖与测试成本推迟到需求出现时；
- backend 存进数据库按空间配——配置是部署决策不是业务数据，放 `config.yaml`，
  改配置重启即生效，不给配置写 CRUD；
- 只实现一种——两种介质的真实取舍存在（见上），且工厂一层分支的代价很小。

**Consequences**：

- 换 backend 是运维动作（改配置重启），**内容不自动迁移**，要搬自己搬；
- 挂载层（`agents/kb/`）与 REST 写入（`services/kb.py`）经**同一个工厂**取 backend，
  两边看到的是同一份内容；新增 backend 实现只需进工厂，两处自动生效。
