# Mini Harness 执行进度

## 当前状态

```text
当前阶段：阶段 6 已完成
项目状态：Mini Harness MVP 全部验收完成
总体进度：100%
```

## 阶段看板

| 阶段 | 状态 | 进度 |
|---|---|---:|
| 0. 目标与范围 | 已完成 | 100% |
| 1. 纯文本 Agent | 已完成 | 100% |
| 2. Tool Loop | 已完成 | 100% |
| 3. 预防与纠错 | 已完成 | 100% |
| 4. 止损 | 已完成 | 100% |
| 5. 持久化与恢复 | 已完成 | 100% |
| 6. 验证与 Hermes 对照 | 已完成 | 100% |

## 已完成

- 确定独立项目目录和 MVP 边界；
- 完成正式项目计划；
- 创建 Python 包、配置和测试结构；
- 明确秘密与行为配置的分离原则；
- 实现 YAML 配置和最小 `.env` 加载；
- 实现 OpenAI-compatible `/chat/completions` HTTP Transport；
- 实现纯文本 Agent、内存历史、单次调用和交互式 CLI；
- Provider 失败不会把悬空 User Message 写入内存历史；
- 创建项目 `.venv` 并完成 editable install；
- Python 编译检查和第一阶段 6 项离线测试全部通过；
- 使用真实 DeepSeek-compatible 配置完成端到端文本请求，返回 `MINI_HARNESS_OK`；
- 实现 Tool Registry、OpenAI Tool Schemas 和 Dispatcher；
- Provider 能解析无文本的 Assistant Tool Calls；
- Agent 能执行 `Tool Call → Tool Result → 下一次模型调用`；
- 实现 `list_files`、`read_file` 和工作区路径边界；
- 未知工具、非法 JSON、Handler 输入错误会变成结构化 Tool Result；
- 加入最小迭代上限，防止阶段 2 出现无限 Tool Loop；
- CLI 显示不含参数和正文的工具执行状态；
- 12 项离线测试全部通过；
- 真实模型成功调用 `read_file` 读取 README，并回答 `# Mini Harness`。
- 引入 Draft 2020-12 JSON Schema 注册检查和调用前参数验证；
- 必填项、类型和额外字段错误会在 Handler 启动前阻断；
- 工具输入错误、未知工具、坏 JSON、Schema 错误和 Handler 异常使用不同错误码；
- 实现只在当前 Registry 授权集合内工作的保守工具名修复；
- 名称修复会在 Tool Result 和 CLI 状态中明确标记，不隐藏实际执行名称；
- 17 项离线测试全部通过；
- 真实模型先触发工作区逃逸错误，再根据 Tool Error 改读 `README.md` 并成功回答。
- `write_file` 与人工审批同时启用；审批拒绝或审批回调异常时 Handler 不会启动；
- 写文件采用同目录临时文件、文件 `fsync` 和原子替换，测试覆盖创建与覆盖；
- Tool Result 大小预算会返回合法 JSON 截断结果；
- Tool 超时返回 `effect_disposition=unknown`，不自动重试；
- 相同工具名与规范化参数生成稳定签名，每轮超过两次后阻止继续执行；
- 协作式中断会为批次中未执行的 Tool Call 补齐结构化 Result，保持消息协议完整；
- 迭代耗尽或中断后进行一次无 Tools 收尾，并明确把该轮标记为部分完成；
- 工具调用产生结果后立即保留 Call/Result 内存证据，后续 Provider 失败不会抹掉已经可能发生的副作用；
- 25 项离线测试全部通过；
- 真实模型经人工审批写入 `STAGE4_OK`，随后成功读回；验证后临时产物已清理。
- 实现 SQLite `sessions`、`messages` 和 `recovery_events`；
- 数据库启用 WAL、外键和 `synchronous=FULL`；
- Session 保存创建时的 System Prompt，恢复后保持原始前缀，不受新配置覆盖；
- User、Assistant Tool Call、Tool Result 和最终 Assistant 消息按条增量提交；
- Assistant Tool Call 在 Handler 启动前落盘，覆盖最危险的崩溃窗口；
- 缺失 Tool Result 会持久插入 `result_unavailable_after_resume`，副作用状态为 `unknown`；
- 恢复 unknown 只修复消息协议，不会调用任何 Handler；
- 部分批次只为真正缺失的 Call 补 Result，已存在的 Result 保持原样；
- 孤立 Tool Result 标记 inactive，不从审计数据库物理删除；
- 重复 Tool Call ID 与对应 Result ID 一起确定性修复，同时保留 `original_json`；
- Provider 失败后的 User 审计记录保留，相邻 User 在 API 工作副本中安全合并；
- CLI 支持 `--session`、`--resume` 和 `--list-sessions`；
- 33 项离线测试全部通过；
- 两个独立 CLI 进程成功恢复同一 Session，并从历史中回答 `RESUME_OK_57`。
- 新增脱敏 JSONL 事件日志，覆盖 Session、Turn、Provider、Tool 和 Recovery 生命周期；
- 事件只包含元数据，不写入用户正文、工具参数或 Tool Result 正文；
- 日志写入采用 Best Effort，观测失败不会改变 Agent 行为；
- 为预防、纠错、止损、恢复建立四个 Agent Loop 端到端故障场景；
- 新增事件脱敏测试，使用秘密标记验证 JSONL 中不存在正文或参数；
- 完成 `docs/FAULT_MATRIX.md`；
- 完成 `docs/HERMES_COMPARISON.md`，逐项对照 Hermes 工具 Registry、Tool Guardrail、并发执行、SessionDB、Replay Cleanup、Prompt Cache 和日志体系；
- 38 项自动化测试全部通过；
- 真实模型执行 `list_files` 后产生 9 条结构化事件，事件包含完整生命周期且不含 Prompt 和文件列表正文。

## 当前阻塞点

- 无。

## 最终验收状态

```text
预防：PASS
纠错：PASS
止损：PASS
恢复：PASS
真实 Provider：PASS
跨进程 Resume：PASS
事件脱敏：PASS
Hermes 对照文档：PASS
```

## 最近一次验证

验证日期：2026-07-23

```text
python -m compileall -q src tests                         PASS
python -m unittest discover -s tests -v                  PASS（38/38）
mini-harness --help                                      PASS
mini-harness --check-config                              PASS
真实 Provider 请求                                      PASS（MINI_HARNESS_OK）
真实 Provider Tool Loop                                 PASS（read_file → # Mini Harness）
真实 Tool Error 自我纠正                                PASS（越界 error → 合法 read_file）
真实审批写入与读回                                      PASS（write_file → read_file → STAGE4_OK）
真实跨进程 Session Resume                              PASS（STORED → RESUME_OK_57）
真实结构化事件链                                       PASS（9 events，正文与结果未泄露）
```

## 风险与决策记录

### D-001：项目与 Hermes 源码分离

决定：项目放在 `D:\learning\hermes-agent\mini-harness`，不修改 Hermes Core。

原因：降低耦合，让每个 Harness 机制都能被独立观察和测试。

### D-002：第一阶段不使用 OpenAI SDK

决定：使用 Python 标准库直接调用 OpenAI-compatible `/chat/completions`。

原因：减少依赖，并让请求消息、Header、错误与响应解析保持可见。未来增加 Streaming 或 Responses API 时再评估 SDK/Transport 抽象。

### D-003：写入工具不与只读 Tool Loop 同时上线

决定：阶段 2 只注册 `list_files` 和 `read_file`；`write_file` 推迟到阶段 4，与工作区写入边界和用户审批一起启用。

原因：不能先暴露有副作用能力，再期待未来补上止损。能力上线必须与对应安全机制同步。

### D-004：Schema 校验使用标准实现，名称修复保持保守

决定：使用 `jsonschema` 的 Draft 2020-12 Validator；名称修复仅在当前 Registry 中选择超过阈值且明显优于第二候选的唯一名称。

原因：手写不完整的 Schema Validator 容易制造假的安全感；名称纠错若跨越授权集合或候选含糊，可能把可见错误变成真实副作用。

### D-005：超时是 unknown，受控停止后只做一次无 Tools 收尾

决定：工具超时不自动重试，统一把副作用状态标记为 `unknown`；重复调用 Guardrail 仅阻止完全相同的调用；达到预算或中断后，只额外调用模型一次且不提供工具。

原因：线程超时只能证明 Harness 没拿到结果，不能证明工具没有执行。自动重试非幂等操作可能重复产生副作用。无 Tools 收尾让模型能够诚实总结已完成、失败和未知事项，又不会重新进入工具循环。

### D-006：阶段 4 只保证进程内证据，不冒充崩溃恢复

决定：每个 Tool Result 产生后立即更新内存历史；SQLite 落盘与崩溃恢复留到阶段 5。

原因：这能避免后续 Provider 请求失败时抹掉当前进程已经观察到的副作用证据，但进程突然退出仍会丢失内存。只有增量持久化后才能宣称跨进程可恢复。

### D-007：审计事实和 API 工作历史分离

决定：消息首次写入的 `original_json` 永远保留；恢复器只修改 `message_json` 和 `active`，并把修复原因写入 `recovery_events`。

原因：恢复必须满足 Provider 消息协议，但不能伪装历史从未损坏。孤立 Result、重复 ID 和相邻同角色消息都需要一个可发送的工作视图，同时保留原始证据。

### D-008：Assistant Tool Call 先落盘，Handler 后执行

决定：收到模型 Tool Call 后先提交 Assistant 消息，再进入 Guardrail、审批和 Handler；每个 Tool Result 产生后立即单独提交。

原因：如果先执行工具再保存 Call，崩溃后可能只剩真实副作用却没有任何调用证据。先保存 Call 即使留下缺失 Result，也能在恢复时诚实标为 unknown。

### D-009：Session Resume 不是 Memory

决定：Session 恢复只重放指定会话的消息历史，并沿用该会话创建时的 System Prompt；不自动把事实传播到其他 Session。

原因：Session 解决“继续同一段对话”，Memory 解决“不同会话选择性共享长期事实”。把两者混在一起会导致信息越界和错误注入。

### D-010：事件日志默认只记录元数据

决定：事件包含工具名称、Call ID、错误码、副作用状态、耗时和数量，不包含用户正文、工具参数与 Result 正文。

原因：可观察性不应成为新的秘密泄露面。完整消息证据属于访问受控的 SessionDB；JSONL 只承担运行诊断。

### D-011：故障矩阵验证关系，不冻结实现快照

决定：端到端测试断言“审批拒绝 ⇒ Handler 0 次”“Schema 错误 ⇒ 首次不执行、修正后执行一次”“重复阻断 ⇒ 第一次副作用保留”“缺失 Result ⇒ unknown 且不重放”等行为关系。

原因：这些不变量可以在实现重构后继续成立；冻结事件总数、内部函数调用顺序或 Schema 数量只会形成脆弱的变化探测测试。
