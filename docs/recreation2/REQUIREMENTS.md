# Pico LangGraph 意图路由需求文档 v1

> 定位：在 `docs/recreation-1` 已完成的可选 LangGraph 三角色 wrapper 上，补齐通用运行入口的任务意图识别与按需路由。
> 本期不修改 native AgentLoop，不改变既有 benchmark 的严格代码修改语义。

**文档优先级：** `docs/recreation-1` 仍是三角色、sandbox、EventSink、harness 和 benchmark
的基础合同；本文只覆盖公共 LangGraph `run` 入口的 plan/route/终态扩展。发生表述冲突时，
公共 LangGraph 入口按本文执行，benchmark 与 native 按 recreation-1 执行，除非本文明确写出兼容调整。

---

## 背景与问题

当前 LangGraph 图按固定的 Coding Agent 流程运行：

```text
plan -> research_delegate? -> execute -> review_delegate -> finalize
```

`plan` 只读取 `requires_research`，不识别任务类型。`execute` 后如果没有
`affected_paths/focus_paths/artifact_path`，图统一以 `no_changes_to_review` 失败。
这个合同适用于“必须修改文件”的 benchmark，但不适用于公共运行入口：普通问候、知识问答和
只读项目分析本来就不应产生文件变化。

本期目标是把 benchmark 合同与通用运行合同分开：

1. 公共 LangGraph 入口支持 conversation、read-only 和 code-change 三类任务。
2. `auto` 模式使用受约束的意图识别模型，并通过 LangGraph 条件边选择节点。
3. 显式模式优先于模型判断，调用方可以获得确定性路由。
4. 工具权限由代码按意图强制，不能由 Router 输出自行授权。
5. benchmark 固定使用 `code_change`，保留原有 review、失败终态和指标语义。
6. Coordinator、Research delegate、Review delegate 的三角色定义不变；Intent Router 是
   Coordinator 内部的决策节点，不作为第四个 Agent。

---

## 术语与意图

内部规范值固定为：

| 意图 | 适用任务 | 执行权限 | Patch Review |
|---|---|---|---|
| `conversation` | 问候、概念解释、无需工作区证据的问答 | 纯模型调用，不创建工具执行器 | 不调用 |
| `read_only` | 项目分析、搜索、定位、解释现有代码 | `list_files/read_file/search`、只读 | 不调用 |
| `code_change` | 新增、修改、删除工作区内容，或要求运行修改型工作流 | Coordinator 原有工具减去 `delegate` | 必须调用 |

对同时包含分析和修改的混合任务，最终意图必须为 `code_change`，并可设置
`requires_research=true` 先调查再修改。

`task_mode` 是调用方请求的模式，允许：

```text
auto | conversation | read_only | code_change
```

`resolved_intent` 是图最终采用的规范意图，只允许后三个值。

---

## 功能需求

### F1：公开 API 与 CLI

- `langgraph_pico.run_agent()` 新增 `task_mode="auto"` 和
  `router_model_client=None` 参数。
- `task_mode` 必须在建图前完成类型和枚举校验；不得静默接受未知值。
- `router_model_client` 未提供时复用 Coordinator 的 model client；提供时只用于
  `auto` 意图识别。
- `pico run` 和裸 `pico` 的 LangGraph 路径新增：

```text
--task-mode {auto,conversation,read_only,code_change}
--router-model MODEL
```

- `--router-model` 只在 `--backend langgraph --task-mode auto` 时生效；其他组合应明确忽略并提示，
  或在参数校验阶段拒绝，不能悄悄改变 native 行为。首版采用参数校验拒绝。
- `--research/--no-research` 改为三态输入：未提供时为 `None`，由意图默认值或 Router 决定；
  显式提供时覆盖 Router 的 `requires_research`。`conversation` 无论该参数为何值都不得启动 Research。
- 为保持现有 LangGraph 修改命令的行为，显式 `code_change` 且未提供 research override 时，
  `requires_research` 缺省为 `true`。
- native 是默认 backend；native 路径不得导入意图 Router 或 LangGraph 包，也不得接受 Router
  对其工具权限和控制流的影响。

**验收：**

- `python -m pico run --backend langgraph --task-mode conversation "你好"` 正常成功。
- `python -m pico run --backend langgraph --task-mode read_only "分析项目结构"` 只能读取。
- `python -m pico run --backend langgraph --task-mode code_change "修改 README"` 保持严格 Review。
- 旧命令不带 `--task-mode` 时使用 `auto`，不再因普通回答没有文件变化而失败。
- `python -m pico run "..."` 的 native 行为不变。

### F2：Intent Router 模型合同

- 只有 `task_mode="auto"` 才允许调用 Router 模型。
- Router 不使用 Pico AgentLoop、不拥有工具、不创建 session/checkpoint、不写 durable memory，
  只执行一次受约束的 `complete()` 分类调用。
- Router 输入包含当前任务、经过裁剪和脱敏的少量会话上下文，以及是否提供显式 review path；
  不得把 `Pico`、model client、sink 或其他运行时对象放入 graph state。
- 会话上下文最多使用最近 4 条消息，总长度上限 2000 字符；它只用于解析“继续”“按刚才方案修改”
  等多轮指代，不得写入公开指标。
- Router 输出必须是单个 JSON object，不接受 Markdown code fence 或前后解释：

```json
{"intent":"read_only","requires_research":true}
```

- `intent` 必须是 `conversation/read_only/code_change`；`requires_research` 必须是 JSON boolean；
  不接受 confidence、tool list、node name 等能够扩大权限或直接指定内部节点的字段。
- 解析使用 `json.loads()` 和字段白名单，不能用正则从自由文本猜测结果。
- 非法协议输出最多重试 1 次；第二次仍非法时安全降级为 `read_only`，并设置
  `requires_research=true`，除非调用方显式 `--no-research`。
- provider/网络异常不按 malformed output 处理，不得吞掉后继续运行；应沿现有 LangGraph
  model boundary 映射为 `model_error`。
- 显式非 auto 模式不得调用 Router，避免额外成本和非确定性。
- 显式 `focus_paths` 在 `auto` 模式下可作为强信号直接解析为 `code_change`，无需调用 Router；
  `acceptance` 文本本身不能单独扩大写权限。

### F3：LangGraph 状态与路由

`AgentState` 新增以下纯数据字段：

```python
requested_task_mode: str
resolved_intent: str
intent_source: str       # explicit | focus_path | router | fallback
intent_attempts: int
intent_context: str
completion_status: str   # pending | success | failed
```

- `review_status` 只表达 Patch Review 状态，不再表示整个图是否成功。
- `completion_status` 是 runner 判断成功/失败的唯一图级状态；成功任务必须为 `success`，所有稳定
  失败终态必须为 `failed`。
- `terminal_reason` 只保存失败原因；成功的 conversation/read-only/code-change 均保持空字符串。
- `plan` 改为 `intent_router` 节点。显式模式只做规范化；`auto` 才调用 Router。
- 路由固定为：

```text
START -> intent_router

intent_router -- conversation -----------------------------> answer
intent_router -- read_only + research ---------------------> research_delegate
intent_router -- read_only + no research ------------------> answer
intent_router -- code_change + research -------------------> research_delegate
intent_router -- code_change + no research ----------------> execute_change

research_delegate -- read_only ----------------------------> answer
research_delegate -- code_change --------------------------> execute_change

answer ----------------------------------------------------> finalize
execute_change -- no review path --------------------------> finalize(failed)
execute_change -- review path -----------------------------> review_delegate
review_delegate -- pass -----------------------------------> finalize(success)
review_delegate -- needs_fix and retries remain -----------> execute_change
review_delegate -- retry limit/delegate failure -----------> finalize(failed)
finalize --------------------------------------------------> END
```

- 条件边函数必须是纯函数，不得调用模型、工具或修改 state。
- 任意已有 `terminal_reason` 优先进入 `finalize`。

### F4：按意图强制能力边界

- `conversation` 由 answer 节点直接调用受模型边界包装的 `complete()`；不创建 Pico executor，
  因而不注册、不解析、也不执行任何工具协议。当前 Pico 明确拒绝空 allowlist，不能使用
  `allowed_tools=()` 伪装零工具 Agent。
- `read_only` 的 answer executor 只允许 `list_files/read_file/search`，并使用
  `read_only=True`、`approval_policy="never"`。
- `code_change` executor 复用 Coordinator 工具集合但去除 `delegate`，保留父审批策略和现有 sandbox。
- 三类 executor 均使用 `InMemorySessionStore`、父 session 深拷贝、
  `allow_checkpoint=False`、`allow_durable_memory_write=False`。
- Research delegate 只在 read-only/code-change 路由按需启动；Review delegate 只在
  code-change 且存在 review path 时启动。
- Router 输出不能提供 `allowed_tools`，也不能直接指定任意节点名。
- `conversation` 或 `read_only` 即使模型输出写工具协议，也必须被 allowlist/read-only 机制拒绝并
  记录 sandbox violation，不能升级为 code-change。

### F5：终态语义

- conversation 的纯模型回答或 read-only answer executor 返回非空最终答案后，
  `completion_status="success"`，
  不要求 `affected_paths`，不伪造 `review_status="pass"`。
- code-change 的 review pass 后，`completion_status="success"`。
- 只有 code-change 在 `affected_paths/focus_paths/artifact_path` 全部为空时，才使用
  `no_changes_to_review` 失败终态。
- read-only answer executor 意外产生 affected path 属于能力边界破坏，必须以 `runtime_error` 失败并记录审计事件，
  不能自动升级路由或进入 Patch Review。
- 空答案、预算不足、Router provider error、delegate failure 和节点异常继续映射为明确失败终态。
- `run_agent(record_session=True)` 仍只把原始用户输入和最终答案各写一次父 session；Router prompt、
  Research prompt 和内部 answer/execute prompt 不得写入父 history。

### F6：预算、审计和指标

- 现有 `coordinator_steps_used` 与 `within_budget` 的 tool-step 口径保持不变，避免破坏 benchmark。
- Router 模型调用单独由 `intent_attempts` 限制为最多 2 次，并计入 graph-level
  `TaskState.attempts` 和 evaluation 的聚合 `attempts`，但不伪装成 tool step。
- 显式模式和 focus-path 快速判定的 `intent_attempts` 必须为 0。
- 新增事件：

```text
intent_classification_requested
intent_classified
intent_classification_recovered
route_selected
answer_completed
```

- `intent_classified` 只记录规范枚举、来源、attempt 数和 `requires_research`，不得记录完整 Router
  prompt、会话上下文或原始非法输出。
- malformed Router 输出增加一次 `malformed_output_recovered`；如果第一次失败、第二次成功，只增加一次；
  两次失败并 fallback 也只增加一次。
- EventSink 仍是旁路观测；控制流读取 graph state，不能读取 JSONL。
- evaluation artifact v2 可增量增加以下 row 字段，旧消费者可忽略：

```text
requested_task_mode
resolved_intent
intent_source
intent_attempts
```

- 本期不提升 artifact schema major version；新增字段必须有默认值，旧 native row 使用空字符串/0。

### F7：benchmark 与兼容性

- `LangGraphBackendRunner.run_task()` 必须显式传 `task_mode="code_change"`，不得使用 auto Router。
- 既有 `delegate_tasks.json` 不要求增加 task mode 字段，避免 fixture churn。
- benchmark 的 FakeModel 响应序列不得因为 Router 多一次模型调用而改变。
- 原有“无 review path → `no_changes_to_review`”测试继续保留，且明确属于 code-change 合同。
- native backend、native delegate schema、主包零运行时依赖和 LangGraph 懒加载边界保持不变。
- 未安装 example/LangGraph 时，native CLI 和 native tests 必须正常。

---

## 非功能需求

- 意图路由必须可审计、可测试、可被调用方显式覆盖。
- 模型分类不能成为权限边界；工具 allowlist、read-only、审批和 workspace sandbox 才是权限边界。
- Router 最多两次短输出调用，默认 `max_new_tokens` 不超过 96。
- 不基于中英文关键词正则做主路由，不把 confidence 当成授权依据。
- graph state 保持可序列化，为后续 checkpointer 留出边界，但本期仍不宣称 durable replay。
- 所有新增异常路径必须完成 graph-level TaskState 生命周期并生成 report/trace。

---

## 不在本期范围内

- 新增第四个自治 Agent。
- Answer Reviewer 或对普通聊天强制 Review。
- Router 自主选择工具、修改权限或任意图节点。
- 多意图并行图、群聊式 Agent 协商。
- Kafka/OpenTelemetry sink 实现。
- 持久 LangGraph checkpointer 和跨进程 replay。
- 修改 native AgentLoop 的默认控制流。

---

## 总体验收

1. “你好”通过 LangGraph auto 入口返回答案，不触发 Research/Review，不产生文件变化。
2. “分析当前项目的测试结构”解析为 read-only，只调用只读工具，可按需调用 Research。
3. “先分析再修改 README”解析为 code-change，执行 Research → Execute → Review。
4. 显式 code-change 未产生 review path 时仍失败为 `no_changes_to_review`。
5. Router 非法输出可恢复或安全降级，不能获得写权限。
6. benchmark 不调用 Router，既有 LangGraph 任务结果与指标语义保持一致。
7. native 全套回归通过，未安装 LangGraph 时 native 可独立运行。
