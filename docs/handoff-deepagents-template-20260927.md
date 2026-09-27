# Handoff — deepagents_template 知识库体系重建

生成时间：会话结束于 commit `ca25936`（已推送到 `origin/feature_workspace`）
仓库：`E:\code_\deepagents_template`（分支 `feature_workspace`）

## 本次会话做了什么

三个阶段，全部完成：

1. **Grilling 设计访谈**（14 问）：把「按工作空间划分的知识库」需求打磨成定案。
   决策结论不在本文重复——见 `CONTEXT.md`（领域词汇）与 `docs/adr/0001~0006`（决策与取舍）。
   关键脉络：空间 = 微服务一一对应（ADR 0001）、权限二元一维、backend 可切换（0002）、
   知识库对 agent 恒只读（0003）、ADR 重排（停止机制三篇由旧 0006~0008 → 新 0004~0006）。
2. **七步实现**（用户逐步确认的计划，全部落地）：models + 迁移 `b4e7f1a92c35` →
   config `kb:` 段 → `agents/kb/storage.py` backend 工厂 → 管理面 routers/services →
   `/kb/` 挂载 + `kb_cells` 每轮注入 + prompt 拆文件 → readme + 3 个新测试。
3. **收尾**：根 `README.md` 全文重写（719 行）；方案 A 清理存量
   （删死路由 `routers/agent.py`、改写 `future_work/subagent.md` 的 `/public/` 引用）；
   提交并推送。

## 当前状态

- **11 个测试脚本全绿**（提交前逐个验证过）：`tests/` 下全部是自清理脚本，
  跑法 `uv run python tests/<name>.py`，**没有 pytest**（依赖里也没装）。
- 数据库：`alembic upgrade head` 已在开发库执行；旧 workspace 四张表已随迁移清除。
- 工作区干净，仅剩两个用户个人笔记未跟踪：`matt_skill.md`、`pony_skill.md`（故意不提交）。
- 此前发现的存量问题已全部清零（HEAD 曾因 `agents/__init__.py` 引用改名前的 `DeepAgent`
  import 即炸，已修；死代码 `services/agent.py`、`tests/test_agent.py` 已删）。

## 关键文件地图（细节看文件本身，勿在此复述）

| 要了解什么 | 看哪里 |
|---|---|
| 领域词汇 / 术语边界 | `CONTEXT.md` |
| 架构决策与取舍 | `docs/adr/0001~0006`（编号已重排，旧 0006~0008 内容并入 0004~0006） |
| agent 模块约定、挂载边界、停止机制 | `agents/readme.md` |
| 全部 HTTP 接口、配置项、测试清单 | `README.md` |
| 分层与路由约定（项目强制规范） | `AGENTS.md` |
| 知识库存储/挂载/权限实现 | `agents/kb/{storage,mount,permissions}.py` |
| 可见范围每轮注入 | `dependencies/kb.py` → `AgentContext.kb_cells` |
| 本次提交全貌 | `git show ca25936`（59 文件，+3021/−1250） |

## 已明确的待办（用户尚未决定做）

见 `agents/readme.md` 第八节「还没做」，按优先级：

1. MCP / SKILL 注册表（现仅 `agents/mcp/`、`agents/skills/` 占位包；挂点 `microservices.id`，
   约定是等第一个真实接入再按实际字段建表）；
2. 成员写自己 private 层的解锁（v1 全员只读，写只归 super）；
3. s3 backend（config 枚举已预留，取到即 `NotImplementedError`；实现需 `boto3`——
   **按 AGENTS.md 规定要先用 ask_user_question 问用户再装依赖**）；
4. xlsx 解析工具（当前二进制只存取不解析）；
5. 会话归属显式表（thread_id 目前只是拼字符串）。

## 环境与操作注意

- **测试/日志输出是 GBK 控制台**：中文会乱码，跑测试请加 `PYTHONIOENCODING=utf-8`，
  判断结果看退出码与末行「全部通过」，别信控制台里的中文编码。
- Windows 跑 uvicorn 的 loop 参数坑、生产连接数公式等都在 `README.md` §4/§6.3，别重新推导。
- 敏感项：数据库与模型密钥在 `.env`（gitignore）；**任何文档都不要回填其内容**。
  模型配置读 `OPEN_MODEL` / `OPEN_BASE_URL` / `OPEN_API_KEY`，yaml 的 `deepagent` 段当前未接线。
- 知识库 backend 切换 = 改 `config/config.yaml` 的 `kb.` 段后重启，**内容不迁移**（ADR 0002）。

## Suggested skills

下一个 agent 按任务调用 Skill 工具：

- **继续设计/需求讨论**（任何新功能先对齐）→ `grilling`；涉及新术语或改 `CONTEXT.md` → 同时用 `domain-modeling`
- **动挂载 / backend / subagent 相关代码** → `deep-agents-core`（先读，本项目深度依赖 deepagents 内部实现，
  `future_work/subagent.md` 记录了 0.7.13 的实测结论与待复现项）
- **改图、state、中断/恢复逻辑** → `langgraph-fundamentals` + `langgraph-human-in-the-loop`
- **写/改 agent 提示词与中间件行为** → `langchain-fundamentals`、`langchain-middleware`
- **新表与迁移** → `AGENTS.md` 第 5 节流程（README §5 有完整命令），无对应 skill
- **代码审查（尤其防过度工程）** → `ponytail-review` 或 `code-review`
