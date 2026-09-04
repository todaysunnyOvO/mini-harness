# Mini Harness 后续开发路线

版本：草案 v1  
日期：2026-07-27

## 1. 文档定位

现有 [PLAN.md](PLAN.md) 记录已经完成的 Mini Harness MVP。本文件定义 MVP 之后的后续开发路线，不修改或重写原验收历史。

目标不是把 Hermes 整体复制一遍，而是继续用最小、可验证的实现学习生产 Harness 中最重要的工程合同。

默认优先级：

```text
正确性与安全边界
→ API 消息与长期历史分离
→ 工具执行审计与隔离
→ Context 预算
→ Memory
→ Skills
```

如果后续目标发生变化，应先修改本文件，再开始代码实现。

---

## 2. 当前基线

MVP 已具备：

- OpenAI-compatible 非流式 Provider；
- Agent Tool Loop；
- Tool Registry 和 Draft 2020-12 JSON Schema；
- `list_files`、`read_file`、需要审批的 `write_file`；
- Workspace 路径边界；
- 名称保守修复；
- 结果预算、超时、重复调用 Guardrail；
- 协作式中断和无 Tools 收尾；
- SQLite Session Resume；
- 缺失 Result 的 `unknown` 恢复；
- 孤立 Result、重复 ID 和相邻 User 修复；
- 脱敏 JSONL 事件；
- 四层工程兜底故障矩阵；
- 95 项自动化测试（阶段 13 完成后）。

已知限制见 [HERMES_COMPARISON.md](HERMES_COMPARISON.md)。

---

## 3. 后续开发原则

### 3.1 先保持事实正确，再扩展能力

任何功能都不能破坏：

```text
Tool Call 先落盘
→ Handler 后执行
→ Result 单独落盘
→ unknown 不自动重放
```

### 3.2 审计事实和 API 工作副本分离

数据库保存真实历史；Provider 适配、协议清洗和 Context 裁剪应作用于每轮 API 副本。

### 3.3 新能力优先放在核心边缘

优先顺序：

```text
扩展现有接口
→ CLI + Skill
→ 条件门控工具
→ Plugin / MCP
→ 新核心工具
```

Mini Harness 不需要为了“看起来完整”持续扩大每轮 Tool Schema。

### 3.4 行为合同优先于实现快照

测试应断言：

```text
非法调用不进入 Handler
未知副作用不被重放
协议修复后 Call/Result 一一对应
日志不泄露正文
```

不冻结工具数量、内部函数调用次数或配置版本常量。

### 3.5 秘密和行为配置分离

- `.env`：API Key、Token、密码；
- `config.yaml`：超时、预算、路径、功能开关。

---

## 4. 目标架构

```text
CLI / Future UI
       │
       ▼
     Agent
       ├── Context Builder
       ├── API Message Builder
       ├── Provider Adapter
       ├── Tool Orchestrator
       │      ├── Registry
       │      ├── Preflight
       │      ├── Approval
       │      └── Execution Backend
       ├── SessionDB
       │      ├── Messages
       │      ├── Tool Execution Journal
       │      └── Recovery Events
       └── Event Sink
```

Agent 继续保持窄腰：只编排状态，不吸收文件、HTTP、Memory 和 Skill 的具体实现。

---

## 5. 阶段 7：工具正确性加固（已完成）

这是下一步正式开发阶段。

### 7.1 Preflight 合同

问题：

当前 `write_file` 的 Workspace Scope 检查位于 Handler 内部，而审批位于 Registry 中：

```text
Schema
→ 审批
→ Handler
→ Scope
```

越界路径最终不会写入，但会先产生一次无意义审批；Handler 启动后抛出的路径错误还会被保守标为 `unknown`，事实精度不足。

目标顺序：

```text
工具名
→ JSON
→ Schema
→ Preflight
→ 审批
→ Handler
```

拟新增：

```python
ToolDefinition(
    ...,
    preflight=callable | None,
)
```

Preflight 允许：

- 解析和规范化路径；
- 检查 Workspace Scope；
- 验证目标类型；
- 返回 Handler 使用的规范化参数；
- 在任何副作用前返回 `effect_disposition=none`。

Preflight 禁止：

- 写文件；
- 发消息；
- 修改数据库业务状态；
- 启动后台任务。

验收：

- 越界 `write_file` 不触发审批；
- Preflight 错误不进入 Handler；
- Preflight 错误的副作用状态为 `none`；
- 审批看到的是规范化后的 Workspace 相对路径；
- 旧的合法写入行为不变。

### 7.2 更精确的执行阶段

为 Tool Result 增加内部阶段：

```text
rejected_before_execution
handler_started
handler_completed
result_unavailable
```

对外仍使用：

```text
none
completed
unknown
```

验收：

- Schema、Preflight、审批拒绝都为 `none`；
- Handler 启动后的异常和超时默认是 `unknown`；
- Handler 可信返回才是 `completed`。

### 7.3 Tool Result 结构统一

统一成功、错误和 Guardrail Result 的元数据：

```json
{
  "effect_disposition": "none|completed|unknown",
  "execution_phase": "...",
  "requested_tool": "...",
  "executed_tool": "...",
  "name_repaired": false
}
```

事件日志继续只记录元数据，不写 arguments 和 Result 正文。

---

## 6. 阶段 8：API Message Builder（已完成）

### 8.1 目标

正式区分：

```text
conversation_history
api_messages
```

`conversation_history`：

- SQLite 中的长期会话事实；
- 保留恢复和审计需要的内部字段。

`api_messages`：

- 当前 Provider 请求的副本；
- 允许进行协议修复、字段清洗和 Context 裁剪；
- 不反向污染长期历史。

### 8.2 工作内容

- 新增 `APIMessageBuilder`；
- 移除 Provider 不接受的内部字段；
- 在副本上补缺失 Result、清理孤立 Result；
- 合并相邻 User；
- 保证 Tool Call/Result 配对；
- 为 Provider Adapter 留出转换接口；
- 为未来 Context Compression 建立唯一入口。

### 8.3 验收

- 构建 API 消息不会修改输入历史；
- 连续构建结果稳定；
- 修复后的消息满足 Tool 协议；
- SessionDB 的 `original_json` 不因 Provider 适配改变；
- System Prompt 在 Session 生命周期中保持字节稳定。

---

## 7. 阶段 9：工具执行日志与幂等边界（已完成）

### 9.1 Tool Execution Journal

在 SQLite 增加工具执行记录：

```text
prepared
→ running
→ completed
或 unknown
```

建议字段：

- Session ID；
- Tool Call ID；
- 工具名；
- 参数哈希，不保存默认明文；
- 幂等键；
- 执行状态；
- `effect_disposition`；
- 开始和结束时间；
- Result Message ID。

### 9.2 重试策略

默认：

```text
任何 unknown 工具都不自动重试
```

只有工具显式声明幂等、且执行后端支持相同幂等键时，才允许后续讨论安全重试。

### 9.3 验收

- Call、Journal 和 Result 可以互相追踪；
- 崩溃在 `prepared`、`running`、`completed` 各阶段都有明确恢复语义；
- 非幂等工具没有隐式自动重试路径；
- 参数正文不进入普通事件日志。

---

## 8. 阶段 10：执行后端与硬超时（已完成）

当前 ThreadPool 超时只停止等待，不能终止已经运行的 Python 线程。

已引入：

```python
ExecutionBackend
├── ThreadExecutionBackend
└── SpawnProcessExecutionBackend
```

### 8.1 第一版范围

- 保留线程后端作为兼容实现；
- 为可序列化的内置文件工具增加子进程后端；
- 超时后终止子进程；
- 区分“进程被终止”和“外部副作用仍未知”；
- 中断取消未开始任务。

### 8.2 验收

- 卡死的纯计算 Handler 不会永久占用主进程；
- 超时后主循环可以继续收尾；
- 已产生外部副作用的状态仍不会被错误标为 `none`；
- Windows 环境使用真实 Spawn 路径测试。

### 8.3 实际落地

- Registry 只负责编排，由每个工具选择执行后端；
- Thread 后端保留不可序列化闭包的兼容性，但超时只停止等待；
- Spawn 后端以父进程启动闸门保证 Journal 先进入 `running`；
- 子进程确认进入 Handler 后，才开始计算 Handler 超时预算；
- 超时后依次 `terminate`、必要时 `kill`，并回收子进程；
- `hard_terminated` 与 `effect_disposition` 分开记录；
- 只读文件工具声明失败副作用为 `none`，写工具失败保守为 `unknown`；
- SQLite Schema v3 持久化执行后端与硬终止事实；
- 8 项新增测试、67 项全量回归、真实 Spawn 读写和 v2→v3 迁移通过。

---

## 9. 阶段 11：Context 预算（已完成）

工作内容：

- 消息字符和近似 Token 预算；
- Tool Result 独立预算；
- API Message Builder 中的裁剪策略；
- 压缩前后保留最近未完成 Tool 批次；
- 压缩失败冷却；
- 压缩事件和审计。

验收：

- 不切断 Tool Call/Result 配对；
- 不丢失 unknown 副作用提醒；
- 压缩失败不会形成重试循环；
- System Prompt 和固定前缀保持稳定。

### 9.1 实际落地

- `ContextBudgetPolicy` 明确输入上限、输出预留、近似字符/token 比例、
  单条 Tool Result 上限和最近轮次数；
- 普通消息和当前 Tool Schema 一起计入预算；
- 旧的大型 Tool Result 替换为结构化压缩占位，保留
  `effect_disposition`；
- `unknown` 结果继续携带禁止自动重试提醒；
- 只删除完整 User Turn，不切断 Assistant Tool Call 与 Tool Result；
- System Prompt、最近轮次和包含 unknown 的轮次受保护；
- 首次超限裁到目标比例，并显式复用裁剪检查点，避免每轮移动缓存边界；
- 保护内容仍超预算时，原请求不发送，只执行一次最小无 Tools 收尾；
- 预算与裁剪事件只记录估算和计数，不记录消息正文；
- SQLite、长期 `conversation_history` 和公开消息快照均不回写；
- 8 项新增测试、75 项全量回归和真实长会话 Provider 验证通过。

---

## 10. 阶段 12：Memory（已完成）

Memory 只在 Session 恢复稳定后加入。

第一版范围：

- `USER.md`：稳定用户偏好；
- `MEMORY.md`：环境和长期工作事实；
- 显式 Memory Tool 做增删改查；
- 新 Session 启动时读取；
- 当前 Session 中磁盘更新不改写已经发送的历史前缀；
- Memory Tool Result 附带更新后的必要状态。

不做：

- 自动向量数据库；
- 无审计的模型自主抽取；
- 跨用户共享；
- 远程 Memory Provider。

验收：

- 新 Session 能读取已保存长期事实；
- 当前 Session 的历史不被回写；
- 不同用户或 Profile 不串 Memory；
- 删除和修改都有审计记录。

### 10.1 实际落地

- `profile.id` 使用受限短标识，不能包含路径逃逸字符；
- SQLite Schema v4 将 `profile_id` 固化到每个 Session；
- 恢复前先只读检查 Profile，不匹配时不执行协议修复；
- Memory 固定为 `<root>/<profile>/memory/USER.md|MEMORY.md`；
- Profile 目录和文档拒绝符号链接/Windows 重解析点跳转；
- append/replace 使用 token 锁、同目录临时文件、`fsync` 和原子替换；
- delete、锁超时、陈旧锁和孤立临时文件均有明确恢复语义；
- `memory_read` 与需审批的 `memory_update` 只在 Memory 启用时注册；
- 两个 Memory 工具使用 Windows Spawn 执行后端；
- Memory 快照只在新 Session 创建时进入稳定 System Prompt；
- 当前 Session 更新通过 Tool Result 获知，不回写旧前缀；
- Call、Journal、Result 和脱敏事件共同提供更新/删除审计；
- 10 项新增测试、85 项全量回归、真实 v3→v4 和跨 Session 验证通过。

---

## 11. 阶段 13：Skills（已完成）

第一版范围：

- 外部、内置、可选 Skills 目录；
- 启动时只构建 Skill 索引；
- 使用时完整读取 `SKILL.md`；
- Skill 内容作为当前 User 工作消息进入，而不是重建 System Prompt；
- 禁止对教学型 `SKILL.md` 做分页懒读；
- Skill 命令不增加常驻核心 Tool Schema。

验收：

- 未使用 Skill 只消耗索引 Token；
- 选择 Skill 后完整读取正文；
- Skill 更新不修改旧会话 System Prompt；
- 缺失 Skill 返回明确错误。

### 11.1 实际落地

- `SkillCatalog` 按 Profile 本地/显式外部、内置、已启用可选的顺序发现；
- 同名时先发现者胜出，其他候选保留为冲突诊断；
- 启动索引只读取 bounded YAML frontmatter，不保存正文；
- `load(name)` 只有完整读取语义，不提供 `offset` 或 `limit`；
- 选中后生成 `SkillInvocation`，通过 `Agent.chat_with_skill()` 作为当前 User
  消息进入；
- `--list-skills`、`--skill NAME --message`、`/skills`、`/skill NAME` 和
  `/<skill-name>` 已接入；
- 新 Session 冻结索引，Resume 继续使用 SQLite 中原始 System Prompt；
- 路径逃逸、嵌套支持 Skill、超限正文、同名覆盖、正文热更新和事件脱敏均有
  测试；
- 10 项新增测试、95 项全量回归和真实 Provider Skill 调用通过。

---

## 12. 暂不进入范围

下一轮开发暂不实现：

- Messaging Gateway；
- Browser / Computer Use；
- Subagent；
- Cron；
- MCP Catalog；
- Plugin Marketplace；
- 多用户权限系统；
- 对外遥测；
- 完整 Hermes Provider 矩阵。

这些能力只有在当前阶段出现具体消费者后再设计接口。

---

## 13. 数据库迁移原则

后续新增 Journal 或 Context 状态时：

- 使用显式 Schema Version；
- 每个迁移可重复检测；
- 先备份或在临时数据库验证；
- 不删除原始消息；
- 迁移测试必须从旧版数据库启动；
- 失败后数据库保持可再次打开。

---

## 14. 测试策略

每个阶段至少包含：

1. 单元测试；
2. 真实模块导入的临时目录 E2E；
3. 一个故障注入场景；
4. 一个“不应发生副作用”的断言；
5. 一个事件脱敏断言；
6. 完整历史回归。

关键测试不能只 Mock 掉文件或 SQLite 路径。

---

## 15. 下一执行里程碑

阶段 7—13 的计划内路线已经完成。下一次扩展前先新增具体消费者和开发计划，
不直接进入 Plugins、MCP、Subagent 或多 Provider：

```text
明确真实需求
→ 在 Footprint Ladder 上选择最小接入面
→ 更新开发计划与验收合同
→ 再开始实现
```

阶段 12 已于 2026-07-27 验收：Session/Profile 绑定、原子 Memory 文件、
显式审批工具、冻结快照和跨 Session 可见性全部落地。Schema v4 迁移后
旧 Session 归属 `default` Profile；85 项测试、真实 Provider 写入、读取、
删除和事件脱敏均通过。阶段 13 已于同日验收：95 项测试、真实
`harness-review` 调用和索引/正文持久化边界均通过，且未扩展到 Plugins 或
远程 Skill 市场。
