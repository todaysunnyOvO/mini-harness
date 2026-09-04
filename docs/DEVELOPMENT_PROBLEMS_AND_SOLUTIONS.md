# Mini Harness 开发实际问题与解决方案日志

创建日期：2026-07-29  
维护方式：持续追加  
适用范围：Mini Harness 后续所有开发阶段

## 1. 文档目的

这是一份长期维护的工程问题台账，用来记录开发和真实验证过程中已经发生的
实际问题，以及最终采用的解决方案。

它回答：

```text
开发中实际出现了什么问题？
问题是怎样被发现和复现的？
表面现象背后的根因是什么？
哪些直觉方案被放弃，为什么？
最终如何解决？
用什么证据证明已经解决？
还有哪些边界和遗留风险？
```

这份文档不只属于某一个功能。以后开发 Memory、Skills、Context、Provider、
Tools、Session、CLI 或其他模块时，只要遇到值得复盘的真实问题，都继续追加
到这里。

## 2. 与其他文档的区别

| 文档 | 负责回答 |
|---|---|
| 开发计划 | 接下来准备做什么 |
| 开发进度 | 已经完成了什么 |
| 测试代码 | 某条行为合同是否持续成立 |
| `FAULT_MATRIX.md` | 某类故障应如何兜底 |
| 本文档 | 实际遇到了什么问题，为什么发生，最后怎样解决 |

本文档不复制整个开发计划，也不罗列所有测试。只有发生过、能够给出证据并且
具有复盘价值的问题才进入这里。

## 3. 收录标准

满足以下任一条件时，应新增一条记录：

- 真实运行结果和原设计预期不一致；
- 自动化测试发现了原实现的错误行为；
- 一个看似正确的修复在集成路径中失败；
- Provider、操作系统、SQLite 或执行后端暴露了新的现实约束；
- 问题导致重复调用、数据风险、状态不一致、成本失控或错误恢复；
- 为解决问题作出了以后仍有参考价值的工程取舍；
- 某次“没有执行”实际上是安全兜底正确工作的结果。

以下内容通常不单独收录：

- 尚未出现的纯理论风险；
- 没有具体消费者的设想；
- 普通功能实现步骤；
- 只修改文案或格式；
- 没有复现证据的猜测。

## 4. 状态定义

| 状态 | 含义 |
|---|---|
| `observed` | 已观察到现象，但尚未确认根因 |
| `diagnosed` | 已定位根因，尚未完成修复 |
| `fixed` | 已实现修复，自动化验证通过 |
| `verified` | 自动化和真实路径均已验证 |
| `mitigated` | 风险已降低，但根因或外部限制仍存在 |
| `accepted` | 明确接受该边界，不计划消除 |
| `regressed` | 已解决的问题再次出现 |

问题状态只有在证据充分时才能从 `fixed` 更新为 `verified`。

## 5. 编号规则

统一使用：

```text
DEV-001
DEV-002
DEV-003
...
```

编号只表示记录顺序，不表示严重程度，也不随开发阶段重新开始。

测试、Session、事件和提交说明可以引用这个编号。

## 6. 新问题记录模板

以后新增问题时，复制以下模板并填写：

```markdown
## DEV-XXX：一句话描述问题

- 首次发现：YYYY-MM-DD
- 所属阶段：阶段编号或功能名
- 当前状态：observed / diagnosed / fixed / verified / mitigated / accepted
- 严重程度：low / medium / high / critical
- 相关模块：文件或模块名称

### 现象

描述真实观察结果，不先写推测。

### 复现条件

写明 Session、命令、配置、输入或最小测试。

### 影响

说明它影响正确性、成本、缓存、审计、恢复、安全还是用户体验。

### 根因

写到具体机制或代码边界，不能只写“逻辑有问题”。

### 放弃的方案

- 方案 A：为什么没有采用；
- 方案 B：为什么会引入新的问题。

### 最终解决方案

写清行为合同和关键工程边界。

### 验证证据

- 自动化测试；
- 真实路径结果；
- 数据库或事件审计；
- 修复前后指标。

### 遗留边界

说明仍未解决、刻意不做或需要继续观察的部分。

### 相关资料

- 代码文件；
- 测试文件；
- 开发计划或进度文档。
```

## 7. 问题索引

| 编号 | 问题 | 状态 | 所属阶段 |
|---|---|---|---|
| DEV-001 | 新鲜 Tool Result 被立即压缩，导致模型重复读取 | verified | Context Compression 14.1 |
| DEV-002 | `max_iterations` 无法限制一轮中的 Tool Call 总量 | verified | Context Compression 14.2 |
| DEV-003 | 临时 Context 裁剪无法为长 Session 提供稳定交接 | verified | Context Compression 14.3 |
| DEV-004 | 推理模型消耗隐藏 reasoning token，摘要没有可见文本 | verified | Context Compression 14.3 |
| DEV-005 | 巨大单轮会话没有可以安全压缩的 Middle | accepted | Context Compression 14.3 |

---

## DEV-001：新鲜 Tool Result 被立即压缩，导致模型重复读取

- 首次发现：2026-07-27
- 所属阶段：Context Compression 14.1
- 当前状态：`verified`
- 严重程度：high
- 相关模块：`context_budget.py`、`context_pruning.py`

### 现象

真实 `harness-review` Skill 中，`read_file` 成功返回源码，但下一轮 Provider
请求中的结果已经被简化。模型缺少刚取得的证据，于是重新读取同一文件。

```text
read_file 成功
→ 新结果立即被压缩
→ 模型看不到完整内容
→ 再次 read_file
```

### 复现条件

当时配置：

```text
tools.max_result_chars = 12000
context.max_tool_result_tokens = 2000
approximate_chars_per_token = 4
```

工具允许约 12,000 字符，但 Context 单条结果预算约为 8,000 字符。

### 影响

- 模型重复读取；
- Tool Call 和 token 数增长；
- 已取得的证据没有获得一次完整消费机会；
- 长任务更容易触发迭代上限。

### 根因

旧算法只判断单条 Tool Result 是否超过固定阈值，没有判断它是否：

- 属于当前 User Turn；
- 位于 Recent Tail；
- 尚未被模型完整看到；
- 在总 Context 仍有余量时可以完整保留；
- 包含必须保护的 `unknown` 状态。

工具层与 Context 层的行为合同不一致。

### 放弃的方案

- 只提高单条上限：只能推迟问题；
- 永久保留所有结果：会让 Context 无限增长；
- 直接字符截断：可能删除状态和文件尾部证据。

### 最终解决方案

改为新鲜度优先：

```text
保护当前 Turn
→ 保护 Recent Tail
→ 旧重复结果回指
→ 旧大型结果信息化摘要
→ 仍超限才删除最旧完整 Turn
```

### 验证证据

真实 Skill 回归：

```text
第一次工具结果受保护：2
第一次工具结果被裁剪：0
普通 Agent Loop 中旧结果裁剪最大值：0
```

自动化测试验证 SQLite 和长期 Agent 历史不被请求副本裁剪回写。

### 遗留边界

Token 计算仍是确定性近似值，不是 Provider 官方 tokenizer。

### 相关资料

- `src/mini_harness/context_pruning.py`
- `src/mini_harness/context_budget.py`
- `tests/test_context_budget.py`
- `CONTEXT_COMPRESSION_V2_PROGRESS.md`

---

## DEV-002：`max_iterations` 无法限制 Tool Call 总量

- 首次发现：2026-07-27
- 所属阶段：Context Compression 14.2
- 当前状态：`verified`
- 严重程度：high
- 相关模块：`agent.py`

### 现象

真实执行中：

```text
Provider 请求：8
Tool Call 请求：26
成功执行：23
最终：partial / iteration_limit
```

虽然循环次数受到限制，但一次 Assistant 响应可以包含多个 Tool Call。

### 影响

- 工具执行量与配置预期不一致；
- Token、延迟和副作用风险继续增长；
- 最后一个 Provider 响应仍可能产生很大批次；
- 任务不能及时进入总结。

### 根因

`max_iterations` 限制的是 Provider 循环，不是 Tool Call 数量。原实现没有：

- 单批次 Tool Call 预算；
- 单 User Turn 累计 Tool Call 预算。

### 放弃的方案

- 只执行超限批次前半部分：会造成批次状态不一致；
- 拒绝后不生成 Result：会破坏 Call/Result 消息协议；
- 超限后继续提供工具：可能形成预算耗尽后的死循环。

### 最终解决方案

新增：

```yaml
max_calls_per_batch: 6
max_calls_per_turn: 16
```

超限批次整批拒绝，每个 Call 仍生成合法 Result 和 Journal。预算耗尽后停止
提供工具，只允许一次 no-tools finalizer。

### 验证证据

真实回归中，剩余一个名额时模型返回四个 Call：

```text
四个 Call 整批拒绝
Handler 执行次数：0
四个 Call 均有 Result
随后只执行一次 finalizer
最终：partial / tool_call_budget
```

### 遗留边界

当前工具仍顺序执行，没有实现 Hermes 式并发 Tool Batch。

### 相关资料

- `src/mini_harness/agent.py`
- `tests/test_agent.py`
- `CONTEXT_COMPRESSION_V2_PROGRESS.md`

---

## DEV-003：临时 Context 裁剪无法为长 Session 提供稳定交接

- 首次发现：2026-07-27
- 所属阶段：Context Compression 14.3
- 当前状态：`verified`
- 严重程度：high
- 相关模块：`context_compression.py`、`session_store.py`

### 现象

旧 Context Budget 能让单次 `api_messages` 放进窗口，却不能解决：

- Resume 后重新处理越来越长的历史；
- 早期决定和证据离开模型视野；
- 被删除的完整 Turn 没有语义交接；
- 进程内 Checkpoint 无法跨进程保存；
- 临时裁剪边界反复移动。

### 影响

SQLite 中虽然仍有原始数据，但模型无法稳定利用它继续工作，表现为重复调查、
遗漏决定和 Prompt Cache 抖动。

### 根因

临时 `api_messages` 裁剪和持久会话状态生命周期不同。旧实现只有前者，没有
明确、可恢复的持久 Compression Boundary。

### 放弃的方案

- 把临时裁剪直接写回 SQLite：破坏原始审计；
- 每次 Resume 重新生成摘要：成本和前缀不稳定；
- 把摘要写入 System Prompt：破坏缓存并提高历史内容优先级；
- 只提高 Context Window：只能推迟问题。

### 最终解决方案

引入：

```text
Head：最早必须保留的完整 Turn
Middle：交给无工具 Summary Provider
Tail：最新任务、Recent Tail、unknown 事实
```

成功后在一个事务中软归档旧活动历史，写入：

```text
Head
→ reference-only Summary
→ Assistant Bridge
→ Tail
```

原消息只改为 `active=0`，不删除。Resume 复用同一份 Summary。

### 验证证据

真实多轮 UTF-8 Session：

```text
before≈29252
after≈13465
saved≈15787
archived=10
active_summaries=1
resume_stable=true
integrity_check=ok
```

### 遗留边界

- 自动持久压缩默认关闭；
- 模型摘要仍可能遗漏语义，因此原文必须继续保留；
- 当前没有多 Summary Provider fallback 链。

### 相关资料

- `src/mini_harness/context_compression.py`
- `src/mini_harness/session_store.py`
- `tests/test_context_compression.py`
- `CONTEXT_COMPRESSION_V2_PLAN.md`

---

## DEV-004：推理模型消耗隐藏 token，摘要没有可见文本

- 首次发现：2026-07-29
- 所属阶段：Context Compression 14.3 真实回归
- 当前状态：`verified`
- 严重程度：medium
- 相关模块：`context_compression.py`、`provider.py`

### 现象

第一次真实摘要请求失败：

```text
Provider response contains neither text nor tool calls
```

原活动历史没有改变，压缩 Run 被记录为失败。

### 复现条件

Summary 输出预算被目标比例压缩到 64 token。真实模型先生成隐藏
`reasoning_content`，耗尽额度后没有产生可见 `content`。

### 影响

- 摘要 Provider 实际可用，但 Harness 误判为无文本；
- 自动重试会浪费调用；
- 持久压缩无法提交。

### 根因

输出预算只按最终 Summary 目标计算，没有考虑推理模型的隐藏 reasoning token
与可见文本共享 `max_tokens`。

### 放弃的方案

- 接受 reasoning 作为 Summary：它不是最终交接文本；
- 空文本时自动无限重试：费用和结果不可控；
- 永远给最大输出额度：失去目标压缩约束。

### 最终解决方案

继续使用 `target_ratio` 控制目标，但为推理模型保留不超过配置上限的
1024-token 实用下限。

失败仍遵循：

```text
活动历史不变
→ failed Run
→ cooldown
→ 人工修复后显式重试
```

### 验证证据

修复后真实压缩成功：

```text
status=committed
used_fallback=false
saved≈15787
```

新增测试验证 Summary 请求不携带工具，并拒绝模型返回的 Tool Call。

### 遗留边界

不同 Provider 对 reasoning token 的计费和字段定义并不完全一致；以后新增
Provider 时需要重新验证其输出预算语义。

### 相关资料

- `src/mini_harness/provider.py`
- `src/mini_harness/context_compression.py`
- `tests/test_provider.py`
- `tests/test_context_compression.py`

---

## DEV-005：巨大单轮会话没有可以安全压缩的 Middle

- 首次发现：2026-07-29
- 所属阶段：Context Compression 14.3 真实回归
- 当前状态：`accepted`
- 严重程度：medium
- 相关模块：`context_compression.py`

### 现象

真实样本约 34,394 tokens，但几乎全部属于一个巨大的 Skill User Turn。

```text
Head/Tail：必须保护
Middle：为空
结果：status=no_middle
```

### 影响

这个 Session 虽然很长，但无法通过当前持久压缩机制缩小。

### 根因

安全压缩以完整 User Turn 和完整 Tool Call/Result 为边界。当前会话没有可以
从中间独立取出的完整 Turn。

### 放弃的方案

- 强行按字符切割：可能拆开 Skill 指令、用户约束或工具协议；
- 将完整巨大 Turn 全部交给摘要：最新真实任务可能被摘要替代；
- 把 `no_middle` 伪装成成功：没有产生任何实际压缩。

### 最终解决方案

明确返回：

```text
status=no_middle
```

不调用 Summary Provider，不修改 SQLite，不伪造节省量。

### 验证证据

真实 Session 保持原活动历史，数据库完整性为 `ok`，没有新增成功压缩 Run。

### 遗留边界

如果未来确实需要压缩巨大单轮，必须先为 Skill 正文、附件或 Tool Payload
设计独立、可验证的内容边界，不能在当前消息层直接切字符。

### 相关资料

- `src/mini_harness/context_compression.py`
- `tests/test_context_compression.py`

---

## 8. 后续维护规则

以后每次开发结束前，检查：

1. 是否出现了新的真实问题；
2. 是否已经有对应 DEV 编号；
3. 状态是否需要从 `observed` 更新为 `diagnosed` 或 `verified`；
4. 修复证据是否包含自动化测试和真实路径；
5. 是否记录了被放弃方案及原因；
6. 是否明确写出遗留边界；
7. 开发进度文档是否链接到相关 DEV 条目。

不要为了让记录看起来整洁而删除失败案例。只要失败真实发生且具有工程价值，
就应保留，它是设计决策和恢复能力的证据。
