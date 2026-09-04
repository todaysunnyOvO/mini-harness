# Mini Harness 后续开发进度报告

报告日期：2026-07-29  
对应路线：[NEXT_DEVELOPMENT_PLAN.md](NEXT_DEVELOPMENT_PLAN.md)

## 1. 当前状态

```text
MVP 状态：已完成
后续开发状态：阶段 7—14 全部完成
当前阶段：计划内开发已验收
最近完成里程碑：阶段 14 Context Compression v2
后续开发总体进度：100%
```

## 2. 基线验证

```text
python -m compileall -q src tests                       PASS
python -m unittest discover -s tests -v                PASS（113/113）
真实文本 Provider                                       历史 PASS
真实 read_file Tool Loop                               历史 PASS
真实审批 + Preflight 规范化 write_file                  PASS
真实 APIMessageBuilder + 跨进程 Session Resume         PASS
真实旧库 v0 → v2 迁移 + Journal                        PASS
真实旧库 v2 → v3 迁移 + Windows Spawn                  PASS
真实 Spawn read_file + 审批 write_file                 PASS
真实长会话 Context 裁剪 + Provider                     PASS
真实旧库 v3 → v4 Profile 迁移                          PASS
真实审批 Memory 写入 + 跨 Session 快照 + 删除           PASS
真实脱敏事件链                                         历史 PASS
真实 Skill 索引 + 完整 User 注入                        PASS
真实旧库 v4 → v5 Context Compression 迁移              PASS
真实 UTF-8 多轮模型摘要 + Resume 稳定性                  PASS
```

阶段 7 新增 6 项测试；阶段 8 新增 6 项 API 消息不可变性、字段清理、
协议修复和事件脱敏测试，并加固公开消息快照的深拷贝边界。50 项自动化测试全部通过。

阶段 9 新增 9 项旧库迁移、失败回滚、Journal 状态机、崩溃恢复和重复 ID
联动测试。真实数据库从 v0 迁移到 v2 后，原 4 个 Session 全部保留；
新增真实 `read_file` Session 后 `integrity_check=ok`，Journal 的 Call、
Result、哈希和终态关联完整。迁移前备份保存在
`.mini-harness/sessions.pre-schema-v2.db`。

阶段 10 新增 8 项执行后端、Windows Spawn、启动闸门、硬超时和 Journal
元数据测试。真实数据库从 v2 迁移到 v3 后，原 5 个 Session 全部保留；
真实 `read_file` 与经审批的 `write_file` 均在 `spawn_process` 后端完成，
`integrity_check=ok`。迁移前备份保存在
`.mini-harness/sessions.pre-schema-v3.db`。当前共 67 项测试。

阶段 11 新增 8 项预算配置、成本测量、完整轮次裁剪、Tool 批次配对、
Tool Result 压缩、受控收尾和 SQLite 审计隔离测试。真实长会话验证中，
Provider 最终只收到 4 条消息，最旧标记未发送，SQLite 仍保留完整历史；
共裁掉 9 个完整轮次，估算输入降至 1322 tokens，真实 Provider 正常回答。
当前共 75 项测试。

阶段 12 新增 10 项 Profile 配置、Schema v4 迁移、目录隔离、原子 CRUD、
锁超时/崩溃恢复、审批、跨 Session 快照和脱敏审计测试。真实数据库从 v3
迁移到 v4 后，原 7 个 Session 全部绑定 `default` Profile 且完整保留；
真实审批写入后，新 Session 从冻结 System Prompt 直接读到测试 Memory，
原写入 Session 的前缀没有被回写。随后真实审批删除测试 Memory，事件日志
未出现正文，`integrity_check=ok`。迁移前备份保存在
`.mini-harness/sessions.pre-schema-v4.db`。当前共 85 项测试。

阶段 13 新增 10 项轻量索引、来源覆盖、可选激活、完整读取、路径逃逸、
嵌套支持目录、超限拒绝、Context 成本、旧 Session Prompt 冻结和事件脱敏
测试。真实 `harness-review` 调用返回 `SKILL_OK`；SQLite 证明 System
Prompt 只有索引、User 消息才有完整正文，JSONL 的 `skill.loaded` 只记录
元数据。当前共 95 项测试。

## 3. 后续阶段看板

| 阶段 | 主题 | 状态 | 进度 |
|---:|---|---|---:|
| 7 | 工具正确性加固 | 已完成 | 100% |
| 8 | API Message Builder | 已完成 | 100% |
| 9 | Tool Execution Journal | 已完成 | 100% |
| 10 | 执行后端与硬超时 | 已完成 | 100% |
| 11 | Context 预算 | 已完成 | 100% |
| 12 | Memory | 已完成 | 100% |
| 13 | Skills | 已完成 | 100% |
| 14 | Context Compression v2 | 已完成 | 100% |

阶段 14 的完整任务、失败测试、迁移备份和真实模型证据见
[CONTEXT_COMPRESSION_V2_PROGRESS.md](CONTEXT_COMPRESSION_V2_PROGRESS.md)。

## 4. 阶段 7 任务分解

| 任务 | 状态 | 验收证据 |
|---|---|---|
| 设计 `Preflight` 输入输出合同 | 已完成 | 纯函数式 Mapping 输入输出；异常 fail-closed |
| `ToolDefinition` 增加 Preflight | 已完成 | 可选字段；旧工具无需修改 |
| Registry 在审批前执行 Preflight | 已完成 | 顺序测试通过 |
| 文件 Scope 迁移到 Preflight | 已完成 | 越界路径 Approval=0、Handler=0 |
| Handler 接收规范化参数 | 已完成 | 审批与 Handler 均收到规范化参数 |
| 增加 `execution_phase` 元数据 | 已完成 | Result 与脱敏 Event 均可观察 |
| 新增故障测试 | 已完成 | Preflight 异常、非法返回、Schema 重校验均覆盖 |
| 完整回归 | 已完成 | 38 项历史测试 + 6 项新测试全绿 |

## 5. 阶段 8 完成清单

| 任务 | 状态 | 验收证据 |
|---|---|---|
| 新增纯函数式 `APIMessageBuilder` | 已完成 | 相同输入连续构建结果稳定 |
| 深层复制 API 工作消息 | 已完成 | Provider 修改输入不污染 Agent 历史 |
| Provider 字段白名单 | 已完成 | 内部字段与嵌套 Tool Call 字段被清理 |
| Tool Call/Result 最终配对 | 已完成 | 缺失 Result 补 `unknown`，孤立 Result 删除 |
| 重复 Tool Call ID 修复 | 已完成 | Call 和对应 Result 在副本中同步改名 |
| 相邻 User 合并 | 已完成 | 只合并 API 副本，原输入保持不变 |
| 可观察性 | 已完成 | 只记录修复计数，不记录消息正文 |
| 公开历史快照隔离 | 已完成 | 嵌套对象也是深拷贝 |
| 完整回归与真实 Provider | 已完成 | 50/50；真实两轮 Resume 通过 |

## 6. 阶段 9 完成清单

| 任务 | 状态 | 验收证据 |
|---|---|---|
| 显式 Schema Version | 已完成 | SQLite `user_version=2` |
| 旧版数据库识别 | 已完成 | 无版本但具备核心表时按 v1 迁移 |
| 事务化、幂等迁移 | 已完成 | 重复打开不重复迁移 |
| 迁移失败回滚 | 已完成 | 故障注入后旧数据与完整性均保留 |
| Tool Call + prepared 原子落盘 | 已完成 | 同一 SQLite 事务 |
| Handler 启动边界 | 已完成 | 审批后、Handler 前转为 `running` |
| Result + 终态原子落盘 | 已完成 | Result Message ID 与 Journal 关联 |
| 参数隐私 | 已完成 | 只存参数哈希和 Call 签名，不存参数正文 |
| prepared 崩溃恢复 | 已完成 | `none`，明确未执行 |
| running 崩溃恢复 | 已完成 | `unknown`，绝不自动重放 |
| 重复 ID 联动修复 | 已完成 | Call、Result、Journal 同步修复 |
| 完整回归与真实验证 | 已完成 | 59/59；真实旧库与 Provider 通过 |

## 7. 阶段 10 完成清单

| 任务 | 状态 | 验收证据 |
|---|---|---|
| 最小 `ExecutionBackend` 合同 | 已完成 | Registry 不再直接拥有线程池 |
| Thread 兼容后端 | 已完成 | 闭包 Handler 仍可显式运行；等待完成，不制造无法兑现的软超时 |
| Windows Spawn 后端 | 已完成 | 使用真实 `spawn`，不依赖 `fork` |
| 父进程启动闸门 | 已完成 | Journal 写入 `running` 失败时 Handler 不进入 |
| Handler 启动确认 | 已完成 | 超时预算从子进程确认进入 Handler 后开始 |
| 硬超时终止 | 已完成 | 超时子进程退出，延迟文件副作用未发生 |
| 副作用语义分离 | 已完成 | `hard_terminated` 不等于 `effect_disposition=none` |
| 内置文件工具迁移 | 已完成 | list/read/write 使用可终止 Spawn 后端 |
| Journal v3 元数据 | 已完成 | 持久化后端名、硬终止标记和错误码 |
| 完整回归与真实验证 | 已完成 | 67/67；真实 Spawn 读写与 v2→v3 迁移通过 |

## 8. 阶段 11 完成清单

| 任务 | 状态 | 验收证据 |
|---|---|---|
| 确定性近似 Token 估算 | 已完成 | 消息与 Tool Schema 均计入；相同输入结果稳定 |
| 输出预算预留 | 已完成 | `max_input_tokens - reserved_output_tokens` |
| Tool Result 独立预算 | 已完成 | 超限正文替换为带副作用事实的压缩占位 |
| 完整轮次裁剪 | 已完成 | 只删除完整 User Turn，不切断中间消息 |
| Tool 协议保护 | 已完成 | 多 Call 与全部 Result 始终一起保留或删除 |
| unknown 事实保护 | 已完成 | 保留 `effect_disposition=unknown` 与禁止自动重试提醒 |
| 固定前缀保护 | 已完成 | System Prompt 字节内容不修改 |
| 缓存边界冷却 | 已完成 | 裁到目标比例；增长未耗尽余量时复用同一检查点 |
| 预算失败止损 | 已完成 | 原超限请求不发送；只做一次最小无 Tools 收尾 |
| 长期历史隔离 | 已完成 | SQLite 和 `agent.messages` 不被裁剪回写 |
| 脱敏事件 | 已完成 | 只记录 token 估算与裁剪计数，不记录正文 |
| 完整回归与真实验证 | 已完成 | 75/75；真实长会话裁剪与 Provider 通过 |

## 9. 阶段 12 完成清单

| 任务 | 状态 | 验收证据 |
|---|---|---|
| Profile ID 合同 | 已完成 | 安全短标识；拒绝路径逃逸字符 |
| Session/Profile 持久绑定 | 已完成 | Schema v4；不匹配时在恢复修改前拒绝 |
| Profile 目录隔离 | 已完成 | `<root>/<profile>/memory`；拒绝链接/重解析跳转 |
| `USER.md` 与 `MEMORY.md` | 已完成 | 固定文档名，不接受模型提供路径 |
| 原子写入 | 已完成 | 同目录临时文件、文件 `fsync`、原子替换 |
| 并发/崩溃锁 | 已完成 | 独占 token 锁、超时、陈旧锁和临时文件恢复 |
| 显式 Memory 读取 | 已完成 | `memory_read`，只读 Spawn 后端 |
| 显式 Memory 更新 | 已完成 | append/replace/delete；审批后 Spawn 执行 |
| 更新后状态 | 已完成 | Result 返回文档、内容、字符数和 revision |
| Session 快照冻结 | 已完成 | 创建时进入 System Prompt；当前 Session 不回写 |
| 新 Session 可见更新 | 已完成 | 真实 Provider 直接读取跨 Session 测试标记 |
| 审计与脱敏 | 已完成 | Call/Journal/Result 可追踪；事件无 Memory 正文 |
| 完整回归与真实验证 | 已完成 | 85/85；v3→v4、真实写入/读取/删除通过 |

## 10. 阶段 13 完成清单

| 任务 | 状态 | 验收证据 |
|---|---|---|
| 三类来源 | 已完成 | Profile 本地/外部 → bundled → enabled optional |
| 同名覆盖 | 已完成 | 先发现者胜出；shadowed 候选保留冲突诊断 |
| 轻量索引 | 已完成 | bounded frontmatter；Index Entry 无正文 |
| 可选 Skill 门控 | 已完成 | 未列入 `enabled_optional` 时不激活 |
| 完整读取 | 已完成 | 无 offset/limit；过大则整份拒绝 |
| User 消息注入 | 已完成 | `Agent.chat_with_skill()`；System 不重建 |
| CLI 命令 | 已完成 | list、单次、交互和动态斜杠命令 |
| Prompt Cache 边界 | 已完成 | Resume 保留原 System Prompt |
| 审计脱敏 | 已完成 | 名称/来源/字符数/hash，无正文 |
| 完整回归与真实验证 | 已完成 | 95/95；真实 Provider 返回 `SKILL_OK` |

## 11. 问题状态

### 已解决 P0：Workspace Scope 晚于审批

原来：

```text
Schema
→ 审批
→ Handler
→ Scope
```

影响：

- 越界写入不会真的发生；
- 但用户会收到一次没有必要的审批；
- Scope 错误发生在 Handler 启动后，当前统一标为 `unknown`，语义过于保守。

现在：

```text
Schema
→ Preflight Scope
→ 审批
→ Handler
```

### 已解决 P0：ToolInputError 副作用分类不够精确

Registry 无法知道 Handler 在抛出 `ToolInputError` 前是否产生了副作用，所以统一使用 `unknown`。

已实现：

- 无副作用输入校验移到 Preflight；
- Handler 启动后的异常继续保守为 `unknown`。
- Preflight、Schema 和审批拒绝标记为
  `rejected_before_execution + none`；
- Handler 正常返回标记为 `handler_completed + completed`；
- Handler 启动后的异常或超时标记为
  `result_unavailable + unknown`。

### 已解决 P1：长期历史和 API 工作消息尚未完整分层

现在每次 Provider 调用都先从 `conversation_history` 构建一次性
`api_messages`。字段清理、最终协议配对和相邻 User 合并都发生在副本中。

SQLite 恢复器仍可持久记录“崩溃后结果未知”等恢复事实；这是长期状态恢复，
不是 Provider 适配。两层不再共用同一个可变消息对象。

### 已解决 P1：线程超时不能终止 Handler

Registry 默认使用 Windows Spawn 子进程后端；超时会终止隔离进程。不可序列化
Handler 在入口前失败，不会静默退回线程。Thread 后端仍作为不可序列化 Handler
的显式兼容实现；因为不能终止 Python 线程，它等待 Handler 完成后才返回，不再
制造“调用已超时但工作仍在后台继续”的分裂状态。

硬终止只证明该执行单元不再继续运行，不能抹掉超时前可能已经发生的外部
副作用。因此写工具超时仍为 `unknown`，不会自动重试。

### 已解决 P1：数据库尚无显式迁移版本

数据库使用 `PRAGMA user_version` 管理版本。v0 旧库迁移、重复启动和迁移失败
回滚均有真实 SQLite 测试；Journal Schema 与版本更新位于同一事务。

### 已解决 P2：尚无 Context 预算

Context 预算已接入 `APIMessageBuilder`。裁剪只作用于一次性
`api_messages`，不会改写 SQLite、`conversation_history` 或 System Prompt。
当前采用可解释的字符/token 近似值，不声称等于 Provider 的精确 tokenizer。

### 已解决 P2：尚无跨会话 Memory

Memory 已按 Profile 隔离。Session 创建时只读取一次快照并将 Profile ID
写入数据库；恢复时配置 Profile 不一致会在任何恢复修复前拒绝。
Memory 更新只影响磁盘、当前 Tool Result 和未来 Session。

### 已解决 P2：尚无 Skills

Skills 已按“摘要发现、全文执行”分层实现。首版通过 CLI 显式激活，不增加
常驻模型 Tool Schema，也不实现远程市场或自动安装。

## 12. 风险登记

| 风险 | 影响 | 应对 |
|---|---|---|
| Preflight 偷偷产生副作用 | 审批边界失效 | 合同明确禁止；测试 Handler 和文件状态 |
| 参数规范化改变模型原始调用证据 | 审计失真 | 原 arguments 保留，执行参数单独记录 |
| API Builder 修改长期历史 | Prompt Cache 与审计受损 | 纯函数复制，输入不变测试 |
| Journal 引入数据库迁移失败 | Session 无法恢复 | 旧库迁移 E2E、显式版本、失败保护 |
| 子进程后端在 Windows 不一致 | 超时与序列化失败 | 已用真实 Spawn 读写与硬超时验证；闭包保留 Thread 兼容路径 |
| 近似 Token 与 Provider tokenizer 有偏差 | 请求仍可能接近真实上限 | 预留输出空间；所有估算事件可观察；配置允许保守调整 |
| Memory 跨 Profile 泄漏 | 用户长期状态串线 | Session 持久绑定 Profile；目录固定；恢复前校验 |
| Memory 更新中崩溃 | 文档撕裂或永久锁死 | 原子替换；token 锁；陈旧锁与临时文件恢复 |
| Skills 增长核心 Tool Schema | 每轮 Token 增加 | CLI 显式激活；未增加 Skill Tool Schema |
| Skill 正文过大或只读第一页 | 指令不完整、执行偏差 | 设完整读取上限；超限整份拒绝，不提供分页 |
| 同名 Skill 来源不明确 | 执行错误版本 | 固定优先级；冲突诊断记录 winner/shadowed |

## 13. 设计决策

### ND-001：先加固，再扩能力

决定：Preflight、API Message Builder 和执行日志优先于 Memory、Skills。

原因：在执行边界和恢复事实还不够精确时扩展能力，只会扩大错误影响面。

### ND-002：不以复制 Hermes 为目标

决定：只迁移能够通过最小故障案例解释和验证的机制。

原因：Hermes 的复杂度来自大量 Provider、平台和长期运行场景；无消费者的接口属于推测性基础设施。

### ND-003：Preflight 必须无副作用

决定：Preflight 只能验证、解析和规范化，不允许修改外部状态。

原因：它位于审批之前；一旦允许副作用，审批就不再是执行闸门。

### ND-004：unknown 默认不可重试

决定：新增 Journal 后仍保持 unknown 不自动重试。

原因：日志能提高可观察性，但不能把未知业务结果变成可安全重放。

### ND-005：借鉴 Hermes 的分层，不复制通用中间件

决定：参考 Hermes 将请求改写/阻断与真实执行分开的边界，在 Mini Harness
中实现每工具可选、无副作用的 `preflight`，暂不引入通用 Middleware
注册系统。

原因：当前具体消费者只有文件路径解析与 Scope 校验。最小接口已经解决
审批顺序和副作用分类问题；此时增加插件式中间件会形成无消费者的扩展面。

### ND-006：持久恢复与 API 修复是两层

决定：SessionDB 继续持久记录崩溃、缺失 Result 和审计修复事实；
`APIMessageBuilder` 只负责每次请求前的 Provider 安全副本。

原因：把崩溃后 `unknown` 只做成临时文本会丢失恢复事实；把 Provider
字段清理写回 SQLite 又会污染长期历史。两层都需要，但生命周期不同。

### ND-007：Journal 的两个原子边界

决定：

```text
Assistant Tool Call + prepared Journal
Result Message + terminal Journal
```

分别使用一个 SQLite 事务。`running` 必须在审批和 Preflight 通过之后、
Handler 提交之前持久化。

原因：只有这样，`prepared` 才能证明 Handler 没有获准启动；`running`
则表示 Handler 可能已经启动，崩溃后必须保守标记 `unknown`。

### ND-008：执行单元终止与业务副作用是两个事实

决定：执行后端负责进程生命周期；Journal 分别记录
`execution_backend`、`hard_terminated` 和 `effect_disposition`。

原因：成功终止子进程只能阻止它继续运行，不能证明它在终止前没有写文件、
发消息或调用远端服务。只有工具按自身语义显式声明失败无副作用时，失败结果
才能为 `none`；非幂等工具默认仍为 `unknown`，且不自动重试。

### ND-009：Context 裁剪只改 API 副本，并按语义单元执行

决定：首版不调用模型摘要，而是确定性压缩旧 Tool Result、按完整 User Turn
删除旧上下文；System Prompt、最近轮次、Tool Call/Result 配对和 unknown
副作用事实受保护。

原因：模型摘要会引入额外调用、失败重试和语义伪造风险。确定性裁剪更容易
审计；当保护内容仍超预算时，Harness 不发送原请求，也不重复压缩，而是只做
一次最小无 Tools 收尾。

### ND-010：Session 必须持久绑定 Profile

决定：Schema v4 在 `sessions` 表保存 `profile_id`；恢复时必须先只读检查
Profile，再执行任何协议恢复。

原因：只把 Profile 放在当前配置里，会允许旧 Session 在切换配置后操作另一
份 Memory。即使 System Prompt 仍是旧快照，Memory 工具也会产生跨 Profile
读写，这是必须在会话边界阻断的问题。

### ND-011：Memory 更新不回写当前 Session 前缀

决定：Memory 只在新 Session 创建时冻结进 System Prompt。当前 Session
更新后通过 Tool Result 得知新状态，但已有 System Prompt 和历史不修改。

原因：回写历史会破坏 Prompt Cache 和审计真实性。磁盘是未来 Session 的
长期状态，Tool Result 是当前 Session 的增量事实，两者生命周期不同。

### ND-012：Skill 摘要与正文使用不同生命周期

决定：新 Session 的稳定 System Prompt 只冻结 Skill 元数据索引；显式选中后
才完整读取 `SKILL.md`，并把它作为当前 User 工作消息提交。

原因：所有 Skill 正文常驻会造成 token 爆炸；中途重建 System Prompt 会破坏
Prompt Cache；分页又可能让模型只读一部分关键指令。完整按需注入同时守住了
成本、缓存和教学指令完整性。

## 14. 下一执行点

阶段 7—14 已完成。后续若继续扩展，先为具体需求新建计划，不默认进入更大的
能力面：

```text
具体消费者
→ 最小接入面
→ 行为合同与失败测试
→ 实现
```

## 15. 阻塞项

当前无技术阻塞。
