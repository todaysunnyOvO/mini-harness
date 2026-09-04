# 四层工程兜底故障矩阵

这份矩阵验证的不是某个函数“返回了预期字符串”，而是错误发生时各层之间必须保持的行为关系。

## 端到端矩阵

| 层 | 注入故障 | 必须保持的关系 | 自动化证据 | 结构化事件 |
|---|---|---|---|---|
| 预防 | 高风险工具审批被拒绝 | Handler 调用次数为 0，副作用状态为 `none` | `test_prevention_denied_approval_never_reaches_handler` | `tool.result/error_code=approval_denied` |
| 纠错 | 第一次参数违反 JSON Schema | 第一次不进入 Handler；模型收到错误后第二次修正；Handler 只接收合法值 | `test_correction_model_repairs_schema_error_on_next_iteration` | 两条 `tool.result` 依次为 `schema_validation`、成功 |
| 止损 | 模型重复完全相同的非幂等调用 | 第一次真实执行保留；重复调用被阻止；不伪装成整批未执行，也不回滚第一次 | `test_stop_loss_blocks_repeated_call_without_undoing_first_effect` | `repeated_call_blocked/effect_disposition=none` |
| 恢复 | Assistant Tool Call 已落盘，Result 缺失 | 恢复插入 `unknown` Result；Handler 调用次数为 0；模型看到不确定性 | `test_recovery_injects_unknown_and_never_replays_handler` | `recovery.applied/inserted_unknown_results=1` |

## 其他关键故障

| 故障 | 预期 |
|---|---|
| Spawn Tool 超时 | 终止并回收子进程；写工具返回 `tool_timeout + unknown`，不自动重试 |
| Thread Tool 超时 | 只停止等待并明确提示 Handler 可能仍在运行；不自动重试 |
| 批次中断 | 已完成调用保持真实结果，未开始调用补 `none` Result |
| 迭代耗尽 | 只进行一次 `tools=None` 收尾，并标记部分完成 |
| 孤立 Tool Result | 从 API 工作历史移除，原始数据库记录保持可审计 |
| 重复 Tool Call ID | Call 和对应 Result 一起确定性改名 |
| Provider 在 User 落盘后失败 | User 审计事实保留；下次 API 工作副本合并相邻 User |
| Context 超预算但可裁剪 | 只删除旧的完整轮次；SQLite 历史不变；Tool Call/Result 不拆分 |
| 受保护 Context 仍超预算 | 原请求不发送；只做一次最小无 Tools 收尾，不形成重试循环 |
| Profile 不匹配恢复 | 在协议修复前拒绝；旧 Session 消息与 Journal 不改变 |
| Memory 审批拒绝 | 文件不创建，Handler 不启动，副作用为 `none` |
| Memory 更新崩溃遗留锁/临时文件 | 陈旧 token 锁可恢复；取得独占锁后清理孤立临时文件 |
| 事件日志 | JSONL 每行可解析，不包含用户正文、工具参数或 Tool Result 正文 |

## 运行

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

四层端到端场景集中在：

- `tests/test_fault_matrix.py`
- `tests/test_events.py`
- `tests/test_session_store.py`
- `tests/test_execution_backends.py`
- `tests/test_context_budget.py`
- `tests/test_memory.py`

## 判定原则

```text
预防看“是否进入执行器”
纠错看“错误是否足够具体，让下一步能改变”
止损看“是否停止扩大损失，同时保留已发生事实”
恢复看“是否恢复协议，但不伪造执行结果”
```
