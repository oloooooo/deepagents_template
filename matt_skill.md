# Matt Pocock Skills 速查

来源：`~/.pi/agent/skills/`（Matt Pocock 工程技能集，入口是 `/ask-matt`）。
每个技能的**作用**与**应用场景**如下，按 ask-matt 的地图分组。

## 前置

| 技能 | 作用 | 应用场景 |
|---|---|---|
| `setup-matt-pocock-skills` | 一次性配置本仓库：issue tracker（GitHub/GitLab/本地 markdown）、triage 标签词表、领域文档布局（CONTEXT.md + ADR） | 第一次使用其它工程技能**之前**运行一次；换新仓库时再跑一次 |
| `ask-matt` | 技能路由器：告诉你当前 situation 该走哪条 flow | 不确定该用哪个技能时，问它 |

## 主流程：idea → ship

| 技能 | 作用 | 应用场景 |
|---|---|---|
| `grill-with-docs` | 通过 relentless 追问打磨想法，并把结论写进 `CONTEXT.md` 和 ADR（有状态） | 在一个工作目录里有了新想法/方案，要把它磨清楚且留下文档。主流程起点 |
| `to-spec` | 把当前对话直接综合成一份 spec，发布到 issue tracker（无 interview，只做综合） | 讨论已充分，要把它固化成可执行 spec；多会话构建的第一步 |
| `to-tickets` | 把 plan/spec/对话拆成一组 tracer-bullet tickets，声明 blocking edges，发到 tracker | spec 要跨多个会话实现，拆票后逐票 `/implement` |
| `implement` | 按 spec 或 tickets 实施工作：内部驱动 `/tdd`，收尾跑 `/code-review` 再提交 | 单会话直接实现，或逐票实现（每票之间 `/clear`） |
| `tdd` | 红-绿-重构的测试驱动开发循环，一次一个行为切片 | 想 test-first 地构建一个具体行为，不需要完整 spec 时 |
| `code-review` | 双轴评审 diff：Standards（是否符合仓库编码规范）+ Spec（是否符合原始 issue/spec），两个子代理并行 | 评审某个 commit/分支/PR/进行中的改动，"review since X" |
| `handoff` | 把当前对话压缩成一份可移植的 handoff 文档 | 换 harness、换目录、交给同事、或中途分叉侧任务时 |

**流程用法**：`grill-with-docs` → （需要跑起来才能回答的问题：`handoff` 出去 → `prototype` → `handoff` 回来）→ 多会话则 `to-spec` → `to-tickets` → 逐票 `implement`；单会话直接 `implement`。步骤 1–3 保持在同一个上下文窗口（注意 smart zone，约 150k tokens，到边界就 `/compact`）。

## 入口匝道（On-ramps）

| 技能 | 作用 | 应用场景 |
|---|---|---|
| `triage` | 让 issue/外部 PR 走五态状态机（needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix），分类、验证、必要时 grill，写出 agent-ready 的 brief | 别人报的 bug、外来功能请求堆积时。**不要**triage 自己 `/to-tickets` 产出的票 |
| `diagnosing-bugs` | 难 bug 的诊断循环：先建立 tight feedback loop（一条命令在本 bug 上变红）才允许推理，修复必须带回归测试 | 一眼看不出的 bug、间歇性 flake、两个已知好状态之间的回归。产出可能交棒给 `improve-codebase-architecture` |
| `wayfinder` | 把超大、模糊的工程规划成 tracker 上一张"决策 ticket 地图"，逐个解决，产出**决策而非交付物**；清雾后交给 `to-spec` 收敛 | 绿色项目或超大功能，一个会话装不下、路线尚不可见时。最烧脑的流程，别用于已界定清晰的功能 |

## 代码库健康

| 技能 | 作用 | 应用场景 |
|---|---|---|
| `improve-codebase-architecture` | 扫描代码库找 deepening opportunities，输出可视化 HTML 报告，然后对选中的一个进行 grill | 有空维护代码库、让它对 agent 更好用时；选中的机会可以带进主流程 `/grill-with-docs` |

## 词汇层（在其它技能之下运行）

| 技能 | 作用 | 应用场景 |
|---|---|---|
| `domain-modeling` | 打磨领域语言：挑战模糊术语、解决一词多义、把难以逆转的决定记成 ADR | 讨论文档术语、编写/修改 `CONTEXT.md`、记录 ADR 时。是 `grill-with-docs` 底层的学科 |
| `codebase-design` | deep module 共享词汇：module / interface / depth / seam / adapter / leverage / locality | 设计或改进模块接口、找 seam、让代码更可测/对 agent 更可导航时。`tdd` 和 `improve-codebase-architecture` 都讲这套词汇 |

## 独立技能（不在主流程上）

| 技能 | 作用 | 应用场景 |
|---|---|---|
| `grilling` | 追问原语本身：一轮轮 interview，agent 负责事实、你负责决策，画出 design tree | 想要不带包装的 interview；`grill-me`/`grill-with-docs` 是两个入口，`triage`/`wayfinder`/`improve-codebase-architecture` 内部也用它 |
| `grill-me` | 与 `grill-with-docs` 相同的 relentless interview，但**无状态**，不写任何文件 | **不在工作目录里**时（打磨一个计划、设计、文章）。在仓库里请用 `grill-with-docs` |
| `prototype` | 写一个一次性小程序回答一个设计问题；原型留在 `prototype/<name>` 分支上作为 primary source | 状态模型/UI/业务逻辑"纸上谈不定"，需要跑起来看时；主流程第 2 步的岔路 |
| `research` | 起一个**后台 agent** 对照一手资料做调研，产出带引用的 Markdown 存进仓库 | 需要文档/API 事实的读物调研，可边干别的边等它；产出带进 `/grill-with-docs` |
| `to-questionnaire` | 把你答不了的问题写成一份问卷发给别人填 | 卡住你的信息在**别人**脑子里时；是 `grill-me` 的反向——先 interview 你"要问谁、要什么"，回来的材料喂给 `grill-with-docs` 或 `to-spec` |
| `wizard` | 生成交互式 bash 向导，引导人类完成只有人能做的步骤，每步开 URL、取值、写进 `.env`/secrets | 开基础设施、配凭证/CI secrets、点陌生第三方后台、一次性迁移切换。agent 自己能做的不要用它 |
| `wait-what` | 纠正没听懂的消息：让 agent 用 CONTEXT.md 词汇、丢掉你缺失的上下文重新表述一遍 | 对话中途你觉得"没讲明白"时；`grill-with-docs` 是事前预防，`wait-what` 是事后纠正 |
| `teach` | 用当前目录做有状态工作区，跨多个会话学习一个概念/技能 | 想系统学某个主题，需要跨会话留存进度 |
| `writing-for-agents` | 写给 agent 看的文档的参考：skills、AGENTS.md/CLAUDE.md、被指针引用的文档该怎么写 | 创建/修改 skill，或改 AGENTS.md/CLAUDE.md 时 |
| `resolving-merge-conflicts` | 逐 hunk 解决进行中的 merge/rebase 冲突：按两侧的**意图**（追到各自 primary source）解，而不是挑行；从不 `--abort` | 已经在冲突中间时。独立于所有流程 |

## 阶段边界（Phase boundaries）

两段工作之间五选一：**Continue**（默认先排除它）、`/clear`（上下文无关时）、`/handoff`（换 harness/目录/同事/中途分叉）、**Subagent**（紧耦合的旁支任务）、`/compact`（树底的默认兜底）。详见 `ask-matt/PHASE-BOUNDARIES.md`。
