# 公共空间的可见范围每轮重查，不写进 checkpoint metadata

私有空间的 `workspace_id` 是写进 checkpoint metadata 的（`_run_config` 的 `metadata`，会话归属靠它）。
公共空间**故意不这么做**：一个用户可以关联 N 个公共空间，被移出任何一个都必须**立刻**失效，
而 metadata 是会话开始时定死的 —— 那样被移除的成员能继续读完整个会话。

所以 `public_workspaces` 由 dependency 每轮查一次（含 `/chat/approve` 续跑），只活在当轮的
`AgentContext` 里，不落库到 checkpoint。代价是会话中途被移出时 `/public/` 里的目录会当场消失 ——
这正是想要的行为。

**Consequences**：`workspace_id` 和 `public_workspaces` 的传递方式不一样，这是有意的，别为了"一致"
把后者也塞进 metadata。
