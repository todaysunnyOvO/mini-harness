# Mini Harness Context Compression v2 开发方案

日期：2026-07-27  
目标项目：`D:\learning\hermes-agent\mini-harness`  
参考项目：`D:\learning\hermes-agent\hermes-agent-main`

实施状态（2026-07-29）：阶段 14.1—14.4 已全部完成；Schema v5、真实模型
摘要、恢复稳定性和完整性检查均已验收。实际证据见
[CONTEXT_COMPRESSION_V2_PROGRESS.md](CONTEXT_COMPRESSION_V2_PROGRESS.md)。

## 1. 开发背景

Mini Harness 在本方案编写时已完成阶段 7—13，具备 Agent Tool Loop、Tool Preflight、审批、
重复调用止损、Execution Journal、SQLite Session、API Message Builder、
Context 预算、Profile Memory 和 Skills，当时共有 95 项自动化测试。

现有 Context 实现主要位于：

- `src/mini_harness/context_budget.py`
- `src/mini_harness/api_messages.py`
- `src/mini_harness/agent.py`

当前机制是一次性 API 请求裁剪：

```text
完整历史
→ 构造深拷贝 api_messages
→ 压缩超大 Tool Result
→ 必要时删除最旧完整 User Turn
→ 发送 Provider
```

它不修改 SQLite 原始历史，审计边界是正确的；但真实 Skill 审查暴露了新问题。

## 2. 真实问题证据

真实 Session：

```text
skill-test-1
```

观测结果：

- `harness-review` 成功加载；
- Agent Loop 调用 Provider 8 次；
- 模型请求 26 个 Tool Call；
- 23 个成功执行；
- 3 个被 `repeated_call_blocked` 阻止；
- 最终因 `iteration_limit` 进入无工具收尾；
- 审查任务只部分完成。

第一次工具执行后：

```text
estimated_tokens_before = 8228
estimated_tokens_after  = 1558
compacted_tool_results  = 2
available_input_tokens  = 60000
budget_exceeded         = false
```

即使总 Context 远低于 60,000，刚执行成功的 Tool Result 仍因单条结果超过
2,000 近似 tokens 而被立即替换。

当前配置还存在上限不协调：

```text
tools.max_result_chars          = 12000
context.max_tool_result_tokens  = 2000 ≈ 8000 字符
```

因此一个合法的 12,000 字符 `read_file` 结果，可能在模型第一次看到它之前
就被替换为通用占位，形成：

```text
read_file 成功
→ 新鲜结果在下一次 API 请求前被省略
→ 模型没有获得完整内容
→ 再次读取
→ 再次省略或被 Guardrail 阻止
→ 达到 iteration_limit
```

## 3. 为什么要开发 v2

### 3.1 新鲜 Tool Result 必须至少完整交付一次

Handler 执行成功不等于模型已经看到结果。当前任务刚产生的证据不能仅因单条
大小阈值而在首次交付前消失。

### 3.2 总预算和单条预算需要协同

单条 Tool Result 限制应该防止旧输出长期占用 Context，而不是无条件删除当前
任务的新鲜结果。

### 3.3 通用占位丢失的信息过多

当前占位没有保留工具名、路径、命令、输出规模和关键错误。模型无法判断结果
是否已经获得，容易重复调用。

### 3.4 `max_iterations` 不是工具总调用上限

一次模型响应可以产生多个 Tool Call。真实测试只有 8 次 Provider 循环，却
执行了 23 个工具，因此需要 Batch 和 Turn 两层工具数量预算。

### 3.5 API 副本裁剪不能替代持久压缩边界

当前 Context Checkpoint 只存在于 Agent 进程中，没有可恢复的稳定 Handoff
Summary。长会话需要：

```text
稳定 Head
+ 结构化 Middle Summary
+ 最近 Tail
```

压缩边界持久化后，后续 Prompt Prefix 才能重新稳定。

## 4. 必须保持的设计不变量

### CC-001：审计历史不能消失

压缩前的原始消息必须继续保存在 SQLite。允许标记为非活动，但不能物理删除
或覆盖原始 JSON。

### CC-002：API 副本和持久历史继续分层

普通 Tool Result 裁剪只作用于 `api_messages`。只有显式 Compression
Boundary 可以改变 Session 的活动上下文，而且必须原子持久化。

### CC-003：保护新鲜 Tool Result

当前 User Turn 和受保护 Tail 中的 Tool Result，不得仅因超过旧的单条阈值
而在首次交付前被省略。

### CC-004：Tool Call/Result 是协议单元

裁剪、归档、摘要和预算拒绝均不得产生缺失 Result 的 Call 或孤立 Result。

### CC-005：unknown 不可降级

`effect_disposition=unknown` 必须继续表达结果未知和禁止自动重试，不能被摘要
成成功、失败或无副作用。

### CC-006：摘要失败不丢历史

摘要 Provider 认证失败、网络失败、空响应或非法响应时，默认行为必须是：

```text
Compression abort
→ 原活动历史不变
→ 不提交压缩边界
→ 明确报告失败
```

### CC-007：摘要只作为参考上下文

摘要中的历史请求不得被当成当前任务。最新真实 User Message 始终拥有最高
优先级。

### CC-008：Prompt Cache 失效必须是显式边界

普通轮次不重建 System Prompt。Compression Boundary 可以造成一次缓存未命中，
但新前缀必须稳定，不能每轮重新生成摘要。

### CC-009：先做确定性优化，再接摘要模型

```text
新鲜结果保护
→ 旧结果信息化裁剪
→ 工具调用总预算
→ Head/Middle/Tail
→ 持久摘要
→ 并发锁与失败恢复
```

## 5. 从 Hermes 学什么

参考：

- `agent/context_compressor.py`
- `agent/conversation_compression.py`
- `agent/turn_context.py`
- `agent/conversation_loop.py`

应迁移的原则：

1. 最近 Tail 按 token budget 保护；
2. 只裁剪旧 Tool Result；
3. 相同 Tool Result 只保留最新完整副本；
4. 旧结果生成信息化一行摘要；
5. Head / Middle / Tail 三段规划；
6. 中间历史生成结构化 Handoff Summary；
7. 多次压缩滚动更新旧摘要；
8. Summary 使用 reference-only 前缀和结束标记；
9. 摘要失败冷却和 anti-thrashing；
10. 原始历史软归档；
11. Session 级 Compression Lock；
12. 压缩后用真实 Provider usage 验证效果。

暂不复制：

- Legacy child Session rotation；
- Plugin Context Engine；
- Codex app-server native compaction；
- Gateway 多实例路由；
- 多模态压缩；
- 完整辅助 Provider 矩阵；
- Hermes 的历史兼容分支。

Mini 首版采用同 Session 原地软归档。

---

## 6. 阶段 14.1：Recent-aware Tool Result Pruning

### 6.1 目标

解决“新鲜 `read_file` 成功后，在下一次 Provider 请求前立即被省略”。

### 6.2 配置

在 `context` 中增加：

```yaml
context:
  max_tool_result_tokens: 2000
  recent_tool_tail_tokens: 12000
  min_recent_turns: 1
```

新语义：

- `max_tool_result_tokens` 只作用于旧 Tool Result；
- `recent_tool_tail_tokens` 是最近结果的完整保护预算；
- `min_recent_turns` 至少保护最近完整 User Turn。

### 6.3 算法

```text
构造 API 副本
→ 从尾部计算受保护 Tail
→ 当前 User Turn 的 Tool Result 优先完整保留
→ 识别重复结果
→ 最新重复结果保留完整
→ 旧重复结果替换为 back-reference
→ 超出 Tail 的旧 Tool Result生成信息化摘要
→ 重新测量总 Context
→ 仍超限时删除最旧完整轮次
```

### 6.4 信息化摘要

至少支持：

```text
[read_file] path=src/mini_harness/agent.py, original_chars=12000,
result=completed, old content pruned

[list_files] path=., entries=<count>, result=completed,
old listing pruned

[write_file] path=..., result=completed,
effect_disposition=completed

[tool_name] result=<ok/error>, original_chars=<n>,
effect_disposition=<none/completed/unknown>
```

unknown 必须额外包含：

```text
Outcome remains unknown. Do not retry automatically.
```

### 6.5 重复结果去重

对字符串结果计算内容哈希，从新到旧扫描：

```text
最新相同结果保留完整
旧相同结果替换为：
[Duplicate tool output — same content as a newer call]
```

只修改 API 副本。

### 6.6 事件

扩展 `context.budget_evaluated`：

```json
{
  "protected_recent_tool_results": 0,
  "pruned_old_tool_results": 0,
  "deduplicated_tool_results": 0,
  "dropped_turns": 0
}
```

事件不得记录路径、参数或正文。

### 6.7 验收

- 新鲜 12,000 字符结果在总预算允许时至少完整交付一次；
- 旧大型结果生成信息化摘要；
- 最新重复结果完整、旧结果回指；
- unknown 语义保持；
- SQLite 和 `agent.messages` 不变；
- 95 项历史测试全部通过；
- 真实 Skill 回归不再形成“读取后立即省略”循环。

---

## 7. 阶段 14.2：Tool Call Turn Budget

### 7.1 配置

```yaml
tools:
  max_calls_per_batch: 6
  max_calls_per_turn: 16
```

### 7.2 批次语义

如果批次超过 `max_calls_per_batch`，或完整执行它会超过剩余 Turn Budget：

```text
整批不执行
→ 每个 Call 生成结构化 Result
→ error_code=tool_call_budget_exceeded
→ effect_disposition=none
→ execution_phase=rejected_before_execution
→ 停止继续提供工具
→ 做一次 no-tools finalizer
```

不能只执行批次前半部分。

### 7.3 计数

记录：

- `requested_tool_calls`
- `executed_tool_calls`
- `blocked_tool_calls`
- `remaining_tool_budget`

Guardrail 阻止的调用计入 requested，不计入 executed。

### 7.4 验收

- 超限批次不执行任何 Handler；
- 每个 Call 都有对应 Result；
- 不产生部分批次副作用；
- 只执行一次无工具收尾；
- 事件不记录参数正文。

阶段 14.1、14.2 完成后必须暂停，更新进度并让用户验收，再进入下一阶段。

---

## 8. 阶段 14.3：Persistent Semantic Compression

### 8.1 配置分层

`context` 继续负责一次性请求预算。新增：

```yaml
compression:
  enabled: false
  threshold_ratio: 0.75
  target_ratio: 0.20
  protect_first_turns: 1
  tail_tokens: 12000
  summary_model: ""
  abort_on_summary_failure: true
  cooldown_seconds: 600
  in_place: true
```

第一版默认关闭，真实验证后再开启。

> 2026-08-24 更新：该段保留的是阶段 14.3 原始上线策略。REAL-001–REAL-012
> 的根因修复和真实长会话验证完成后，当前产品默认已改为 `enabled: true`；
> `false` 仍保留为用户显式退出开关。

### 8.2 触发

优先使用 Provider 真实 `prompt_tokens`；缺失时使用近似估算。阈值必须扣除输出
预留和 Tool Schema。

### 8.3 Head / Middle / Tail

```text
Head:
- System Prompt
- protect_first_turns 指定的最初完整轮次

Middle:
- 要压缩的旧完整轮次

Tail:
- 最近 tail_tokens 范围内的完整协议轮次
- 最新 User Turn
- 必须保留的 unknown 事实
```

边界不得切断 Tool Call/Result。

### 8.4 摘要格式

```text
[CONTEXT COMPACTION — REFERENCE ONLY]

## Historical Goals
## Completed Actions and Evidence
## Decisions
## Files and Durable State
## Errors and Blockers
## Unknown Tool Outcomes
## Historical User Preferences
## Unresolved Historical Questions
## Critical Context

--- END OF CONTEXT SUMMARY —
Respond only to the latest real user message below.
```

避免使用容易被误认为当前命令的裸标题，如 `Active Task`、`Next Steps`。

### 8.5 摘要安全

- 删除 reasoning；
- 输入和输出都做秘密脱敏；
- Tool 参数只保留必要路径和命令并限制长度；
- 单条消息保留 head + tail；
- 摘要模型不携带 Tool Schema；
- 摘要模型不能调用工具。

### 8.6 SummaryProvider

```python
class SummaryProvider(Protocol):
    def summarize(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_output_tokens: int,
    ) -> str:
        ...
```

默认可复用主 Provider，同时允许独立 `summary_model`。

### 8.7 SQLite Schema v5

新增：

```sql
compression_runs(
    id,
    session_id,
    created_at,
    trigger_tokens,
    original_active_message_count,
    compacted_message_count,
    summary_revision,
    used_fallback,
    status
)
```

原地软归档事务：

```text
先在内存生成并验证摘要
→ 开启事务
→ 旧 active 消息标记 active=0
→ 插入 Head + Summary + Tail 为 active=1
→ 插入 committed compression_run
→ 提交
```

事务失败必须全部回滚。Session ID 和原 System Prompt 保持不变。

### 8.8 Resume

- 只加载 `active=1` 的压缩后上下文；
- 原始 `active=0` 历史继续用于审计；
- 不重新生成摘要；
- Summary 字节内容保持一致；
- 新 Prompt Prefix 稳定。

### 8.9 验收

- 长会话产生一次稳定 Compression Boundary；
- 最新任务和 Tail 原文保留；
- Middle 被结构化摘要替代；
- 原始消息仍在 SQLite；
- Resume 不重新摘要；
- Summary Provider 失败时历史不变；
- 摘要不会重新执行历史任务；
- unknown 不会被改写成确定状态；
- Schema v4→v5 和失败回滚测试通过。

---

## 9. 阶段 14.4：Compression Resilience

### 9.1 Compression Lock

按 Session 增加带 TTL 的 SQLite Lock：

```text
try_acquire
→ refresh lease
→ atomic compression
→ release
```

两个 Agent 同时压缩时只允许一个成功。

### 9.2 Failure Cooldown

摘要失败后记录同 Session 冷却。自动压缩在冷却期不重复调用，手动 `/compress`
可以显式重试。

### 9.3 Anti-thrashing

以下情况连续两次后暂停自动压缩：

- 节省比例低于 10%；
- 压缩后真实 prompt tokens 仍高于阈值；
- 连续使用 deterministic fallback。

### 9.4 Rolling Summary

```text
上一份 Handoff Summary
+ 新进入 Middle 的轮次
→ 更新后的单份 Summary
```

不得累积多份摘要，也不得跨 Session 泄漏。

### 9.5 CLI

可增加：

```text
/context
/compress
/compress <focus topic>
```

这些命令不增加模型 Tool Schema。

## 10. 推荐代码结构

```text
src/mini_harness/
├── context_budget.py
│   ├── token估算
│   ├──完整轮次划分
│   └──请求预算报告
├── context_pruning.py
│   ├──RecentTailPlanner
│   ├──ToolResultPruner
│   └──Tool Result信息化摘要
├── context_compression.py
│   ├──CompressionPlanner
│   ├──SummaryProvider
│   ├──结构化Handoff
│   └──失败/冷却状态
├── api_messages.py
├── session_store.py
│   ├──Schema v5
│   ├──archive_and_compact
│   └──Compression Lock
└── agent.py
    ├──触发判断
    ├──Tool Call预算
    └──no-tools finalizer
```

不要复制 Hermes 整个 `ContextCompressor`。

## 11. 实施顺序

1. 用临时目录重现新鲜结果丢失；
2. 先写阶段 14.1 失败测试；
3. 实现 Recent-aware Tool Result Pruning；
4. 运行全量回归；
5. 先写阶段 14.2 失败测试；
6. 实现 Tool Call Turn Budget；
7. 运行真实 Skill 审查；
8. 更新进度并暂停；
9. 用户确认后再进入阶段 14.3；
10. Schema v5 先在临时数据库验证；
11. 备份真实数据库为
    `.mini-harness/sessions.pre-schema-v5.db`；
12. 再执行真实迁移；
13. 最后进入阶段 14.4。

## 12. 测试要求

每一阶段至少包含：

- 单元测试；
- 临时目录真实文件 E2E；
- 不应发生副作用的断言；
- 事件脱敏断言；
- SQLite 审计不变断言；
- 故障注入；
- 全部历史回归。

Schema v5 额外要求：

- v4→v5；
- 重复打开；
- 迁移失败回滚；
- 原始消息保留；
- 压缩事务失败回滚；
- Resume 只读取 active 上下文。

## 13. 非目标

本阶段不实现：

- Plugin Context Engine；
- Child Session rotation；
- 多模态压缩；
- Gateway；
- MCP；
- Subagent；
- 多 Provider fallback；
- 删除原始历史；
- 对外遥测。

## 14. 最终验收场景

```powershell
.\.venv\Scripts\mini-harness.exe `
  --session skill-context-v2 `
  --skill harness-review `
  --message '请审查 src/mini_harness/agent.py 的 Agent 主循环，并给出具体文件证据'
```

目标：

- 新鲜 `read_file` 内容至少完整发送一次；
- 不因立即省略而反复读取；
- Tool Call 总数不超过配置预算；
- 任务过大时明确止损；
- 不建议绕过 Guardrail；
- 事件解释保留、裁剪、去重和压缩决策；
- SQLite 原始审计历史完整；
- Semantic Compression 后 Resume 使用稳定 Handoff Summary。
