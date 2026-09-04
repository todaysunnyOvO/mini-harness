---
name: harness-review
description: Review an agent harness by tracing state, model, tool, recovery, and audit boundaries.
---

# Harness Review

Use this skill when reviewing an agent harness design or implementation.

Work in this order:

1. Identify the durable conversation state and the per-request API message copy.
2. Trace one complete model loop: request, assistant response, tool preflight,
   execution, tool result, and final answer.
3. At every boundary, state what may mutate, what is persisted, and what can
   safely be retried.
4. Check the four engineering fallback layers:
   prevention, execution control, recovery, and observability.
5. Verify that unknown execution outcomes remain unknown rather than being
   rewritten as success or failure.
6. Check prompt-cache stability: old messages and the Session system prompt
   must not be rebuilt during an ordinary turn.
7. Finish with concrete evidence: file names, tests, observed events, and any
   remaining limitation.

Do not call a feature safe merely because its happy-path unit test passes.
Prefer a real temporary-directory or database path for integration checks.
