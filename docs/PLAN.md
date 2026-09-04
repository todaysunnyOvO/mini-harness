# Mini Harness 项目计划

## 1. 项目目标

亲手实现一个最小但完整的 Agent Harness，用来理解 Hermes Agent 的核心工程原则：

```text
用户目标
→ Context 构建
→ 模型决策
→ Tool Call
→ 工具执行
→ Tool Result
→ 继续推理或结束
```

最终重点是实现并验证四层兜底：

```text
预防 → 纠错 → 止损 → 恢复
```

## 2. 设计原则

1. 每一阶段只增加一个主要概念，保持代码可读。
2. 先实现最小正确语义，再对照 Hermes 的工业级实现。
3. 秘密放入 `.env`，行为配置放入 `config.yaml`。
4. 每个安全或恢复机制必须有对应的失败测试。
5. 不为了“看起来完整”提前加入 Memory、Skills、Plugins 等非当前阶段能力。

## 3. MVP 范围

计划包含：

- Python CLI；
- 一个 OpenAI-compatible Provider；
- System Prompt 与消息历史；
- Tool Registry、Schema 和 Dispatcher；
- `list_files`、`read_file`、`write_file`；
- 工作区 Scope 与写入审批；
- 参数校验与结构化 Tool Error；
- 结果限制、迭代预算和中断；
- SQLite SessionDB；
- Tool Call/Result 增量持久化；
- Resume 与缺失 Result 的 unknown 恢复；
- 单元测试、故障注入和 Hermes 对照文档。

第一版暂不包含：

- Memory、Skills、Plugins、MCP；
- 多 Agent、Gateway、Cron、Browser；
- Context Compression；
- 多 Provider、凭证池和自动 fallback。

## 4. 阶段与验收标准

### 阶段 1：纯文本 Agent

工作内容：

- 项目骨架、配置加载和 CLI；
- OpenAI-compatible Chat Completions Provider；
- 内存消息历史；
- 单次和交互式对话；
- 不访问网络的基础测试。

验收标准：

- `--check-config` 能验证配置；
- 使用有效 Provider 配置时能获得文本回答；
- 第二轮请求会携带第一轮 User/Assistant 历史；
- Provider 异常会变成明确错误，而不是打印模糊 traceback。

### 阶段 2：Tool Loop

工作内容：

- Tool Registry、Tool Schema 和 Dispatcher；
- `list_files`、`read_file` 两个只读工具；
- Assistant Tool Call 与 Tool Result 配对；
- 模型无 Tool Call 时自然结束。

验收标准：模型能读取文件、根据结果继续推理并给出最终回答。

### 阶段 3：预防与纠错

工作内容：

- Tool allowlist、工作区路径 Scope；
- JSON 参数与 Schema 校验；
- 有限工具名修复；
- Handler 异常转结构化 Tool Error。

验收标准：未授权工具、非法路径和非法参数都不会进入真实 Handler；模型可以根据 Tool Error 修正下一步。

实现说明：JSON Schema 使用 Draft 2020-12 标准 Validator。工具名修复只允许在当前 Registry 已注册集合中选择唯一、足够相似的候选，任何含糊情况都返回错误。

### 阶段 4：止损

工作内容：

- `write_file`、工作区写入边界与审批；
- 最大迭代数和 Tool Result 大小限制；
- 重复调用 Guardrail；
- 超时与协作式中断；
- 无 Tools 的最终收尾。

验收标准：危险动作必须审批，无进展循环和超时调用能够受控结束。

实现说明：

- `write_file` 在 Schema 校验之后、Handler 启动之前请求审批；拒绝或审批界面异常都会失败关闭，明确记录为 `effect_disposition=none`。
- 写入采用目标目录中的临时文件、`fsync` 临时文件和 `os.replace`，保证目标路径对观察者呈现旧文件或完整新文件，而不是半个文件。它不等于多文件事务，也不承诺目录项在断电时绝对持久。
- Tool Result 有字符预算；过大结果会被替换为合法 JSON 的截断摘要。
- 重复调用以“工具名 + 规范化参数”的 SHA-256 签名计数，每轮第三次相同调用会被阻止，不自动猜测替代动作。
- Tool 超时只报告 `effect_disposition=unknown`，不会自动重试，因为后台操作可能已经产生副作用。
- 达到迭代上限或收到协作式中断后，只允许一次 `tools=None` 的模型调用生成收尾摘要，并把该轮标记为部分完成。

### 阶段 5：持久化与恢复

工作内容：

- SQLite `sessions` 和 `messages`；
- User、Assistant Tool Call、Tool Result 增量保存；
- Session Resume；
- 缺失 Result 占位、孤立 Result 删除和重复 ID 修复。

验收标准：模拟在 Call 落盘后、Result 落盘前崩溃，恢复后不会自动重放未知的非幂等工具。

实现说明：

- SQLite 开启 WAL、外键和 `synchronous=FULL`。Session 保存创建时的 System Prompt，恢复时不使用后来修改的配置覆盖它，从而保持会话前缀稳定。
- User、Assistant、Tool Result 按消息逐条独立事务写入；尤其是 Assistant Tool Call 必须在 Handler 启动前落盘。
- `messages.original_json` 保存首次写入的审计事实；`message_json` 是恢复后可发送给模型的工作副本；`active` 控制工作副本是否参与会话；`recovery_events` 记录每次修复。
- Call 存在但 Result 缺失时，插入 `effect_disposition=unknown` 的 Tool Result，并明确写明没有自动重试。
- 孤立 Tool Result 从活动历史移除但不物理删除；重复 Tool Call ID 及对应 Result ID 一起确定性改名，原始 JSON 保持不变。
- Provider 在 User 落盘后失败时，原始 User 记录仍保留；下一次请求会把相邻 User 消息合并为一个 API 消息，避免非法角色序列。

### 阶段 6：验证与 Hermes 对照

工作内容：

- 四层兜底端到端故障测试；
- 事件日志和调试输出；
- 编写最小实现与 Hermes 实现的差异说明。

验收标准：每层至少有一个可重复的故障案例和自动化测试。

实现说明：

- JSONL 事件只记录 Session、阶段、工具名、Call ID、错误码、副作用状态、耗时和计数，不记录用户正文、工具参数或 Result 正文。
- 事件日志是 Best Effort 诊断数据；写日志失败不能改变 Agent、工具或恢复语义，也不能替代 SQLite 审计事实。
- `tests/test_fault_matrix.py` 为预防、纠错、止损、恢复各提供一个贯穿 Agent Loop 的故障场景。
- `docs/HERMES_COMPARISON.md` 从工具面、循环、四层兜底、Prompt Cache、持久化和可观察性逐项对照 Hermes 源码。

## 5. 最终验收场景

1. 正常完成两轮文本对话。
2. 读取工作区文件并回答问题。
3. 写文件前请求审批。
4. 阻止访问工作区外路径。
5. 非法 Tool 参数不进入 Handler。
6. Handler 异常作为 Tool Result 返回模型。
7. 无限 Tool Loop 被预算终止。
8. 崩溃后恢复消息协议，但不盲目重试 unknown 副作用。
9. SQLite 可审计完整的 User、Assistant、Call 和 Result。
10. 文档能解释各机制在 Hermes 中的对应实现。

## 6. 推荐学习方法

每个阶段都按以下顺序进行：

```text
解释原理
→ 阅读 Hermes 对应源码
→ 编写最小实现
→ 主动制造失败
→ 编写测试
→ 记录两者差异
```
