# Mini Harness Context Compression v2 开发进度

报告日期：2026-07-29  
对应方案：[CONTEXT_COMPRESSION_V2_PLAN.md](CONTEXT_COMPRESSION_V2_PLAN.md)
  
实际问题台账：
[DEVELOPMENT_PROBLEMS_AND_SOLUTIONS.md](DEVELOPMENT_PROBLEMS_AND_SOLUTIONS.md)

## 1. 当前结论

```text
Context Compression v2：14.1—14.4 全部完成
自动化测试：113/113 PASS
SQLite：Schema v5，integrity_check=ok
真实模型压缩：PASS
总体进度：100%
```

这次开发完成了两类互相独立的 Context 处理：

1. 每次请求的临时优化：只改 `api_messages`，不改长期历史；
2. 显式的持久化压缩：建立一次可审计的压缩边界，允许该边界处发生一次
   Prompt Cache 失效，后续 Resume 复用同一摘要。

## 2. 阶段看板

| 阶段 | 内容 | 状态 | 验收摘要 |
|---|---|---|---|
| 14.0 | 问题复现与 Hermes 对照 | 完成 | 找到新鲜 Tool Result 被过早裁剪、无 Tool Call 总预算、无持久摘要等根因 |
| 14.1 | Recent-aware Tool Result Pruning | 完成 | 新鲜结果、Recent Tail、unknown 受保护；旧结果信息化摘要和去重 |
| 14.2 | Tool Call Turn Budget | 完成 | 单批次/单轮预算；超限整批拒绝；只执行一次无工具收尾 |
| 14.3 | Persistent Semantic Compression | 完成 | Schema v5、Head/Middle/Tail、模型摘要、软归档、手动和自动入口 |
| 14.4 | Compression Resilience | 完成 | Session Lock、Cooldown、回滚、滚动摘要、低收益/回退熔断 |

## 3. 14.1：请求副本裁剪

已落地：

- `recent_tool_tail_tokens=12000`；
- 当前 User Turn 和 Recent Tail 的 Tool Result 优先保持完整；
- 旧的大型 Tool Result 改为包含工具名、路径、规模和结果状态的信息化摘要；
- 相同旧结果改为指向最新完整结果的回指；
- `effect_disposition=unknown` 的事实和禁止自动重试提醒不会被丢掉；
- 只有仍然超限时才删除最旧的完整 User Turn；
- SQLite、Agent 长期历史和 System Prompt 不被修改。

报告新增：

- `protected_recent_tool_results`；
- `pruned_old_tool_results`；
- `deduplicated_tool_results`；
- `protected_unknown_results`。

## 4. 14.2：Tool Call Turn Budget

默认配置：

```yaml
tools:
  max_calls_per_batch: 6
  max_calls_per_turn: 16
```

行为契约：

- 单批次超限时整批不执行；
- 剩余单轮名额不足时整批不执行；
- 每个被拒 Call 仍生成合法 Tool Result 和 Journal 终态；
- `requested / executed / blocked / consumed / remaining` 分开计数；
- 到达预算后不再提供工具，只做一次 finalizer；
- 事件不记录工具参数和结果正文。

## 5. 14.3：持久化语义压缩

新增核心模块：

- `src/mini_harness/context_compression.py`；
- `CompressionPolicy`；
- `CompressionManager`；
- `CompressionResult`；
- `OpenAICompatibleProvider.summarize()`。

压缩流程：

```text
测量活动历史和 Tool Schema
→ 保护最早 N 个完整轮次
→ 保护最近 token tail 与 unknown Tool 轮次
→ 只把中间完整轮次交给无工具 Summary Provider
→ 输入和输出脱敏
→ 生成 reference-only Summary + 固定 Assistant Bridge
→ 一个 SQLite 事务中软归档旧活动历史并写入新边界
→ Agent 从数据库重新加载边界
```

摘要不是新的用户任务。它使用以下边界语义：

```text
[CONTEXT COMPACTION — REFERENCE ONLY]
...
--- END OF CONTEXT SUMMARY —
Respond only to the latest real user message below.
```

最新真实 User Turn 始终位于摘要之后。摘要正文不会写入 JSONL 事件。

入口：

```powershell
.\.venv\Scripts\mini-harness.exe `
  --resume <session-id> `
  --compress "可选关注点"
```

交互模式：

```text
/context
/compress [可选关注点]
```

自动压缩由 `compression.enabled` 和阈值共同控制。手动 `/compress` 或
`--compress` 是显式强制边界，不依赖自动开关。
完成真实长会话和失败保全验证后，当前默认为开启；用户可显式设置
`compression.enabled: false` 退出。

## 6. Schema v5 与审计边界

Schema v5 新增：

- `compression_runs`：记录触发 token、原活动消息数、压缩消息数、摘要
  revision、是否使用 fallback、节省 token、成功或失败；
- `compression_state`：记录会话锁、锁过期、冷却期、连续低收益、连续
  fallback、自动暂停和最后失败类型。

成功压缩不会删除原始消息：

- 原活动记录改为 `active=0`；
- Head、Summary、Bridge、Tail 作为新的活动边界写入；
- `original_json` 继续保留；
- Resume 只读取活动边界；
- 审计接口仍能读取压缩前原文。

所有归档、边界插入、Run 记录和 State 更新在同一个
`BEGIN IMMEDIATE` 事务中完成。任何一步失败都整体回滚。

## 7. 14.4：工程韧性

已落地：

- 每个 Session 一把带 TTL 的压缩锁；
- 压缩期间活动消息 ID 发生变化则拒绝提交；
- 存在 `prepared/running` Tool Journal 时拒绝压缩；
- 摘要失败默认不改活动历史，并进入 cooldown；
- 可配置确定性 fallback；
- 再次压缩会合并旧 Summary，而不是叠加多个活动 Summary；
- 连续 fallback 或连续低收益达到阈值后，自动压缩暂停；
- 手动压缩仍可用于人工恢复；
- 推理模型的摘要输出预算保留最多 1024 token 的实用下限，避免隐藏
  reasoning 耗尽全部输出额度。

## 8. 自动化验证

全量命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

结果：

```text
Ran 113 tests
OK
```

新增压缩测试覆盖：

- v4 → v5 临时数据库迁移且会话不丢失；
- 中间历史软归档；
- Resume 复用同一摘要；
- 摘要失败保持活动历史；
- 事务注入失败整体回滚；
- 摘要输入、输出和活动副本敏感信息脱敏；
- 两个数据库连接竞争同一 Session Lock；
- 滚动 fallback 始终只有一份活动 Summary；
- 连续 fallback 触发自动熔断；
- Agent 手动压缩后重新加载；
- Agent 自动压缩发生在主 Provider 请求之前。
- Summary Provider 请求不携带工具，并拒绝异常 Tool Call。

## 9. 真实数据迁移证据

迁移前原库：

```text
Schema v4
integrity_check=ok
sessions=24
messages=174
```

先通过 SQLite Backup API 创建：

```text
.mini-harness/sessions.pre-schema-v5.db
```

备份校验：

```text
Schema v4
integrity_check=ok
sessions=24
messages=174
```

之后才由新版 `SessionDB` 打开原库。迁移后：

```text
Schema v5
integrity_check=ok
sessions=24
messages=174
compression_runs=0
```

原有 24 个 Session 和 174 条消息全部保留。

## 10. 真实模型回归

第一次样本 `context-compression-v2-regression` 是一个几乎只有一个巨大
Skill User Turn 的会话。所有内容都属于受保护的 Head/Tail，没有完整 Middle，
系统返回 `no_middle`，没有伪造可压缩空间，也没有调用摘要模型。

随后建立多轮 UTF-8 样本 `context-compression-v2-real`。首次摘要请求暴露出
推理型模型会先消耗 reasoning token；修正输出预算下限后，真实结果为：

```text
status=committed
before≈29252
after≈13465
saved≈15787
archived=10
used_fallback=false
```

只读审计结果：

```text
active_messages=10
inactive_messages=18
active_summaries=1
strict_role_alternation=true
resume_changed=false
resume_stable=true
latest_utf8_preserved=true
integrity_check=ok
```

该会话保留了一次失败 Run 和一次成功 Run。失败 Run 是有价值的真实审计，
不能删除或伪装成成功。

## 11. 最终能力边界

已经解决：

- 新鲜工具结果过早省略；
- 单轮工具调用数量失控；
- 长 Session 没有持久 Handoff；
- 摘要失败破坏原历史；
- 并发压缩产生分叉；
- 每轮重新摘要导致成本和缓存抖动；
- Resume 叠加多个摘要。

仍然刻意未做：

- Provider 官方 tokenizer；
- Streaming；
- 多 Summary Provider fallback 链；
- 向量检索或外部 Context Engine；
- Gateway、MCP、Plugin；
- 自动修改长期 Memory。

这些不属于 Context Compression v2 的验收范围。
