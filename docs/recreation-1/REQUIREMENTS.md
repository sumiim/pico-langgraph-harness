# pico 重构需求文档 v2

> 定位：Coding Agent Runtime。不做大系统重写，在现有基础上增量叠加。
> 本文档与 [THREE_ROLE_DELEGATE_HARNESS.md](THREE_ROLE_DELEGATE_HARNESS.md) 对齐。

---

## 背景与目标

pico 现有一个完整的原生 agent 控制流（`AgentLoop`），具备 checkpoint/resume、分层记忆、trace 输出、eval 基础设施，以及一个受限 `delegate` 子 agent 机制。

本次重构目标是在不破坏主包零依赖定位的前提下，补齐五块：

1. **EventSink 可观测性抽象**：让 trace 输出可插拔，默认 JSONL，后续可接 OpenTelemetry / Kafka。
2. **LangGraph 三角色协作**：在可选 wrapper 中形成 Coordinator + Research delegate + Review delegate 的显式闭环；原生 Pico 不自动启用这套编排。
3. **sandbox 指标化**：不重建 sandbox 基础能力，只把现有拦截事件接入 trace、TaskState 和 harness 指标。
4. **`pico eval` harness CLI**：用固定任务集评估 agent 稳定性，并通过 BackendRunner 支持 native / langgraph 后端。
5. **LangGraph 可选 wrapper**：不替代主 runtime，只在 wrapper 中复用已有 child-agent 能力并显式编排为可审计、便于后续接入 replay 的图。

---

## 功能需求

### F1：EventSink 抽象

**现状**：`emit_trace` 直接写 JSONL，trace 输出与 `RunStore` 强绑定。

**需求**：

- 新增 `EventSink` 抽象，定义 `emit(task_state, event_type, payload)` 接口。
- 默认实现 `JsonlSink`，行为与现有 `emit_trace` 一致。
- 新增 `NullSink` 用于测试。
- 新增内存 `EventCollector` 和组合 `CompositeSink`；harness 必须始终挂载 collector，并可同时旁路到 `JsonlSink`、KafkaSink 或 OpenTelemetry Sink。
- `EventSink` 后续可扩展为 OpenTelemetry / Kafka sink，但本期不实现。
- Agent 通过构造函数注入 sink，不改变外部调用方式。
- BackendRunner 通过 `event_sink_factory(run_store)` 注入本次 task 的旁路 sink；默认返回 `JsonlSink`，测试可返回 `NullSink`，不能在 runner 内硬编码 JSONL。
- EventSink 只负责旁路观测，控制流不得读取 sink 的落盘结果；`affected_paths` 等执行事实必须记录在 `TaskState` 中。
- evaluator 的事件类指标必须读取 runner 返回的内存事件快照，不得反向解析 `trace.jsonl`；JSONL 是否启用只影响审计产物，不影响指标和控制流。
- configured sink 失败时只能向内存 collector 追加脱敏的 `event_sink_failed`，不得抛回 Agent 改变控制流；collector 自身失败属于 harness runtime error。

**验收**：

- 现有所有 trace 事件（`run_started` / `prompt_built` / `tool_executed` / `run_finished` 等）正常写入 JSONL。
- 替换为 `NullSink` 后不报错，且不生成 trace 文件。
- 使用 `NullSink` 时，delegate/review/retry 指标与默认 `JsonlSink` 一致。
- trace JSONL 格式向后兼容。
- 现有测试不 break。

---

### F2：LangGraph 三角色协作

**现状**：原生 `delegate` 只有 `task` 和 `max_steps` 参数，子 agent 默认只读、限步；原生 Pico 不负责 Research → Execute → Review 的固定编排。

**需求**：

- 原生 `DELEGATE_TOOL_SPEC`、旧 delegate 参数和 `spawn_delegate(task, max_steps)` 行为保持不变；不得为了 LangGraph 角色需求修改 native 初始 prompt 或自动路由。
- 在主包提供不暴露给模型的通用 child-agent 构造/收口能力，供 LangGraph wrapper 复用 workspace、model client、sandbox、session store、run store 和 EventSink；该能力不能改变旧 `spawn_delegate()` 的默认行为。
- LangGraph wrapper 内部定义 `RoleDelegateSpec`，包含 `role`、`task`、`allowed_tools`、`focus_paths`、`acceptance`、`context_summary` 和 `max_steps`；这些字段不是 native delegate 的公开工具 schema。
- `role="research"` 只能使用 `list_files/read_file/search`，并使用内存 session、`allow_checkpoint=False`、`allow_durable_memory_write=False`。
- `role="review"` 只能使用 `read_file/search`，必须收到非空的 `focus_paths`、`acceptance` 和 `context_summary`，并使用同样的内存隔离策略。
- wrapper 的 role child factory 必须从 child 构造开始用 `try/except/finally` 收口；异常记录 `delegate_failed`，不得丢失 child state、耗时和 child run 关联。若 native 增加旁路生命周期采集，只能通过不改变旧控制流的兼容 helper 接入；LangGraph wrapper 负责将 role child 结果转换为图状态。
- Review 输出首行不是严格的 `status: pass` 或 `status: needs_fix` 时，LangGraph wrapper 统一规范化为 `needs_fix`，记录 `malformed_review_status`，并只增加一次 `malformed_output_recovered`。

**三角色定义**：

| 角色 | 实现位置 | 职责 |
|---|---|---|
| Coordinator | LangGraph `plan/execute/finalize` 节点 + Pico runtime | 决定图路由、执行修改、汇总结果 |
| Research delegate | LangGraph `research_delegate` 节点 + 受限 Pico child | 只读调查、定位文件、给出建议 |
| Review delegate | LangGraph `review_delegate` 节点 + 受限 Pico child | 只读审查、按验收标准挑问题 |

**验收**：

- 原生 `<tool>{"name":"delegate","args":{"task":"..."}}</tool>` 仍然可用，且 native 不自动进入三角色流程。
- LangGraph 图中 Research 和 Review 是独立节点，不是把完整 `coordinator.ask()` 套在图外面。
- Research/Review child 无法写文件，且不创建持久子 session、checkpoint 或 durable memory。
- Review child 能收到 `focus_paths/acceptance/context_summary`。
- native 与 LangGraph 都能通过 EventSink/TaskState 保留 delegate 事件和 child state。

---

### F3：sandbox 指标化

**现状**：基础约束已经存在：

- path 约束：`Pico.path()`
- 工具 allowlist：`allowed_tools`
- 风险工具审批：`approval_policy`
- 只读 delegate：`read_only=True` + `approval_policy="never"`
- shell timeout：`run_shell.timeout`
- shell 环境变量 allowlist：`shell_env()`
- trace/report 脱敏：`redact_artifact()`

**需求**：

- 不重建上述基础能力。
- 将以下拦截计入 `TaskState.sandbox_violations`：
  - `tool_not_allowed`
  - `path_escape`
  - `read_only_block`
- 将 sandbox 拦截写入 EventSink，事件名为 `sandbox_violation`。
- delegate 子任务里的 sandbox violation 需要能汇总到父任务，或在 harness 中可被统一统计。

**验收**：

- 白名单外 tool 返回拒绝，并计入 sandbox violation。
- path escape 返回拒绝，并计入 sandbox violation。
- 只读 delegate 尝试写入时被拒绝，并可在父任务或 harness 指标中看到。
- 脱敏后 trace / report 里不出现明文 key。

---

### F4：`pico eval` harness

**现状**：`pico/evaluation/` 有 evaluator + metrics，`benchmarks/coding_tasks.json` 有任务集，但没有 CLI 入口；`BenchmarkEvaluator.run_task()` 当前直接绑定 `Pico`。

**需求**：

- 新增 `pico eval` 子命令。
- CLI 先重构为 subparser 结构，同时保持裸 `pico`、`pico <prompt>`、原有 flags 和 `pico --help` 行为兼容。
- `run` / `eval` 成为首词保留字；需要把它们作为普通 prompt 时显式使用 `pico run <prompt>`。
- 抽象 `BackendRunner`，由 native / langgraph 后端各自实现 task 执行；native 是原生行为 baseline，LangGraph backend 才负责三角色编排。
- 默认 backend 为 `native`。
- 支持指定任务文件（默认 `benchmarks/coding_tasks.json`）。
- 现有 12 题默认 benchmark 不改；新增四个 delegate/review/sandbox 任务放在独立的 `benchmarks/delegate_tasks.json`，通过 `--tasks` 显式运行。
- 结果写入 `benchmarks/results/<timestamp>-eval.json`。
- benchmark 任务定义继续使用 `schema_version=1`；新增指标后的 evaluation result artifact 使用独立的 `schema_version=2`，并在 `benchmark.schema_version` 中保留输入版本。
- result reader 必须同时接受历史 v1 和新 v2 artifact；writer 默认只写 v2。升级时必须审计 `pico/evaluation/metrics.py`、现有结果读取脚本和 schema 断言测试，不能把它们列为“无需改动”。

每个任务输出指标至少包含：

| 指标 | 说明 |
|---|---|
| `status` | `pass`、`fail` 或 `skipped`；skipped 不进入评测分母 |
| `passed` | 任务是否通过；由 verifier、artifact、预算和停机原因统一判定 |
| `tool_steps` | coordinator 与全部 child run 的工具调用总数 |
| `attempts` | coordinator 与全部 child run 的模型调用尝试总数 |
| `duration_ms` | 耗时毫秒 |
| `stop_reason` | 停机原因 |
| `failure_category` | 失败分类；包含 model/runtime/harness/verifier/verifier_timeout/budget/persistence 等稳定枚举 |
| `delegate_calls` | delegate 调用次数 |
| `delegate_failures` | delegate 失败次数 |
| `research_calls` | research delegate 调用次数 |
| `review_calls` | review delegate 调用次数 |
| `review_passed` | review 是否通过 |
| `review_retries` | review 失败后 coordinator 重新执行修改的次数 |
| `sandbox_violations` | sandbox 拦截次数 |
| `malformed_output_recovered` | 解析失败自动恢复次数 |

指标口径：

- runner 返回 graph-level `task_state`、未合并的 `child_task_states`、用于预算判定的 `budget_task_states` 和本次运行的只读 `events` 快照；evaluator 只在一个地方聚合计数，避免漏算或重复计数。
- `duration_ms` 由 evaluator 在 runner 外层计量整个任务的墙钟时间。
- `delegate_calls = count(delegate_started) + count(review_requested)`；`delegate_failures = count(delegate_failed)`；`research_calls = count(delegate_started where agent_role="research")`；`review_calls = count(review_requested)`。native baseline 只统计它实际发生的旧 delegate 调用，Research/Review 指标通常为 `0` 或 `null`；LangGraph backend 统计图中的 Research/Review 节点。事件失败口径统一，但 native 允许旧 Coordinator 从 tool failure 继续恢复，LangGraph review failure 按图合同进入 `delegate_failed` 终态。
- `review_passed` 取最后一次已完成 review 的结果；任务未发生 review 时为 `null`。
- `review_retries` 只统计 `review_failed` 之后实际重新进入 execute 时发出的 `review_retry_started` 事件，不等同于 `review_failed` 事件数。
- `within_budget` 只按 `budget_task_states` 中的 Coordinator 步数判定，不使用包含 research/review 子 agent 的聚合 `tool_steps`。native 的预算状态只有主 `task_state`；langgraph 的预算状态为 graph-level 调度 `task_state` 加每次 execute 节点的 executor child state，不包含 research/review child state。

**验收**：

- `pico eval --backend native` 可跑通默认任务集。
- 输出 JSON 包含上述指标。
- `BenchmarkEvaluator` 不再硬绑定唯一 runtime 实现。
- 后续接入 `--backend langgraph` 时不破坏 native 行为。
- deterministic FakeModel 输出必须按 `(backend, task_id)` 分开；native 与 LangGraph 不得共用一条假定调用顺序相同的响应序列。
- 每个 task 是独立故障域：backend、模型、setup 或 verifier 异常只能产生当前 task 的 failed row，不能中止后续任务或阻止最终 artifact 写出。FakeModel 响应耗尽统一抛出 `RuntimeError`，harness 在 backend 边界将其分类为 `model_error`；harness 内部可包装 model client 进行错误分类，但普通 native CLI 的异常合同不变。
- verifier 必须且只能提供 `verifier_argv` 或 legacy `verifier` 字符串之一；历史 v1 字符串仅作为受信 benchmark 的兼容输入，执行时必须 `shell=False`、把 `python3` 规范化为 `sys.executable`，并应用有上限的 `verifier_timeout_s`。超时记录为 `verifier_timeout`，stdout/stderr 必须裁剪和脱敏。
- 任务可选 `backends: list[str]`；为保持旧 benchmark 语义，历史任务缺省为 `["native"]`，需要 LangGraph 的任务必须显式声明 `backends=["langgraph"]`，需要双后端对比时显式声明 `["native", "langgraph"]`。backend 不适用的 task 输出 `status="skipped"`、`failure_category="backend_not_applicable"`，不计入 pass/fail、预算率或 verifier 率分母。summary 必须同时保留 `total_tasks`（包含 skipped）、`eligible_tasks`、`skipped_tasks`；`passed/failed/pass_rate` 只基于 `status != "skipped"` 的 row。`default_delegate_write_readonly_block` 是 native-only 的旧行为回归任务，不代表 native 需要实现三角色编排。
- pre-run setup/构造失败的 row 必须保留统一字段：路径类字段使用空字符串、`task_state/report` 使用空对象、`events` 使用空列表，并设置 `execution_started=false`、`failure_category="harness_error"`；reader 不得假设这些字段一定指向文件。若 LangGraph role child 在模型异常处退出且自身尚未完成 AgentLoop finalizer，child-agent factory 必须把 child state 标为 failed，并补写 child 的 `run_finished`、耗时和 report，不能只在父任务上记录 `delegate_failed`。
- evaluation artifact v2 必须保留 v1 的 `captured_at`、`runtime`、`reproducibility`、`failure_category_counts`、`rows` 和 `summary`，只做增量扩展。

---

### F5：LangGraph 可选 wrapper

**现状**：无 LangGraph 模块。

**需求**：

- 位置：`examples/langgraph-pico/`，有独立 `pyproject.toml`。
- 依赖：`langgraph`，只在 example 模块声明，不进入主包依赖。
- LangGraph 只作为 wrapper，不替代主 runtime；本期图结构为后续接入持久 checkpointer/replay 提供边界，不宣称默认已经启用持久化 replay。
- State 只放纯数据，不放 `Pico` 实例。
- example 的可导入模块名固定为 `langgraph_pico`；`Pico` / model client / runner 通过节点闭包或 `config["configurable"]` 注入。
- 主包只在用户选择 `--backend langgraph` 时懒加载 `langgraph_pico`，native 路径不得在 import 阶段触碰 LangGraph 依赖。
- LangGraph 首版只运行独立的 `benchmarks/delegate_tasks.json`；任务的 `allowed_tools` 必须显式包含 `delegate`。默认 12 题不增加该权限，也不用于 LangGraph 三角色图。
- `delegate_tasks.json` 可选字段 `requires_research` 决定 plan 路由，缺省为 `true`；该值必须在建图前规范化为布尔值，plan 节点不得调用模型临时决定是否 research。
- `delegate_tasks.json` 可选字段 `focus_paths` 和 `artifact_path` 作为 review 路径后备，只接受工作区内的非空相对路径，绝对路径和 `..` 越界必须拒绝。review 路径按“本轮 `TaskState.affected_paths` → 任务显式 `focus_paths` → 任务显式 `artifact_path`”取首个非空集合；不得调用总能返回路径或抛错的 legacy `_artifact_path_for_task()` 推导 review 路径。仍为空时不得调用 reviewer，图以 `no_changes_to_review` 失败终态收口。
- research/review 图节点调用 role child factory 前必须检查任务的 `allowed_tools` 是否允许 delegate 能力，不能把图节点当成绕过权限的隐藏能力。native 与 LangGraph 统一事件名和 failure category，但不强行统一恢复策略：native 保持旧 delegate/tool failure 行为；LangGraph research failure 可继续 execute，review failure 必须以 `STOP_REASON_DELEGATE_FAILED` 收口。
- execute 节点使用独立 TaskState 计量，并从父 Coordinator 的 session 深拷贝运行快照，以读取 task setup、history、memory 和 checkpoint 上下文；父 Coordinator 是 LangGraph 流程中持久 session/checkpoint/durable memory 的唯一所有者。executor 以及 research/review delegate 都必须使用内存 session store，并设置 `allow_checkpoint=False`、`allow_durable_memory_write=False`。
- `allow_checkpoint` 和 `allow_durable_memory_write` 在普通 `Pico` 和 default delegate 中默认均为 `true`；research/review delegate 与 LangGraph executor 显式关闭，保证旧 native 调用行为不变。
- provider/model 异常只在 harness-local model client adapter 边界包装为 `model_error`，native/LangGraph 评测共用；不得修改主包 provider client 或普通 native CLI 的异常语义。节点和路由异常归为 `runtime_error`，持久化写入失败归为 `persistence_error`，并映射到稳定停机常量 `STOP_REASON_PERSISTENCE_ERROR`。
- graph state 必须保存 `step_budget/coordinator_steps_used` 并在节点执行前硬校验：delegate 调度计 1 步，executor 按实际 tool steps 计数，并为后续 review 预留 1 步；预算不足时不得再启动节点，直接以 `budget_exhausted` 收口。`within_budget` 仍作为 evaluator 的二次校验，而不是唯一预算防线。
- `finalize` 必须产生明确终态：review pass 为成功；修复次数耗尽为 `review_retry_limit_reached`；无可审查路径为 `no_changes_to_review`；运行时预算不足为 `budget_exhausted`；review delegate 异常为 `delegate_failed`，并映射到稳定停机常量 `STOP_REASON_DELEGATE_FAILED`。`BackendRunResult.task_state` 始终使用 graph-level TaskState，节点状态仅进入 `child_task_states`。
- graph-level TaskState 的生命周期必须由 `try/except/finally` 收口；节点、模型或持久化异常后不得保持 `running`，必须按边界记录 `model_error`、`runtime_error` 或 `persistence_error`，并补齐 `run_finished`、`run_duration_ms` 和 report。

建议图结构：

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

注意：research_delegate / review_delegate 是显式的 LangGraph 节点，各自构造 `RoleDelegateSpec` 并调用内部 role child factory；把 research/review 内嵌在单一 coordinator 节点里只是套壳，无法体现 LangGraph 的多 agent 编排价值。

**验收**：

- `examples/langgraph-pico/` 有独立包元数据，并可按文档用仓库根 + example 的双 editable install 安装；不要求 example 单独解析未发布的本地 `pico` 依赖。
- smoke test 验证实际 LangGraph 版本可用后，再锁定版本范围。
- graph state 不包含不可序列化对象。
- 缺少 `delegate` 权限的任务在进入图执行前被明确拒绝，不能由图节点绕过 allowlist。
- executor 能从隔离的 session 快照看到 `_apply_task_setup()` 写入的 history、memory 和 checkpoint 状态，且不会覆盖父 session。
- executor 运行前后父 session、checkpoint 与 workspace durable memory 均保持不变。
- research/review delegate 运行后不新增持久 session/checkpoint，workspace durable memory 保持不变；default delegate 回归行为不变。
- `requires_research=false` 时不产生 research delegate 调用；缺省或为 `true` 时只产生一次 research 节点调用。
- 无 `affected_paths/focus_paths/artifact_path` 时不调用 reviewer，并以 `no_changes_to_review` 结束。
- 全局 Coordinator 预算不足时不再启动 delegate/executor/reviewer，并以 `budget_exhausted` 结束。
- 图执行异常后 graph-level TaskState 不处于 `running`，且生命周期 artifact 完整。
- 至少一个 `benchmarks/delegate_tasks.json` 中的任务可通过 langgraph backend 跑通。

---

## 非功能需求

- 主包 `pico/` 运行时依赖保持为零。
- LangGraph 依赖只在 `examples/langgraph-pico/pyproject.toml`。
- 现有测试全部通过，新增功能有对应测试。
- trace 格式向后兼容。
- 旧 `delegate` 用法向后兼容。
- native backend 是默认路径，不能被 LangGraph wrapper 阻塞。
- 未安装 `langgraph` 和 example 包时，`import pico`、`pico`、`pico <prompt>` 与 native 测试必须正常。
- native delegate schema 不因 LangGraph wrapper 改变；LangGraph role spec 属于 wrapper 内部数据。旧 checkpoint 不应仅因新增 LangGraph 功能而触发 `tool_signature` mismatch，且不得丢失 session/history。

---

## 不在本期范围内

- Milvus / 向量数据库。
- Docker sandbox。
- OpenTelemetry sink 实现。
- Kafka sink。
- 复杂 4 agent 群聊。
- 把 LangGraph 改成主 runtime。
