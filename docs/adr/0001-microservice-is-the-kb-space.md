# 微服务就是知识库空间

上一版体系把「空间」拆成两类实体（`Workspace` 三级权限 + `PublicWorkspace` 二元只读），
两套实现大量重复，最终在 `16c3cf9` 整体移除。这次重建只留**一张 `microservices` 表**：
空间与微服务**一一对应**——创建 / 删除知识库空间就是创建 / 删除微服务，没有独立的空间实体。

权限也收敛成**一个维度**：`user_microservices` 二元成员关系（是 / 不是成员），没有 permission 列。
用户对知识库一律只读，写只归 super（经 REST，见 `docs/adr/0003`）。将来每个微服务要挂 N 个
MCP / SKILL，挂点就是 `microservices.id`——先不建关联表，第一个 MCP 进来时按实际字段再建。

**Considered Options**：

- 独立 `kb_spaces` 表挂 `microservice_id`（1:1 起步、可扩一个微服务多个空间）——
  为「空间生命周期独立于微服务」服务，但我们没有这个需求，白付一张表 + 一套仓储 + 一次 join；
- `microservices` 加 `kb_enabled` 开关 + 清空动作——同上，且把「开 / 关知识库」变成了一个
  没有调用方的状态位；
- 保留旧的三级权限（admin / editor / viewer）——v1 除 super 外人人只读，等级没有可判定的行为差异。

**Consequences**：

- 删微服务 = 删知识库：内容级联清理（先删表记录、再清存储，清理失败只记日志）；
- 「能用这个服务但看不到它的知识库」的场景**不存在**——成员关系即权限；
  真要分成两个维度时得加一张关系表，改的是鉴权入口一处；
- 未来加 MCP / SKILL 注册表时，外键指向 `microservices.id`。
