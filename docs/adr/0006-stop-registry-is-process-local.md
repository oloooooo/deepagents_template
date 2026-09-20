# 停止的注册表放在进程内存里

`/chat/stop` 要能找到"正在跑的那一轮"才能取消它，而 `ChatService` 是每请求一个实例，
`StreamingResponse` 返回后路由就撒手了 —— 所以需要一个 `thread_id -> Turn` 的注册表。
它放在 `GeneralAgent.turns` 上（跟着 agent 的生命周期走，测试里每建一个 agent 就是一份干净的表），
**是进程内存里的 dict，不是数据库里的取消标志**。

**Consequences**：`/chat/stop` **只在单进程有效**。多 worker / 多实例部署时，按停止的请求
可能落到没有那一轮的进程上。失败模式比“停不掉”糟得多：

```
进程 A：/chat/stream  →  reserve("t1")，正在跑，检查点 next=('model',)
进程 B：/chat/stop    →  turns.get("t1") → None（B 的表里没有）
                      →  但 astop 看到 next 非空，就写了一份收尾
进程 A：还在跑，还会继续写检查点 → 把收尾盖掉，或两条写互相打架
```

用户拿到 **200**，以为停了，其实没停，历史还被写脏了。所以水平扩容前**必须**先解决它，
而不是“反正停不掉也无所谓”。换实现时注意选一个不会写坏历史的方案，并从两个方向都验一遍。

顺带一提：现在的区分依据是 ``interrupts``。卡在等人批准时 ``next`` 停在
``HumanInTheLoopMiddleware.after_model``（也是非空），但 ``interrupts`` 有东西；
换成跨进程方案时别把这个区分丢了 —— 那个分支本来就没有 task 可取消，收尾是必须做的。

### 同一条天花板也压在 409 互斥上

`ChatService.open_turn` 里“一个会话同时只允许一轮在跑”靠的是同一个 `turns.reserve`，
所以它**也只在单进程有效**：两个 worker 可以同时给同一个 `thread_id` 各跑一轮，
而 langgraph 对同一 thread 的并发写会互相覆盖（`checkpoints` 按
`thread_id + checkpoint_ns + checkpoint_id` 写，同一个超步两条写谁赢不确定）。
所以“一个会话最多一轮在跑”这句话，单进程下是硬约束，多进程下不是。

三条出路，按登价比排：

1. **粘性路由**：让 LB 把同一会话的请求都送到同一个实例。`thread_id` 在 body 里 LB 看不到，
   所以要么改成路径参数（仅对 `/chat/stop` 有用，`/chat/stream` 的还是在 body），
   要么让 SSE 响应带回一个 `X-Instance-Id`、前端在 `/chat/stop` 时原样回传，LB 按这个头 hash；
2. **数据库里的取消标志**：agent 侧在节点边界轮询。要改图，且引入轮询延迟；
3. **就单 worker 跑**（现在）。

这是有意的取舍：内存注册表约 30 行、零依赖、零延迟；后两者都要改图或改部署。
现在就是单进程 uvicorn，先按最短可行做。
