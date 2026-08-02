# pico 三角色 Delegate + Harness 设计文档

> 定位：保留原生 Pico 行为，把新增三角色协同放入可选 LangGraph wrapper。
> 目标：在不改变 native delegate 合同的前提下，体现多 agent 编排、受限沙盒、可观测性和 eval harness。

---

## 1. 当前事实

`pico/tools.py` 里已经有 `delegate` 工具：

```python
DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}
```

当前参数只有：

| 参数 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `task` | `str` | 无 | 子 agent 要执行的调查任务 |
| `max_steps` | `int` | `3` | 子 agent 最大模型/工具轮数 |

当前校验逻辑：

- `task` 不能为空
- `context.depth >= context.max_depth` 时拒绝委派
- `delegate` 只有在 `context.depth < context.max_depth` 时才会暴露给模型

当前 native `spawn_delegate()` 已经实现：

- 子 agent 复用同一个 model client
- 子 agent 复用同一个 workspace / session store / run store
- 子 agent 使用 `approval_policy="never"`
- 子 agent 使用 `read_only=True`
- 子 agent 步数更少
- 子 agent 通过 `history_text()` 获得父 agent 的少量上下文

这些已经构成了一个轻量的分层 agent 基础。后续不需要改造 native 的公开工具，而是在 wrapper 中复用
workspace、model client、sandbox 和生命周期基础设施，补成有角色、有输入契约、有指标的协作闭环。

---

## 2. 设计目标

本轮目标不是引入复杂的 4 节点群聊，而是形成一个清楚、可测试、可面试解释的三角色结构：

**三角色 = Coordinator（调度 + 执行） + Research delegate + Review delegate**

```text
Coordinator（调度 + 执行）
    |
    |-- Research delegate  只读调查
    |
    |-- Review delegate    只读审查
```

> 注意：下文偶尔出现的 "Executor" 不是第四个独立角色，而是 Coordinator 自身使用写入工具执行修改的那部分职责。native 后端由同一个 Pico 实例完成调度和执行；LangGraph 后端为隔离 graph-level TaskState 会创建 executor Pico，但它只读取父 session 的隔离快照并返回结构化执行结果，因此仍是同一个逻辑角色，不是独立 agent 或独立进程。

其中：

- `Coordinator` 在 LangGraph wrapper 中负责 plan/execute/finalize；native `Pico + AgentLoop` 仍只执行原有 loop
- `Research delegate` 负责找证据、定位文件、提出建议
- `Review delegate` 负责按验收标准挑毛病
- Harness 负责把流程固定任务化、指标化、可复现

核心原则：

1. 不把 LangGraph 作为主框架。
2. 不把 Kafka / OpenTelemetry / Docker sandbox 做成主依赖。
3. 不重复实现已有 sandbox 能力。
4. 不把不可序列化的 `Pico` 实例塞进 LangGraph state。
5. native 只作为兼容 baseline；三角色协同只在 LangGraph wrapper 中启用。

---

## 3. 三个角色

### 3.1 Coordinator

Coordinator 是 LangGraph wrapper 中的逻辑角色，使用 Pico runtime 完成 plan/execute/finalize。
native `Pico.ask()` 仍由原有 `AgentLoop` 独立运行，不自动进入这套三角色流程。

职责：

- 理解 benchmark task
- 按已校验的 `requires_research` 决定图路由
- 根据 research 结果执行修改
- 汇总工具执行 metadata
- 构造 review packet
- 调用 wrapper 内部的 review role child
- 根据 review 结果决定继续修复或返回最终答案

允许工具：

- 保持现有主 agent 工具集
- 仍受 `allowed_tools`、approval policy、path sandbox、shell timeout、secret redaction 约束

### 3.2 Research Delegate

Research delegate 是只读子 agent。

职责：

- 调查项目结构
- 搜索相关代码
- 阅读关键文件
- 给出候选修改点
- 不执行写入

建议 allowed tools：

```python
("list_files", "read_file", "search")
```

输出约束：

```text
Findings:
- ...

Candidate files:
- ...

Suggested action:
- ...
```

### 3.3 Review Delegate

Review delegate 也是只读子 agent，但它不应该依赖 `history_text()` 猜上下文。

职责：

- 根据明确的验收标准检查修改
- 只审查 focus paths
- 发现遗漏、误改、未验证项
- 输出结构化结论

建议 allowed tools：

```python
("read_file", "search")
```

输出约束：

```text
status: pass | needs_fix
issues:
- ...
verify_targets:
- ...
```

首行缺失或不是严格的 `status: pass` / `status: needs_fix` 时，wrapper 不保留第三种状态：统一规范化为
`needs_fix`，并把 `malformed_review_status` 写入 issues。该函数属于 wrapper；native 不新增 review 路由。

---

## 4. Wrapper RoleDelegateSpec

native `delegate` 参数保持不变：

```json
{
  "task": "inspect README.md",
  "max_steps": 3
}
```

LangGraph wrapper 不把下面的对象作为模型可见 tool args，而是在 graph node 内部构造
`RoleDelegateSpec`：

```json
{
  "role": "research",
  "task": "Find the file that defines the CLI entry point.",
  "max_steps": 3
}
```

Review 模式使用显式 review packet：

```json
{
  "role": "review",
  "task": "Review the patch for the README update.",
  "max_steps": 3,
  "focus_paths": ["README.md"],
  "acceptance": "README opening sentence must say this fixture is a locked benchmark workspace.",
  "context_summary": "Coordinator executed patch_file on README.md; diff_summary=modified:README.md."
}
```

wrapper 内部字段：

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `role` | `str` | 无 | `"research"` 或 `"review"`；不提供 default role |
| `max_steps` | `int` | `3` | wrapper role child 的模型/工具轮数，限制在 `[1, 12]` |
| `focus_paths` | `list[str]` | `[]` | review 重点检查文件 |
| `acceptance` | `str` | `""` | review 验收标准 |
| `context_summary` | `str` | `""` | Coordinator 传给 reviewer 的结构化上下文 |

边界规则：

- native 旧格式不添加 `role/mode/focus_paths/acceptance/context_summary`，继续只校验 `task/max_steps`。
- wrapper `role="research"` 只允许 `list_files/read_file/search`，使用内存 session，并关闭 checkpoint/durable memory 写入。
- wrapper `role="review"` 只允许 `read_file/search`，必须提供非空 `focus_paths/acceptance/context_summary`，并使用同样的隔离开关。
- `max_steps` 默认 3；wrapper role child 限制在 `[1, 12]`，native 旧 delegate 的原有正整数范围不变。
- wrapper role child 的构造、模型调用和 child finalizer 必须放在 `try/except/finally` 内；失败时记录 `delegate_failed`，不把原始异常消息写入 artifact。

---

## 5. Wrapper role child 行为

LangGraph 节点调用内部 `create_role_delegate(RoleDelegateSpec)`。native `spawn_delegate()` 保留旧实现，
不按 `mode` 分叉，也不自动创建 Research/Review 角色。

### 5.1 Research role

构造只读子 agent：

```python
child = Pico(
    ...,
    session_store=InMemorySessionStore(),
    approval_policy="never",
    read_only=True,
    allowed_tools=("list_files", "read_file", "search"),
    allow_checkpoint=False,
    allow_durable_memory_write=False,
)
```

prompt 约束：

```text
You are a read-only research delegate.
Do not modify files.
Use only evidence from the workspace.
Return Findings, Candidate files, and Suggested action.
```

上下文：

- 可以继续给少量 `history_text()`
- 但应明确告诉子 agent 这是背景，不是审查标准

### 5.2 Review role

构造只读审查子 agent：

```python
child = Pico(
    ...,
    session_store=InMemorySessionStore(),
    approval_policy="never",
    read_only=True,
    allowed_tools=("read_file", "search"),
    allow_checkpoint=False,
    allow_durable_memory_write=False,
)
```

prompt 约束：

```text
You are a read-only review delegate.
Do not modify files.
Review only the focus paths unless evidence requires one extra read.
Judge the result against the acceptance criteria.
First line must be: status: pass or status: needs_fix.
```

上下文必须来自 review packet：

- `task`
- `focus_paths`
- `acceptance`
- `context_summary`

不要只依赖 `history_text()`。`history_text()` 可以作为补充背景，但不应该作为 reviewer 的主要输入。

review 输出由 wrapper 的 `_normalize_review_result(text)` 处理：严格首行分别映射为 `pass` / `needs_fix`；其他首行统一改写为 `status: needs_fix`，并在 issues 中加入 `malformed_review_status`。规范化函数必须幂等，LangGraph review 节点的路由使用规范化结果；malformed review 计入一次 `malformed_output_recovered`。

---

## 6. Review Packet 来源

Coordinator 不需要凭空编 review packet。现有工具层已经提供一部分结构化信息。

`ToolExecutionResult.metadata` 里已有：

- `tool_status`
- `tool_error_code`
- `security_event_type`
- `affected_paths`
- `workspace_changed`
- `diff_summary`
- `workspace_fingerprint`

Coordinator 可以在每次工具执行后收集这些 metadata，形成最近一次执行摘要：

```python
review_packet = {
    "role": "review",
    "task": "Review whether the requested change is complete.",
    "focus_paths": affected_paths,
    "acceptance": user_request,
    "context_summary": "; ".join(diff_summary),
    "max_steps": 3,
}
```

第一版不需要复杂规划，只要在 LangGraph execute 节点产生可审查路径后调用一次 review role child 即可。

---

## 7. Sandbox 处理方式

本轮不重建 sandbox 基础能力。

已有能力：

- path 约束：`Pico.path()`
- 工具 allowlist：`allowed_tools`
- 风险工具审批：`approval_policy`
- 只读 delegate：`read_only=True` + `approval_policy="never"`
- shell timeout：`run_shell.timeout`
- shell env allowlist：`shell_env()`
- trace/report 脱敏：`redact_artifact()`

本轮要补的是事件化和指标化：

- `tool_not_allowed` 计入 `sandbox_violations`
- `path_escape` 计入 `sandbox_violations`
- `read_only_block` 计入 `sandbox_violations`
- 这些事件写入 EventSink
- harness 汇总这些指标

建议事件：

```json
{
  "event": "sandbox_violation",
  "tool": "write_file",
  "tool_error_code": "approval_denied",
  "security_event_type": "read_only_block",
  "agent_role": "review"
}
```

---

## 8. Harness 设计

Harness 的目标不是证明模型能力，而是证明 runtime 合同稳定。

### 8.1 BackendRunner 协议

为了支持 native 和 langgraph，对 evaluator 增加 backend 适配层。

```python
from typing import Protocol

class BackendRunner(Protocol):
    def run_task(self, task: dict, workspace, session_store, run_store, fixture_copy_root, model_client=None):
        """跑一个 benchmark task，返回 BackendRunResult。"""
        ...
```

`BackendRunResult` 携带 `task_state / final_answer / agent / child_task_states / budget_task_states / initial_state / events`，由 evaluator 统一组装最终 row、执行 verifier、写 artifact；backend 不直接拼 JSON。runner 通过 `event_sink_factory(run_store)` 接受 Jsonl/Null 等旁路 sink，并始终与独立 EventCollector 组合。native 和 langgraph 都返回未合并的 child task_state；`budget_task_states` 用于预算二次校验；`events` 是本次 task 的只读内存事件快照。delegate 异常也必须返回该结构，不能因 child 抛错而丢失 child state 或事件。

第一版：

- `NativeBackendRunner`：从 `BenchmarkEvaluator.run_task()` 提取逻辑实现，不复制粘贴
- `model_client_factory` 通过 evaluator 层传入，不在 backend 内部 import `FakeModelClient`
- harness-local model adapter 只在 evaluator/backend 边界分类 `model_error`，不改变普通 native CLI
- runner 在已有 TaskState 后收口执行异常；evaluator 再收口 setup/构造/verifier 异常，保证单 task 失败不终止整批任务

第二版：

- `LangGraphBackendRunner`
- 只在选择 langgraph backend 时懒加载 example 暴露的 `langgraph_pico` 模块
- 返回与 native 一致的 `BackendRunResult`

### 8.2 CLI 入口

`pico eval` 需要先重构 CLI：

- 保留公开的 `build_arg_parser()` 直接解析现有 run 参数，避免破坏已有调用和测试
- 新增内部 `build_cli_parser()` 采用 subparser 结构；无子命令 CLI 输入在解析前规范化为 `run`
- `pico --help` 继续规范化为 `pico run --help`；`run/eval` 作为普通 prompt 首词时要求显式写 `pico run ...`
- 新增 `eval` 子命令

建议命令：

```bash
pico eval
pico eval --backend native
pico eval --backend langgraph --tasks benchmarks/delegate_tasks.json
pico eval --tasks benchmarks/coding_tasks.json
pico eval --tasks benchmarks/delegate_tasks.json
pico eval --out benchmarks/results/xxx.json
```

### 8.3 最小任务集

新增独立的 `benchmarks/delegate_tasks.json`，包含四类 harness 任务；不要追加到现有 `benchmarks/coding_tasks.json`。每个任务必须且只能提供 `verifier_argv` 或 legacy `verifier` 字符串之一；优先使用结构化 argv，legacy 字符串只作为受信 v1 输入兼容。任务可增加 `acceptance`、`requires_research`、`focus_paths`、`artifact_path`、`verifier_timeout_s` 和 `backends`。review 只使用显式 `focus_paths/artifact_path` 后备，不能调用 legacy fixture 映射；路径逃逸必须拒绝：

| 任务 | 目标 |
|---|---|
| `research_then_patch` | Coordinator 先调用 research delegate，再完成修改 |
| `review_catches_incomplete_fix` | Review delegate 发现修改不完整，Coordinator 继续修复 |
| `delegate_write_denied` | research delegate（有 allowlist）尝试写入，被 `tool_not_allowed` 拦截，记录 sandbox violation |
| `default_delegate_write_readonly_block` | default delegate（无 allowlist，`read_only=True`）尝试写入，被 `read_only_block` 拦截，记录 sandbox violation；仅适用于 native |

其中 `research_then_patch`、`review_catches_incomplete_fix` 和 `delegate_write_denied` 是 LangGraph
role task，应显式设置 `backends=["langgraph"]`；`default_delegate_write_readonly_block` 是 native-only
旧行为回归。这样 benchmark 不会把 native 没有的三角色流程误算成 native 失败。

### 8.4 输出指标

每个 task result 至少包含：

- `status`（`pass` / `fail` / `skipped`）
- `passed`
- `tool_steps`
- `attempts`
- `delegate_calls`
- `delegate_failures`
- `research_calls`
- `review_calls`
- `review_passed`
- `review_retries`
- `sandbox_violations`
- `malformed_output_recovered`
- `stop_reason`
- `failure_category`
- `duration_ms`

其中 `tool_steps/attempts/sandbox_violations/malformed_output_recovered` 统一按 graph-level task state 加全部 child task state 求和；`duration_ms` 在 evaluator 调 runner 的外层计量。事件公式固定为：`delegate_calls=delegate_started+review_requested`，`delegate_failures=delegate_failed`，`research_calls=agent_role 为 research 的 delegate_started`，`review_calls=review_requested`，`review_retries=review_retry_started`；事件类指标不得读取 `trace.jsonl`。native baseline 只统计实际发生的旧 delegate 调用，Research/Review 指标为 `0` 或 `null`；LangGraph backend 才产生三角色节点指标。`delegate_failed` 必须由 native 和 LangGraph 共同产生，且只记录脱敏异常类型，不把异常消息作为指标输入。

`malformed_output_recovered` 同时统计 AgentLoop parser retry 和 malformed review status 规范化，同一次异常输出只计一次。benchmark 输入继续使用 `schema_version=1`；result artifact 使用 `schema_version=2`，但必须保留 v1 的 captured_at/runtime/reproducibility/failure_category_counts 和既有 row 字段。writer 写 v2，reader 支持 v1/v2。历史任务缺省 `backends=["native"]`；LangGraph role task 显式声明 `["langgraph"]`，需要对比时显式声明 `["native", "langgraph"]`。不适用的 backend 输出 `status="skipped"`、`failure_category="backend_not_applicable"`，不计入 pass/fail、预算率或 verifier 率分母；summary 同时输出 `total_tasks/eligible_tasks/skipped_tasks`。setup、runner 构造或模型创建在执行前失败时，输出统一 failed row：`execution_started=false`、`failure_category="harness_error"`、路径字段为空字符串、`task_state/report` 为空对象、`events` 为空列表。持久化写入失败使用 `failure_category="persistence_error"` 和 `STOP_REASON_PERSISTENCE_ERROR`，并继续 best-effort 写入剩余生命周期 artifact。若 child 在模型异常处提前退出，必须补齐 child 的 failed state、`run_finished` 和 report，不能只保留父级 `delegate_failed` 事件。

`within_budget` 使用单独的 Coordinator 预算状态集合做二次校验：native 只计主 `task_state`；LangGraph 计 graph-level 调度和每次 executor tool steps，不计 research/review 子 Agent 内部步骤。LangGraph graph state 同时维护 `step_budget/coordinator_steps_used` 做运行时硬限制，并为 review 预留一步。

FakeModel 脚本按 `(backend, task_id)` 分开。native 使用旧 delegate 工具合同，LangGraph 直接启动 role child，两者调用顺序不同，禁止共用响应队列。FakeModel 响应队列耗尽时抛出 `RuntimeError`，runner 将其在 harness 边界分类为 `model_error`；对外 artifact 只保留异常类型和脱敏诊断，不写入原始 provider 异常消息；普通 native CLI 不改变原异常合同。verifier 优先使用结构化 argv，兼容旧字符串时用 `sys.executable + shell=False`，并设置 1-60 秒 timeout；异常或超时只失败当前 task。

---

## 9. EventSink 与可观测性

先实现轻量 EventSink，不直接接 OpenTelemetry。除 `JsonlSink/NullSink` 外增加内存 `EventCollector` 和扇出用 `CompositeSink`；BackendRunner 通过 `event_sink_factory(run_store)` 获得 configured sink，harness 每个 task 都使用 `CompositeSink(EventCollector, configured_sink)`，不能硬编码 JsonlSink。

EventSink 只负责旁路观测，不能成为控制流的数据源。工具执行产生的 `affected_paths` 累计写入 `TaskState`；切换 `NullSink`、KafkaSink 或 OpenTelemetry Sink 不得改变 review 路由。evaluator 消费 `EventCollector.snapshot()`，所以关闭 JSONL 也不得改变 delegate/review/retry 指标。

CompositeSink 先写 collector，再 best-effort 写 configured sink。旁路 sink 异常只产生脱敏的 `event_sink_failed` 内存事件，不得中断 Agent；collector 自身异常才按 harness runtime error 处理。

建议事件：

| 事件 | 说明 |
|---|---|
| `delegate_started` | 子 agent 开始 |
| `delegate_finished` | 子 agent 完成 |
| `delegate_failed` | 子 agent 异常退出 |
| `review_requested` | Coordinator 发起 review |
| `review_passed` | review 通过 |
| `review_failed` | review 发现问题 |
| `review_retry_started` | review 失败后实际重新进入修改执行 |
| `sandbox_violation` | sandbox 拦截 |
| `event_sink_failed` | configured sink 写入失败；仅存在于内存 collector |

事件 payload 应包含：

- `agent_role`
- `task`
- `max_steps`
- `allowed_tools`
- `focus_paths`
- `status`
- `duration_ms`

`review_*` 和 `review_retry_started` 只由 LangGraph wrapper 发出；native baseline 只保留旧 delegate
事件和实际 sandbox 事件。所有业务指标来自内存 `EventCollector`，不能依赖 JSONL 是否存在。

---

## 10. LangGraph 的位置

LangGraph 不是第一阶段主框架，而是可选 wrapper。

它负责把多 agent 流程显式化为可视图，并为后续接入持久 checkpointer/replay 提供清楚的节点边界：

```text
START -> plan
plan -- requires_research=true --> research_delegate
plan -- requires_research=false -----------------------> execute
research_delegate -- budget ok --> execute
research_delegate -- budget exhausted --> finalize
execute --> route_after_execute
route_after_execute -- no review path --> finalize
route_after_execute -- budget exhausted --> finalize
route_after_execute -- review path --> review_delegate
review_delegate --> route_finish_or_fix
route_finish_or_fix -- budget exhausted --> finalize
route_finish_or_fix -- pass --> finalize
route_finish_or_fix -- needs_fix and retries remain --> execute
route_finish_or_fix -- retry limit --> finalize
finalize --> END
```

research_delegate 和 review_delegate 是独立的 LangGraph 节点，各自构造 `RoleDelegateSpec` 并调用内部
`create_role_delegate(...)`。如果把它们内嵌在 coordinator 节点里，LangGraph 图就只是套壳，无法体现多 agent 编排价值。

注意：

- LangGraph state 只放纯数据（task/research_result/execution_result/review_status 等）
- 不把 `Pico` 实例放进 state
- `Pico` 通过 `config["configurable"]["agent"]` 注入，各节点从 config 取，不从 state 取
- LangGraph 首版只运行 `benchmarks/delegate_tasks.json`；其中每个任务的 `allowed_tools` 显式包含 `delegate`，默认 12 题保持不变
- plan 只读取已校验的 `requires_research`，缺省为 `true`；不得调用模型决定路由
- research/review 节点调用 role child factory 前仍检查父任务是否允许 `delegate`，不能把图节点当成绕过 allowlist 的隐藏能力
- review path 按 `affected_paths → task.focus_paths → task.artifact_path` 回退；不调用 legacy `_artifact_path_for_task()`，仍为空时以 `no_changes_to_review` 收口
- executor 使用独立 TaskState 和父 session 深拷贝；executor/research/review 均使用内存 session store，并关闭 checkpoint/durable memory 写入
- 两个写入开关在普通 Pico/default delegate 中默认开启，保持 native 旧行为
- 模型异常由 harness-local adapter 对两个 backend 统一标记；不修改主包 provider clients
- graph state 运行时硬限制 Coordinator 预算；不足时不启动节点并设置 `budget_exhausted`
- finalize 映射 pass、`review_retry_limit_reached`、`no_changes_to_review`、`budget_exhausted` 和 `delegate_failed`；其中 `delegate_failed` 在 graph-level TaskState 上使用稳定常量 `STOP_REASON_DELEGATE_FAILED`。native 与 LangGraph 共享事件和指标口径，但 native delegate failure 可以由 Coordinator 继续恢复，LangGraph review failure 按图合同终止。
- graph invoke 必须用 `try/except/finally` 完成失败状态、`run_finished`、耗时和 report，异常后不能留下永久 `running`
- LangGraph backend 必须返回和 native backend 一致的 harness result
- 第一版明确 `builder.add_node/add_edge/add_conditional_edges/compile()`，但不启用 checkpointer；configurable 中的运行时对象不能被描述为已支持跨进程 durable replay

这样简历叙事更自然：

> native loop 保持原有受限 delegate 合同；我在 LangGraph wrapper 中用 Coordinator、Research role 和 Review role
> 编排 research → execute → review 流程，并把每个多 agent 步骤显式化为图节点，方便后续接入 checkpoint、replay 和审计。

---

## 11. 分阶段实施

### 阶段 1：wrapper role child 基础

修改：

- `pico/runtime.py`
- `pico/session_store.py`
- `examples/langgraph-pico/`

内容：

- 保持 `DELEGATE_TOOL_SPEC`、native `validate_tool()` 和 `spawn_delegate()` 的旧合同
- 新增 wrapper 内部 `RoleDelegateSpec` 和 `create_role_delegate()`
- 新增核心 `InMemorySessionStore`；wrapper research/review 关闭 checkpoint/durable memory 持久写
- child 构造、模型调用和失败 finalizer 使用统一 `try/except/finally`

验收：

- 旧格式 delegate 仍然可用
- LangGraph `research` role 只能读
- LangGraph `review` role 只能读
- native 旧 delegate 的行为和 schema 不变
- role child 写入请求被拒绝，并记录 `sandbox_violation`

### 阶段 2：wrapper review packet

修改：

- `examples/langgraph-pico/`
- `pico/runtime.py`（仅抽取可复用的、向后兼容的生命周期 helper）

内容：

- wrapper role factory 强制 review packet 三个字段非空
- wrapper review 节点只用显式 packet 构造 reviewer prompt，`history_text()` 仅作补充背景
- 新增 wrapper 内幂等 `_normalize_review_result(text)`，规范化 status/issues，malformed review 只增加一次恢复计数
- Coordinator 通过 graph state 用 acceptance、affected paths 和执行摘要填充 packet；native `AgentLoop` 不自动 review

验收：

- reviewer 能拿到 `focus_paths`
- reviewer 不依赖纯 `history_text()`

### 阶段 3：EventSink + metrics

修改：

- `pico/event_sink.py`
- `pico/runtime.py`
- `pico/task_state.py`
- `pico/tool_executor.py`
- `pico/agent_loop.py`

内容：

- 新增 EventSink / JsonlSink / NullSink / EventCollector / CompositeSink
- 记录 delegate/review/review retry/sandbox 事件
- TaskState 增加计数器和累计 `affected_paths`
- harness 始终返回内存事件快照，指标不依赖 JSONL
- BackendRunner 使用 event_sink_factory 注入 Jsonl/Null sink

验收：

- trace 格式向后兼容
- NullSink 不写 trace
- sandbox 拦截计数进入 report
- NullSink 与 JsonlSink 的 delegate/review/retry 指标一致

### 阶段 4：BackendRunner + pico eval

修改：

- `pico/cli.py`
- `pico/evaluation/evaluator.py`
- `pico/evaluation/metrics.py`（审计并兼容读取 v1/v2 result artifact）
- 新增 `pico/evaluation/backends.py`
- 新增 `pico/evaluation/verifier.py`
- `tests/test_evaluator.py`

内容：

- CLI 改 subparser
- 新增 `pico eval`
- 抽 `NativeBackendRunner`
- 预留 `LangGraphBackendRunner`
- result writer 写 v2，reader 兼容 v1/v2
- v2 保留原有 provenance；逐 task 异常收口，失败不终止整批任务
- FakeModel 脚本按 backend/task 分开
- verifier 使用结构化 argv、sys.executable、shell=False 和 timeout

验收：

- `pico eval --backend native` 可跑通
- 输出 artifact 包含 delegate/review/sandbox 指标

### 阶段 5：LangGraph wrapper

修改：

- `examples/langgraph-pico/`
- `pico/evaluation/backends.py`（只增加选择 langgraph 时执行的懒加载分支）

内容：

- 纯数据 state
- example 安装后暴露 `langgraph_pico` Python 模块
- 通过 wrapper 调用 native backend 能力
- plan 按 `requires_research` 确定性路由；review path 使用受控回退并定义无路径终态
- executor/research/review 禁止 checkpoint 和 durable memory 写入；model error 在 harness-local adapter 边界对两种 backend 统一包装
- graph state 硬限制全局 Coordinator 预算并预留 review 步骤
- 显式实现 add_node/add_edge/add_conditional_edges/compile
- finalize 明确映射 pass、retry limit、no changes、budget exhausted 和 delegate failed
- smoke test 锁定实际可用版本

验收：

- `pico eval --backend langgraph --tasks benchmarks/delegate_tasks.json` 至少跑通一个任务
- 结果结构与 native 一致
- 未安装 example/langgraph 时，`import pico` 和 native backend 仍正常
- executor/research/review 不修改持久 session/checkpoint/durable memory；NullSink 不影响路由和指标

---

## 12. 面试表述

可以这样讲：

> pico 先保留原生 AgentLoop 和受限 delegate 合同，保证默认 native 使用不变。新增能力放在可选 LangGraph wrapper：Coordinator 负责 plan/execute/finalize，Research role 只读调查，Review role 根据结构化 review packet 验收。所有 wrapper child 都受 allowed tools、read_only、审批策略和路径沙盒约束。最后用 harness 固定任务集，分别对 native baseline 和 LangGraph backend 评估 delegate 调用、review 结果、sandbox violation、恢复次数和 stop reason。

---

## 13. 不做的事

本阶段不做：

- 不做复杂 4 agent 群聊
- 不做数据库记忆
- 不做 Kafka sink
- 不做 OpenTelemetry sink
- 不做 Docker sandbox
- 不把 LangGraph 变成主 runtime
- 不把 `Pico` 实例放进 LangGraph state
