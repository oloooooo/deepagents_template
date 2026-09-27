# Ponytail Skills 速查

来源：`@dietrichgebert/ponytail`（npm 包，位于 `~/.pi/agent/npm/node_modules/@dietrichgebert/ponytail/skills/`）。
核心哲学：**只做能工作的最懒方案** —— YAGNI → 复用现有代码 → 标准库 → 原生平台特性 → 已装依赖 → 一行能写就不写五十行。

## 技能列表

| 技能 | 作用 | 应用场景 |
|---|---|---|
| `ponytail` | **核心技能（常驻模式）**。强制最懒但真正能工作的方案：先问任务是否该存在（YAGNI），优先标准库而非自写、原生特性而非依赖、一行而非五十行。带一个"阶梯"（ladder）规则逐级判断，非平凡逻辑要求留下一个最小可运行检查（assert 自检或一个小 test）。强度分三级：`lite` / `full`（默认）/ `ultra` | **任何编码任务**：写、加功能、重构、修 bug、评审、设计、选库。或用户说 "ponytail" / "be lazy" / "simplest solution" / "yagni" / 抱怨过度工程时。关闭用 "stop ponytail" / "normal mode" |
| `ponytail-review` | **Diff 级过度工程评审**。只找可删的东西：重复造标准库、多余依赖、投机抽象、死灵活性。每条一行：位置、删什么、用什么替代 | 用户说 "review for over-engineering"、"what can we delete"、"is this over-engineered"、"/ponytail-review"。与正确性评审互补，只管复杂度 |
| `ponytail-audit` | **全仓过度工程审计**，与 `ponytail-review` 同思路但扫描整个代码库而非 diff，产出按优先级排序的删除/简化/替换清单。一次性报告，**不改代码** | 用户说 "audit this codebase"、"find bloat"、"what can I delete from this repo"、"/ponytail-audit" |
| `ponytail-debt` | 把代码库里所有 `ponytail:` 注释（ponytail 留下的故意捷径/延期标记）收割成一份**债务台账**，防止 "later" 变成 "never"。一次性报告，不改代码 | 用户说 "ponytail debt"、"list the shortcuts"、"what did ponytail defer"、"/ponytail-debt" |
| `ponytail-gain` | 以紧凑记分板展示 ponytail 的实测影响：更少代码、更低成本、更快速度（来自 benchmark 中位数）。一次性展示，**不是** per-repo 数字，也不是常驻模式 | 用户说 "/ponytail-gain"、"ponytail gain"、"show ponytail impact"、"ponytail scoreboard" |
| `ponytail-help` | 所有 ponytail 模式、技能、命令的**速查卡**。一次性展示 | 用户说 "/ponytail-help"、"ponytail help"、"how do I use ponytail"、"what ponytail commands" |

## 使用要点

- `ponytail` 是唯一常驻模式（跟随对话持续生效），其余 5 个都是一次性命令（one-shot），显示完即止。
- 强度切换：`/ponytail lite|full|ultra`；关闭：`stop ponytail` / `normal mode`，会话内持续有效直到再次更改。
- 不适用于非编码请求（闲聊、翻译、菜谱等）。
- 故意留下的捷径会带 `# ponytail: <天花板> + 升级路径` 注释 —— 想盘点它们就用 `ponytail-debt`。
