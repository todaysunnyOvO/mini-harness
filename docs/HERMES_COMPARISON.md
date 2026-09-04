# Mini Harness 与 Hermes Agent 对照

## 1. 两者的共同骨架

```text
稳定 Context
→ 模型返回文本或 Tool Call
→ Harness 校验、授权并执行
→ Tool Result 回到模型
→ 自然结束、受控停止或恢复
```

Mini Harness 不是 Hermes 的裁剪运行版，而是把最关键语义重新实现成一个可读、可注入故障的小项目。

## 2. 总体差异

| 维度 | Mini Harness | Hermes |
|---|---|---|
| Provider | 一个非流式 OpenAI-compatible Chat Completions Transport | 多 Provider、Chat/Responses/Anthropic/Codex 模式、流式、凭证池和 fallback |
| Agent Loop | 同步循环、工具顺序执行；内置文件 Handler 可在 Spawn 子进程运行 | 共享迭代预算、并发工具、Gateway/TUI/Desktop、子 Agent、steer 和多种中断路径 |
| 工具面 | 静态 Registry；文件工具与配置门控 Memory 工具 | 自动发现、Toolset、`check_fn`、Plugin、MCP、平台和服务门控 |
| Context | 完整长期历史 + Profile Memory/Skill 索引冻结快照 + 预算 API 副本 | 项目规则、Memory、Skills 索引、平台 Context、压缩和 Provider 专用适配 |
| 持久化 | SQLite SessionDB + Profile-scoped Markdown Memory | Session 分支、压缩、用量、平台路由、FTS、后台任务等完整状态系统 |
| 可观察性 | 脱敏 JSONL + 恢复事件 | `agent.log`、`errors.log`、`gateway.log`、回调、Hook 和工具生命周期事件 |

## 3. 预防层

Mini Harness：

- Registry 就是允许执行的全集；
- Draft 2020-12 JSON Schema 在 Handler 之前校验；
- 文件路径必须留在 Workspace；
- `write_file` 必须审批；
- 工具名只能在已授权集合内保守修复。

Hermes 在此基础上还有：

- Toolset 决定本轮工具集合；
- `check_fn` 让未配置的服务工具根本不进入 Schema；
- Plugin 覆盖内置工具需要显式 Operator opt-in；
- Terminal 可以落到 local、Docker、SSH、Modal 等隔离后端；
- 核心工具面保持窄，大多数能力放在 CLI + Skill、Plugin 或 MCP 边缘。

源码入口：

- Mini：[tools/registry.py](../src/mini_harness/tools/registry.py)
- Hermes：[tools/registry.py](../../hermes-agent-main/tools/registry.py)
- Hermes：[toolsets.py](../../hermes-agent-main/toolsets.py)

## 4. 纠错层

Mini Harness 把未知工具、坏 JSON、Schema 错误、审批拒绝、Handler 异常分别编码成结构化 Tool Result。模型可以在下一轮看到具体错误并修改调用。

Hermes 的纠错面更宽：

- 修复 Provider 返回的空工具名和不兼容字段；
- 在 API 副本上清理孤立 Tool Result、补缺失 Result；
- 针对不同 Provider 清理 reasoning、thinking-only 消息和参数格式；
- 支持 Provider 重试、凭证切换和 fallback；
- 工具 Guardrail 可以把诊断建议附加到 Result，要求模型改变策略。

关键区别：Mini 的纠错主要发生在 Dispatcher；Hermes 的纠错分布在 Provider Transport、消息清洗、工具执行器和会话循环多个边界。

源码入口：

- Hermes：[agent_runtime_helpers.py](../../hermes-agent-main/agent/agent_runtime_helpers.py)
- Hermes：[conversation_loop.py](../../hermes-agent-main/agent/conversation_loop.py)

## 5. 止损层

两者都用“工具名 + 规范化参数的 SHA-256”识别重复调用，但策略不同。

Mini Harness：

- 每轮相同调用超过固定次数就阻止；
- 不区分它之前成功还是失败；
- Spawn 后端可硬终止执行单元，但写工具副作用仍标记为 `unknown`；
- 达到迭代上限后只允许一次无 Tools 收尾。

Hermes：

- 默认先警告，Hard Stop 需要显式开启；
- 分别统计“完全相同的失败”“同一工具连续失败”和“幂等工具无进展”；
- 对只读工具与可能产生副作用的工具采用不同语义；
- 工具批次可以并发；中断时取消未开始任务，已经运行的线程可能被放弃等待；
- Agent 本身有每轮上限、父子共享预算和 Grace Call。

因此 Mini 的重复阻断更容易理解，但也更激进：完全相同的成功读取也可能被阻止。Hermes 的策略更接近真实产品，但需要失败分类、结果哈希和工具幂等性知识。

源码入口：

- Mini：[guardrails.py](../src/mini_harness/guardrails.py)
- Hermes：[tool_guardrails.py](../../hermes-agent-main/agent/tool_guardrails.py)
- Hermes：[tool_executor.py](../../hermes-agent-main/agent/tool_executor.py)

## 6. 恢复层

Mini Harness 使用两份消息语义：

```text
original_json：首次落盘的审计事实
message_json：恢复后可发送给模型的工作副本
```

它会：

- 给缺失 Result 的 Call 插入 `unknown`；
- 把孤立 Result 标记 inactive；
- 同时修复重复 Call/Result ID；
- 保存恢复事件；
- 永不自动重放 unknown 工具。

Hermes 的 SessionDB 将 role、content、tool_calls、tool_call_id、reasoning、`effect_disposition` 等拆成独立列，并用自增 ID 而不是时间戳恢复真实插入顺序。这样可避免系统时钟回拨把 Tool Result 排到 Call 前面。

Hermes 的恢复还分两道：

1. Resume 清理中断尾部：副作用工具恢复为 `unknown`，只读工具可以标为 `none`；
2. 每次 API 调用前再做安全清洗：删除孤立 Result、补缺失 Result、去重 ID。

Mini 把修复持久写回数据库；Hermes 很多修复只作用于本次 `api_messages` 副本，长期会话历史仍保留更多原始信息。

源码入口：

- Mini：[session_store.py](../src/mini_harness/session_store.py)
- Hermes：[hermes_state.py](../../hermes-agent-main/hermes_state.py)
- Hermes：[replay_cleanup.py](../../hermes-agent-main/agent/replay_cleanup.py)
- Hermes：[message_sanitization.py](../../hermes-agent-main/agent/message_sanitization.py)

## 7. Prompt Cache

Mini Harness 在 Session 创建时保存 System Prompt，Resume 时忽略后来配置中的新 Prompt。长期历史会持续增长，但每次发送给 Provider 的 API 副本有独立预算：

- 消息和 Tool Schema 一起做近似 Token 估算；
- 先压缩旧 Tool Result，再按完整 User Turn 删除旧上下文；
- Tool Call/Result 配对、最近轮次和 unknown 副作用事实受保护；
- 超限后裁到目标比例，并复用裁剪检查点，避免每轮移动缓存边界；
- 裁剪不回写 SQLite，也不修改 System Prompt；
- 保护内容仍超限时只进行一次最小无 Tools 收尾。

Hermes 把 Prompt Cache 当作核心设计约束：

- 长会话不在中途重建 System Prompt 或随意切换工具面；
- `conversation_history` 保留长期状态；
- `api_messages` 是每轮发送给 Provider 的工作副本，可以做清洗和 Provider 适配；
- Context Compression 是允许重写前缀的少数例外；
- Anthropic 等 Provider 还会加入显式 cache-control 断点。

## 8. Profile Memory

Mini Harness 的首版 Memory 是刻意受限的：

- Session 在 SQLite Schema v5 中持久绑定 Profile；
- 每个 Profile 只有固定的 `USER.md` 与 `MEMORY.md`；
- 新 Session 创建时把快照冻结进 System Prompt；
- 当前 Session 更新后不回写旧前缀，只通过 Tool Result 得知新状态；
- 更新需要审批，并使用 Spawn、锁文件和原子替换；
- 没有自动抽取、向量检索、远程 Provider 或跨 Profile 共享。

Hermes 的 Memory 同样区分磁盘长期状态与当前 API 消息，并允许 Memory
Provider 插件、服务门控工具和更完整的配置体系。Mini 只实现了最容易审计的
文件后端，但保留了最重要的生命周期边界。

## 9. Skills

Mini Harness 实现了 Skills 的核心渐进披露边界：

- Profile 本地/显式外部、内置、已启用可选目录按固定顺序发现；
- 启动只从 bounded frontmatter 构建元数据索引；
- 新 Session 冻结索引，Resume 不重建 System Prompt；
- 显式调用时完整读取一份 `SKILL.md`，作为当前 User 消息注入；
- 没有分页读取，超出完整读取上限就明确失败；
- Skill 命令不增加模型 Tool Schema。

Hermes 在此基础上还有 `skills_list`、`skill_view`、支持文件渐进读取、平台与
工具条件、配置变量、缓存快照、动态 slash command、Plugin namespace 和
更完整的安装管理。Mini 保留的是最值得学习的“摘要发现、全文执行、稳定
前缀”三条合同，不追求兼容完整生态。

## 10. 事件与审计

Mini 的 `events.jsonl` 只记录：

- Session ID；
- Provider 阶段、耗时和 Tool Call 数；
- 工具名称、Call ID、错误码和副作用状态；
- Turn 完成、部分完成或恢复统计。

它不记录用户正文、工具参数和 Result 正文。事件日志是 Best Effort 诊断数据，不替代 SQLite 消息审计。

Hermes 的可观察性覆盖 CLI、Gateway、Tool 生命周期、Hook、日志分级、Session 用量和平台消息 ID，适合多入口长期运行。

## 11. 执行后端

Mini Harness 把 Registry 编排与 Handler 生命周期分开：

- `SpawnProcessExecutionBackend` 是默认后端，使用父进程启动闸门和真实 Windows
  Spawn，超时后终止并回收子进程；不可序列化 Handler 在进入前安全失败；
- `ThreadExecutionBackend` 是显式兼容后端。因为 Python 无法安全终止运行中的
  线程，它会等待 Handler 完成，不会在后台工作仍继续时返回虚假的超时；
- Journal 先持久化 `running`，再放行 Handler；
- `hard_terminated` 与业务 `effect_disposition` 分别记录。

Hermes 的执行环境更广，终端工具可以运行在 local、Docker、SSH、Modal、
Daytona 等后端；但同样不能因为执行单元被杀死，就断言此前没有发生外部
副作用。

## 12. 最值得迁移到其他项目的原则

1. Assistant Tool Call 先落盘，Handler 后执行。
2. `none`、`completed`、`unknown` 是不同事实，不能用一个成功布尔值代替。
3. 恢复消息协议不等于恢复业务结果，更不等于可以重试。
4. 审计历史和 API 工作副本分开。
5. Guardrail 只在已授权集合和明确事实内决策。
6. 结构化事件默认脱敏，诊断日志不应成为新的秘密泄露通道。
7. 工具能力应尽量扩展在核心边缘，避免让每次模型调用都承担无关 Schema 成本。
8. Context 裁剪应作用于 API 副本，并以完整协议单元而不是字符切片为边界。
9. 跨会话 Memory 必须绑定用户/Profile，并且不能回写当前会话的稳定前缀。
10. Skill 索引只承担发现；执行前完整加载，既不常驻所有正文，也不分页偷懒。

## 13. Mini Harness 尚未解决

- Thread 兼容后端超时后仍可能继续运行，只有显式选择 Spawn 的工具可硬终止；
- Spawn 只提供进程生命周期隔离，不是权限、文件系统或网络沙箱；
- 没有多文件事务和目录 `fsync`；
- 没有 Streaming、精确 Provider tokenizer 和多 Summary Provider fallback 链；
- Skills 只能由用户/CLI 显式激活，没有模型自主 `skill_view`、安装器或远程市场；
- 没有 Plugins、MCP、Subagent、Gateway 和 Cron；
- 没有并发工具批次；跨进程协调目前只覆盖 Memory 更新和 Session 压缩；
- JSONL 日志只是 Best Effort，不提供遥测投递或集中查询。

这些不是“漏写”，而是最小 Harness 与生产 Agent Harness 的清晰边界。
