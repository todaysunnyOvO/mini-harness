# Mini Harness

一个用于学习 Harness Engineering 的最小 Python Agent 项目。当前已经完成纯文本对话、Tool Loop，以及预防、纠错、止损、恢复四层工程机制：

```text
用户输入 → 构造消息 → OpenAI-compatible Provider
→ Tool Call → 校验 / 审批 / Guardrail → 本地工具 → Tool Result
→ 模型继续推理 → 最终文本回答
```

当前暴露 `list_files`、`read_file`，并可通过配置启用必须人工审批的
`write_file`。SQLite Session 持久化、Profile 隔离、跨会话 Memory 和
Context Compression v2 已经加入；Skills 已实现轻量索引和按需完整注入，
多 Provider 仍未加入。

当前 Tool 调用在进入 Handler 前会经过：

```text
已注册工具集合中的名称解析
→ JSON Object 解析
→ Draft 2020-12 JSON Schema 校验
→ 无副作用 Preflight
→ 高风险工具人工审批
→ Execution Backend 与 Handler
→ Tool Result 大小预算
→ 结构化 Tool Result
```

工具名自动修复只会选择当前 Registry 中唯一且足够相似的候选；相似度不足或候选含糊时返回 `unknown_tool`，不会猜测新的操作意图。

## 快速开始

需要 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
```

然后修改：

- `.env`：填写 API Key；
- `config.yaml`：填写 Provider 的 `base_url` 和 `model`。

交互模式：

```powershell
.\.venv\Scripts\mini-harness.exe
```

单次调用：

```powershell
.\.venv\Scripts\mini-harness.exe --message "你好"
```

创建或使用指定 Session：

```powershell
.\.venv\Scripts\mini-harness.exe --session my-session --message "记住暗号"
```

严格恢复已存在的 Session：

```powershell
.\.venv\Scripts\mini-harness.exe --resume my-session --message "暗号是什么？"
```

为已有 Session 建立一次持久化压缩边界：

```powershell
.\.venv\Scripts\mini-harness.exe `
  --resume my-session `
  --compress "可选的摘要关注点"
```

列出最近 Session：

```powershell
.\.venv\Scripts\mini-harness.exe --list-sessions
```

列出当前 Skill 索引，以及为单次请求完整加载一个 Skill：

```powershell
.\.venv\Scripts\mini-harness.exe --list-skills
.\.venv\Scripts\mini-harness.exe --skill harness-review --message "审查这个项目"
```

交互模式也支持 `/skills`、`/skill harness-review <任务>`、
`/harness-review <任务>`、`/context` 和 `/compress [关注点]`。

只检查配置、不访问网络：

```powershell
.\.venv\Scripts\mini-harness.exe --check-config
```

运行测试：

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

## 配置原则

- API Key 等秘密只放在环境变量或 `.env`；
- Provider、模型和超时等行为设置放在 `config.yaml`；
- 真实 `.env` 已被 `.gitignore` 排除。

`provider.max_attempts` 和 `provider.retry_backoff_seconds` 控制 Provider 传输层的
有限指数退避。只重试网络、超时、HTTP 408/425/429 和 5xx；认证错误、
请求错误和已收到的非法响应不会重试。

`profile.id` 决定当前用户状态边界，只允许字母、数字、下划线和连字符。
每个 Session 在创建时会把 Profile ID 固化到 SQLite；使用不同 Profile
恢复时会在任何恢复修改发生前拒绝。

`agent.workspace_root` 定义文件工具的边界。绝对路径和逃逸到工作区外的 `..` 路径会在审批前被 Preflight 拒绝；合法路径会先规范化，审批和 Handler 看到同一个 Workspace 相对路径。`write_file` 使用同目录临时文件和原子替换，且每次执行前都要求人工批准。

`tools.auto_repair_names` 和 `tools.name_repair_threshold` 控制保守的名称纠错行为。
`tools.max_result_chars`、`tools.same_call_limit` 和 `tools.timeout_seconds` 分别
控制工具结果预算、相同调用止损和执行超时。`max_calls_per_batch` 与
`max_calls_per_turn` 还会在执行前限制单批次和单轮工具调用；超限批次整批拒绝，
不会只执行前半批。

`context` 配置控制每次 Provider 请求的输入预算：

- `max_input_tokens`：输入与预留输出的总窗口；
- `reserved_output_tokens`：不给输入使用的输出预留；
- `approximate_chars_per_token`：首版确定性近似比例；
- `max_tool_result_tokens`：超出 Recent Tail 后，旧 Tool Result 的单条预算；
- `recent_tool_tail_tokens`：从最新结果向前计算的完整内容保护预算；
- `min_recent_turns`：始终保留的最近完整用户轮次数；
- `compaction_target_ratio`：触发裁剪后降到的预算比例，用余量减少连续
  多轮反复移动缓存前缀。

## 执行后端与超时

Registry 不直接执行 Handler，而是委托给工具选择的执行后端：

- `SpawnProcessExecutionBackend`：Registry 的安全默认后端，使用真实 Windows
  Spawn 子进程；Handler 超时后终止并回收隔离进程；不可序列化的 Handler 会在
  进入 Handler 前返回 `HandlerNotSerializable`，不会偷偷降级到线程；
- `ThreadExecutionBackend`：显式兼容后端，用于必须共享进程内状态或不可序列化的
  Python 闭包。Python 线程无法安全强杀，因此该后端不制造软超时，而是等 Handler
  真正完成后返回；需要硬期限的工具不能选择它。

Spawn 子进程受父进程启动闸门控制。审批通过后，Harness 先把 Journal
持久化为 `running`，成功后才放行 Handler。子进程确认已经进入 Handler
之后，才开始计算工具的超时预算。

`hard_terminated=true` 只表示隔离进程已经停止，不表示超时前必然没有发生
外部副作用。只读工具可把失败声明为 `none`；写工具失败仍保守标记为
`unknown`，Harness 不会因此自动重试。

达到迭代上限或收到协作式中断后，Harness 会停止提供工具，只允许模型做一次总结；此时结果会显示为“部分完成”，不会伪装成正常完成。

`storage.database_path` 指定 SQLite SessionDB，默认是 `.mini-harness/sessions.db`。每条 User、Assistant Tool Call 和 Tool Result 都增量提交。恢复时：

- `prepared` Call 缺少 Result：Journal 证明 Handler 未获准启动，补
  `none / not_executed_after_resume`；
- `running` Call 缺少 Result：补 `unknown` Result，绝不自动重放工具；
- 没有 Journal 的旧版 Call 缺少 Result：保守补 `unknown`；
- 孤立 Result：从活动历史移除，但保留原始审计记录；
- 重复 Tool Call ID：Call 和对应 Result 一起修复；
- System Prompt：继续使用 Session 创建时的版本。

SQLite 使用显式 `user_version` 管理迁移，当前 Schema 为 v5。Assistant Tool Call 与整批
`prepared` Journal 在同一事务落盘；Tool Result 与
`completed/unknown` 终态也在同一事务落盘。Journal 只保存参数哈希和
Call 签名，不保存参数正文；它还分别记录执行后端和是否发生硬终止。

普通 Session 历史仍只属于该 Session；只有经过显式 Memory 更新批准的长期
事实，才会通过 Profile Memory 进入未来的新 Session。

## Profile Memory

启用 `memory.enabled` 后，文件布局为：

```text
memory.root_path/
└── <profile.id>/
    └── memory/
        ├── USER.md
        └── MEMORY.md
```

- `USER.md` 保存稳定偏好；
- `MEMORY.md` 保存环境和长期工作事实；
- `memory_read` 读取其中一个固定文档；
- `memory_update` 执行 append、replace 或 delete，并始终要求人工审批。

模型不能为 Memory 提供任意文件路径。Profile 目录和文档也不能通过符号链接
或 Windows 重解析点跳转到其他 Profile。更新在 Windows Spawn 子进程中
执行，使用独占 token 锁、同目录临时文件、文件 `fsync` 和原子替换；
陈旧锁和硬终止留下的临时文件会在下一次独占更新时恢复。

新 Session 创建时，Harness 将当前 Profile 的 Memory 快照和 revision
冻结进该 Session 的 System Prompt。当前 Session 更新 Memory 后：

```text
磁盘 Memory 更新
→ Tool Result 把新状态告诉当前模型
→ 已有 System Prompt 和历史保持不变
→ 未来新 Session 读取新快照
```

因此 Memory 持久化不会通过回写旧上下文破坏 Prompt Cache。更新和删除可由
Assistant Tool Call、Tool Execution Journal 与 Result 共同审计；JSONL
事件只记录成功状态，不记录 Memory 正文。

## Skills

Skill 来源按以下顺序覆盖：

```text
<skills.profile_root_path>/<profile.id>/skills
→ skills.external_dirs（配置顺序）
→ skills.bundled_dir
→ skills.optional_dir 中由 enabled_optional 显式启用的 Skill
```

启动扫描只读取 `SKILL.md` 的 YAML frontmatter，索引只保留名称、摘要、来源
和文件元数据。Skill 正文不会常驻索引，也没有 `offset/limit` 分页接口。
显式选择后，Harness 才完整读取一份 `SKILL.md`，把正文、不含本机路径的
`skill://<source>/<name>/` 逻辑引用和用户任务组合成当前轮的一条 User 消息。

新 Session 的 System Prompt 会冻结当时的轻量索引；恢复旧 Session 时不会
用磁盘上的新索引覆盖原 Prompt。磁盘正文更新后，后续显式调用读取最新完整
正文，但既有 Session 的 System Prompt 和旧历史保持不变。同名 Skill 的
胜出项和被遮蔽项会分别保留为冲突诊断。

首版刻意使用 CLI 命令，不增加常驻模型 Tool Schema；模型不能自行调用
Hermes 式 `skill_view`。`skill.loaded` 事件只保存名称、来源、字符数和内容
哈希，不保存 Skill 正文或用户任务。

## API 消息边界

Agent 的长期 `conversation_history` 与每次发送给 Provider 的
`api_messages` 已分离。每次请求都会构建一次性深层副本，并在副本上：

- 删除 Provider 不接受的内部字段；
- 清理嵌套 Tool Call 字段并稳定 JSON arguments；
- 删除孤立 Tool Result；
- 为缺失 Result 补充 `unknown` 协议结果，但绝不重放工具；
- 同步修复重复 Tool Call ID；
- 合并相邻 User 消息。

这些操作不会回写 SQLite 或 Agent 的长期消息；公开的 `agent.messages`
也是深拷贝快照。

## Context 预算

Context 预算在 API 消息清理完成后执行，普通消息和当前 Tool Schema 都会
计入。Tool Result 先按新鲜度处理：

```text
保护当前 User Turn 和 Recent Tail 中的新鲜结果
→ 最新重复结果保持完整，旧重复结果改成回指
→ 超限的旧 Tool Result 生成包含工具名、路径、规模和结果状态的信息化摘要
→ 保留其 effect_disposition 与 unknown 禁止重试提醒
→ 删除最旧的完整 User Turn
→ 重新测量
```

裁剪不会切断 Assistant Tool Call 与对应 Tool Result，也不会修改 System
Prompt、SQLite 或长期 `conversation_history`。包含 `unknown` Tool Result
的轮次和最近轮次不会被删除。新鲜大型结果只要总预算允许，就会在执行后的
下一次 Provider 请求中完整交付，而不会先被单条阈值替换。

首次超限时会裁到 `compaction_target_ratio` 指定的目标，而不是刚好卡在
上限。Harness 将已删除轮次的指纹作为当前 Agent 的显式检查点；后续增长
仍在余量内时复用相同边界，以减少 Prompt Cache 反复失效。检查点不包含消息
正文，也不会写回会话历史。

如果这些受保护内容本身仍然超限，原 Provider 请求不会发送。Harness
不会对同一超限历史重复压缩，而是用稳定 System Prompt 和一条最小控制消息
进行一次无 Tools 收尾，并把本轮标记为 `context_budget` 部分完成。

这里的 token 数是可配置的确定性近似值，不是 Provider 官方 tokenizer 的
精确结果，因此需要保留安全余量。

## 持久化 Context Compression

`compression` 配置控制长期 Session 的语义压缩。它与上面的请求副本裁剪
不是同一个机制：

实际问题、根因和方案统一记录在长期维护的
[开发实际问题与解决方案日志](docs/DEVELOPMENT_PROBLEMS_AND_SOLUTIONS.md)。

```text
请求副本裁剪：每次请求临时执行，不改 SQLite
持久化压缩：显式建立一次压缩边界，SQLite 软归档旧活动历史
```

自动压缩默认开启，可用 `compression.enabled: false` 明确退出。只有活动历史
达到 `threshold_ratio` 才尝试；
`target_ratio` 控制摘要后的目标规模。最早完整轮次、最新 token tail 和带
`unknown` Tool Result 的轮次受保护，只对中间完整轮次做摘要。

模型摘要没有工具，并经过输入/输出脱敏。写回时使用
reference-only Summary、固定 Assistant Bridge 和最新真实 User Turn，
避免把历史摘要解释成新任务。Summary 正文不会写入 JSONL 事件。

Schema v5 的 `compression_runs` 保存每次成功或失败的压缩审计；
`compression_state` 保存 Session Lock、Cooldown 和 Anti-thrashing 状态。
成功提交在一个事务中完成：

```text
校验锁和活动消息版本
→ 原活动消息 active=0（原文仍保留）
→ 写入 Head / Summary / Bridge / Tail
→ 写入 committed Run 和 State
→ Commit
```

任何一步失败都会回滚。摘要 Provider 默认失败时不改变活动历史；重复失败或
低收益会暂停自动压缩。再次压缩会滚动合并旧 Summary，活动历史中始终只保留
一份 Summary。手动 `/compress` 仍可在人工判断后执行。

## 结构化事件

`observability.event_log_path` 默认指向 `.mini-harness/events.jsonl`。事件覆盖 Session、Turn、Provider、Tool 和 Recovery 生命周期，只记录诊断元数据：

- 工具名称、Call ID、错误码、副作用状态和执行阶段；
- Provider 阶段、耗时、消息数量和 Tool Call 数量；
- API 工作副本的字段清理和协议修复计数；
- Context 预算、Recent Tool Result 保护、旧结果裁剪、去重和完整轮次裁剪计数；
- Tool Turn 的 requested、executed、blocked、预算占用与剩余调用数；
- Memory 更新是否成功及其副作用状态；
- Turn 完成、部分完成和恢复计数。

日志不会记录用户正文、工具参数和 Tool Result 正文。它是 Best Effort 可观察性，不替代 SQLite 审计记录。

项目计划见 [docs/PLAN.md](docs/PLAN.md)，执行状态见 [docs/PROGRESS.md](docs/PROGRESS.md)。

- [四层故障矩阵](docs/FAULT_MATRIX.md)
- [Mini Harness 与 Hermes 对照](docs/HERMES_COMPARISON.md)
- [后续开发路线](docs/NEXT_DEVELOPMENT_PLAN.md)
- [后续开发进度](docs/NEXT_DEVELOPMENT_PROGRESS.md)
