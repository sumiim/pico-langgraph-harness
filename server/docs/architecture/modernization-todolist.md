# Pico Web Agent 现代化改造 TODO

> 状态：核心方案已确认，可以进入 Phase 0；带箭头的选择按里程碑渐进交付。
>
> 文档关系：第一版实施范围以 [`docs/requirements-v1.md`](../requirements-v1.md) 为准；本文作为 V1 之后的演进 backlog，不得反向扩大 V1 验收范围。
>
> 原则：优先复用现有 AgentLoop、LangGraph wrapper、prompt prefix、context manager、memory、session、checkpoint、EventSink、TaskState、run artifacts、评测与安全策略；新增能力通过适配层接入，不把业务逻辑重写进 FastAPI 路由或 React 组件。

## 1. 已确认决策

- [x] 前端采用 React + Vite + TypeScript。
- [x] 第一阶段交付普通 Web 应用，不做 Electron/Tauri 打包。
- [x] UI 组件采用 Ant Design；TailwindCSS 只负责布局与少量原子样式。
- [x] 通信采用 REST + SSE；取消、审批和追加消息使用 REST，流式输出与状态事件使用 SSE。
- [x] 后端采用 FastAPI，但保留 Python 核心运行时。
- [x] 首个模型后端为 Right Code GPT，沿用 OpenAI-compatible `/responses` 接口。
- [x] 编排目标为单机多 worker，不以多服务器部署为首期目标。
- [x] 工具执行默认不可信，使用独立 Docker worker 容器隔离。
- [x] 保留既有 prompt 编排的语义、记忆与上下文压缩能力。
- [x] 长期保留 CLI，并让 CLI 与 Web 复用同一 application service。
- [x] `native` backend 暂时保留；只有 LangGraph 完整覆盖、迁移验证通过且确认无外部依赖后，才单独评估弃用。
- [x] 身份能力按“本机单用户 -> 远程单用户 -> 多用户”演进，不在首期提前实现完整多租户。
- [x] 工作区首期只选择安全根目录中的已有项目，后续增加受控 Git URL 克隆；不规划 ZIP 上传。
- [x] 首期使用 SQLite；进入多用户阶段前重新评估并默认迁移 PostgreSQL。
- [x] 调度按“进程内 + 持久状态 -> Redis + 独立 worker”演进。
- [x] `container_sandbox` 默认禁网，按任务、目标和有效期审批临时网络能力。
- [x] 工具采用风险分级审批，不提供突破 `policy_boundary` 或 `container_sandbox` 的全局放行。
- [x] 模型可见工具固定为 `read/write/edit/bash`；`read` 结构化聚合 read/list/search。
- [x] Git 项目使用独立 worktree；非 Git 项目使用独立目录快照。
- [x] Planning 首期只展示，后续增加执行前可选批准，不提供直接表格式编辑。
- [x] Swarm 由系统自动规划并行，用户可以限制最大 Agent 数、并发和预算。
- [x] Right Code 首期由服务端环境变量配置，后续增加管理员模型 allowlist 供前端选择。
- [x] 生产基准为 Linux Docker，同时支持 Windows Docker Desktop 本地开发。
- [x] 暴露版本化 Skills API；后续实现受评测、审批和回滚约束的 Skills 自进化。

## 2. 目标架构

```text
React Web
  |-- REST: sessions / tasks / approvals / cancel / messages
  `-- SSE: tokens / tools / plan / agents / lifecycle
             |
          FastAPI
             |
      Application Service
       |             |
  Event Broker   Coordinator / LangGraph
                       |
                Local Worker Scheduler
                 |       |       |
              Worker A Worker B Worker C
                 |       |       |
              isolated Docker sandboxes
                       |
        Tool Registry / Skills Registry / MCP
```

控制面与执行面必须分离：FastAPI、模型凭据、计划与调度属于控制面；模型生成的命令、文件操作和项目测试属于不可信执行面。

### 2.1 核心术语与标识关系

- `policy_boundary`：现有代码提供的策略边界，包括工作区路径校验、只读限制、工具 allowlist、审批和环境变量过滤；它不是容器隔离。
- `policy_violation`：违反上述策略边界的事件。迁移期可读取旧 `sandbox_violation`，但新 API/SSE/UI 不再把它展示成 Docker sandbox 违规。
- `container_sandbox`：Phase 4 引入的 Docker 执行隔离，具备独立文件系统、进程、网络和资源限制。
- `container_sandbox_violation`：容器创建、挂载、网络、资源或逃逸策略被拒绝的事件，必须与 `policy_violation` 分开统计和展示。
- `Session`：连续对话及上下文容器；一个 Session 可以包含多个 Task。
- `Workspace`：经过安全根目录校验的项目工作区，拥有稳定 `workspace_id`；Session 创建后绑定一个 Workspace，M1 不允许原地切换。
- `Task`：一次逻辑请求，拥有稳定 `task_id`；用户请求创建 root Task，Research/Review/Swarm 委派创建带 `root_task_id/parent_task_id` 的内部 child Task。
- `Run`：Task 的一次执行尝试，拥有独立 `run_id` 和 artifacts；一个 Task 可以因首次执行、重试或恢复产生多个 Run。
- 恢复 Session 默认创建新 Task 并继承允许的会话上下文；重试或断点恢复同一 Task 创建新 Run，不覆盖旧 Run。

## 3. Phase 0：合同冻结与回归基线

- [ ] 记录当前 `native` 和 `langgraph` 公共入口、CLI 参数及返回行为。
- [ ] 为现有 prompt prefix、memory、context reduction、session resume 建立快照或合同测试。
- [ ] 固化现有安全不变量：路径逃逸、符号链接逃逸、只读模式、审批拒绝、环境变量过滤。
- [ ] 为旧 `sandbox_violation` 建立兼容测试：读取时映射为 `policy_violation`，不得伪造成 `container_sandbox_violation`。
- [ ] 固化现有 Agent 审计事件及 `task_state.json`、`trace.jsonl`、`report.json` 的兼容字段。
- [ ] 跑通当前单元测试和 benchmark，保存改造前基线。
- [ ] 定义弃用策略：旧 `<tool>...</tool>` 解析器在迁移期保留，仅作为 legacy adapter。
- [ ] 编写 Workspace/Session/Task/Run ADR，冻结 Workspace 绑定、`Session 1:N root Task`、Task 父子关系、`Task 1:N Run`、恢复语义、ID 生成和 artifact 归属。
- [ ] 在上述 ADR 中冻结 Checkpoint 恢复映射：新 Run 记录 `source_run_id/source_checkpoint_id`，源 Run/Checkpoint 保持不可变，并在恢复前验证 Workspace 与 runtime identity。
- [ ] 在上述 ADR 中冻结现有 `child_task_states` 的导入与事件映射：每个 child state 映射为内部 child Task + Run，并保留 `root_task_id/parent_task_id/parent_run_id/agent_id`。
- [ ] 编写 Scheduler/TaskRunner ADR，冻结进程内实现与未来 Redis worker 的共同接口、lease、幂等和状态所有权。
- [ ] 编写 Approval ADR，冻结 CLI 同步适配与 Web 异步挂起/恢复、超时、取消、重复决策和持久化语义。
- [ ] 编写 M1 流式策略 ADR：区分 SSE 事件流与 provider token streaming；不把完整文本人工切片伪造成 `message.delta`。
- [ ] 编写持久化 ADR，冻结 SQLite、现有 JSON 数据导入、文件 artifacts 和失败恢复边界。
- [ ] 编写安全术语 ADR，把 `policy_boundary` 与 `container_sandbox` 的事件、指标、UI 标签和兼容映射分开。
- [ ] 编写其他架构决策记录：通信协议、工具合同、worker 模型和状态所有权。

验收：不修改功能的基线分支能够重复运行；后续每个阶段均能证明未破坏受保护的旧能力。

## 4. Phase 1：后端应用层与 FastAPI

### 4.1 分层

- [ ] 新增 application service，统一封装创建任务、恢复会话、取消任务、审批工具和查询状态。
- [ ] FastAPI 路由只做 HTTP 校验、鉴权、调用 application service 和响应转换。
- [ ] Application Service 的公开接口同时兼容 Web 与未来 CLI adapter，但 M1 只要求 FastAPI 接入；CLI 迁移见 §4.8。
- [ ] 定义 `TaskRunner` 与 `Scheduler` 接口；Phase 1 使用进程内实现，Phase 6 的 Redis/独立 worker 实现不得改变 REST 合同。
- [ ] Web 默认使用 `langgraph` backend；`native` 保留给 CLI、回归测试和显式兼容模式。
- [ ] 使用 Pydantic 模型定义公开 API，并生成 OpenAPI 文档。
- [ ] 为错误响应定义稳定的 `code/message/details/request_id` 合同。
- [ ] 首期数据模型预留 nullable `owner_id/tenant_id`，但本机单用户模式不得伪装成已实现租户隔离。

### 4.2 REST API

- [ ] `GET /api/v1/workspaces`：只列出后端配置的安全根目录下、当前调用方可选择的已有 Workspace。
- [ ] `POST /api/v1/workspaces/resolve`：只接受 `root_id + relative_path`，解析真实路径、拒绝逃逸/符号链接越界，并返回稳定 `workspace_id`。
- [ ] `POST /api/v1/sessions`：必须携带 `workspace_id` 创建会话；Session 绑定在 M1 内不可变，切换项目必须新建 Session。
- [ ] `GET /api/v1/sessions`：分页查询会话。
- [ ] `GET /api/v1/sessions/{session_id}`：会话详情与消息摘要。
- [ ] `POST /api/v1/tasks`：请求体携带 `session_id`，继承其 `workspace_id` 并创建新 root Task 及首次 Run；Task 请求体不得覆盖 Workspace 路径。
- [ ] `GET /api/v1/tasks/{task_id}`：查询逻辑任务、当前状态和 latest Run 摘要。
- [ ] `GET /api/v1/tasks/{task_id}/runs`：按时间查询该 Task 的全部执行尝试。
- [ ] `POST /api/v1/tasks/{task_id}/runs`：显式重试或从检查点恢复，并创建新的 `run_id`，不得覆盖旧 Run。
- [ ] `GET /api/v1/runs/{run_id}`：查询单次执行快照、终态和 stop reason。
- [ ] `POST /api/v1/tasks/{root_task_id}/cancel`：幂等取消 root Task 的 active Run 及全部活动后代，并在 root Task 上聚合最终状态。
- [ ] `POST /api/v1/tasks/{root_task_id}/approvals/{approval_id}`：处理 root 或 child Run 的审批；approval 自身携带实际 `task_id/run_id/tool_call_id`。
- [ ] `GET /api/v1/runs/{run_id}/artifacts`：列出该 Run 的 trace、report、patch 等产物。
- [ ] `POST /api/v1/tasks/{task_id}/messages` 不进入 M1；后续若支持运行中 steering，必须另行定义消息顺序、可接受状态和 checkpoint 语义，不能借此隐式“恢复 Session”。
- [ ] 后续里程碑增加受控 Git URL 克隆入口，只允许明确协议并实施 SSRF、凭据和目标路径校验。
- [ ] `GET /health/live` 与 `GET /health/ready`：存活和依赖就绪检查。

### 4.3 SSE

- [ ] `GET /api/v1/tasks/{root_task_id}/events` 提供 root Task 及其后代的统一 SSE 事件流。
- [ ] root Task 的 SSE 流复用传输其全部 child Task/Run 事件；定义统一信封：`event_id/root_task_id/task_id/run_id/parent_task_id/parent_run_id/agent_id/type/timestamp/sequence/data`。
- [ ] M1 至少支持 `task.*`、`run.*`、`model.started`、`message.completed`、`tool.*`、`approval.*`、`plan.*`、`agent.*`、`policy.violation` 和 `error`。
- [ ] 只有 provider 确实返回 token/chunk 流时才发送 `message.delta`；M1 非流式 `/responses` 路径只发送 lifecycle 与 `message.completed`，前端不得伪造打字流。
- [ ] `container_sandbox.*` 事件只有 Phase 4 真正启用容器隔离后才允许发送；M1 不得生成占位 sandbox 事件。
- [ ] 使用单调递增序号，支持 `Last-Event-ID` 断线续传和事件重放。
- [ ] 配置 heartbeat，处理代理缓冲、断线和慢消费者。
- [ ] SSE 断开不得自动取消 Agent；取消只能通过显式 REST 命令。
- [ ] 将现有 EventSink 适配为事件发布源，而不是在 AgentLoop 内耦合 HTTP。

### 4.4 Web 异步审批

- [ ] 定义 `ApprovalStrategy` 接口；CLI adapter 可以同步等待终端输入，Web adapter 必须使用持久化异步决策。
- [ ] 风险工具发起后将 Task/Run 转为 `waiting_for_approval`，持久化 pending approval，再通过 SSE 发出 `approval.required`。
- [ ] REST 批准或拒绝必须校验 task、run、call、审批状态与调用方权限，并保证重复请求幂等。
- [ ] child Task 的审批记录必须携带实际 `task_id/run_id/tool_call_id/agent_id`，通过 root Task 页面统一展示；决策不得误授权同一 root 下的其他 child Run。
- [ ] 决策后通过内部通知唤醒挂起的 TaskRunner；FastAPI 请求线程不得承担 AgentLoop 的阻塞等待。
- [ ] 审批等待支持超时、任务取消和 stale approval 拒绝；服务重启后若无法恢复原执行栈，则旧 Run 标记 `interrupted`、原审批失效，由用户重试产生新 Run。
- [ ] CLI 与 Web 审批最终写入同一审计合同，记录策略、决策方、时间、范围和理由，但不记录 secret。
- [ ] M1 Web 的审批策略固定为 `per_call_only`；Q6 的 `per_task_class` 授权属于 container sandbox 上线后的长期能力，不进入 M1。

### 4.5 TaskRunner 与取消语义

- [ ] 为每个任务建立 cancellation token。
- [ ] 同步 AgentLoop 必须运行在 API event loop 之外；首期可使用受控后台线程/进程，但只能由 TaskRunner 管理。
- [ ] 在 AgentLoop 每轮、模型调用前后、LangGraph 节点、审批等待、工具调用和子 Agent 边界检查 cancellation token。
- [ ] 在模型调用、图节点、工具执行、worker 调度和子 Agent 边界传播取消信号。
- [ ] 将不可取消的 `subprocess.run` 路径改造为可追踪进程组；取消时终止完整进程树和对应 `container_sandbox`，禁止留下后台子进程。
- [ ] 为无法主动中止的模型 HTTP 请求设置有限超时；取消后丢弃迟到结果，不再执行后续工具。
- [ ] 定义 `cancel_requested -> cancelling -> cancelled` 状态迁移。
- [ ] 规定不可中断操作的超时和最终一致性行为。

### 4.6 SQLite 与文件数据迁移

- [ ] SQLite 作为新 application service 的可变控制状态存储，保存 Session/Task/Run 索引、消息、审批、状态迁移和 SSE sequence。
- [ ] `trace.jsonl`、`report.json`、patch、日志和其他大体积 Run artifacts 继续按 Run 落盘；SQLite 只保存路径、摘要、大小、校验和及状态。
- [ ] 为现有 SessionStore/RunStore JSON 数据提供幂等导入器，保留原始文件并记录导入版本。
- [ ] 现有每份 TaskState 在首次导入时映射为一个 Task 和一个 Run；重复导入必须保持相同 ID 映射。
- [ ] 现有 `child_task_states` 导入为内部 child Task/Run；child artifacts 独立归属其 Run，同时可从 root Task 索引查询。
- [ ] 迁移期定义唯一写入者和 reconciliation 检查，不允许无限期无约束双写。
- [ ] CLI 切换到 application service 后与 Web 写入同一 SQLite/文件存储合同；legacy 读取适配仅用于迁移与回归。
- [ ] 增加迁移中断、重复导入、缺失 artifact、损坏 JSON 和数据库回滚测试。

### 4.7 LangGraph 打包与 Phase 1 依赖

- [ ] 将 `examples/langgraph-pico` 从示例提升为正式受支持后端，优先迁入 `pico.backends.langgraph`，不再要求第二次 editable install。
- [ ] 在主 `pyproject.toml` 定义 `langgraph` 可选依赖组；缺少依赖时 CLI 显式选择 LangGraph 应返回可操作的安装提示。
- [ ] 定义 `web` 可选依赖组，至少包含 `fastapi`、`uvicorn`、`sqlalchemy`、`aiosqlite`、`sse-starlette` 及 Web 默认所需的 `langgraph`。
- [ ] 锁定并测试兼容版本范围；开发、CI 和 Docker 构建使用同一 lock/constraints 来源，避免只修改声明不更新部署环境。
- [ ] 基础 CLI 安装继续允许不携带 Web 依赖；提供 `pico[web]`（包含 LangGraph）和需要时的 `pico[all]` 安装入口。
- [ ] 更新 package discovery、公开 import、测试矩阵和发布构建，证明 wheel/sdist 中确实包含 LangGraph backend。

### 4.8 CLI Application Service 迁移（M1 非阻塞）

- [ ] M1 期间旧 CLI 可继续直接调用现有 runtime，以保持回归面稳定。
- [ ] M1 完成后增加 CLI adapter，逐步复用 Workspace、Session/Task/Run、ApprovalStrategy 和 TaskRunner 合同。
- [ ] CLI adapter 切换前后运行同一合同测试；迁移不得改变既有命令参数、脚本退出码和 benchmark 语义。
- [ ] CLI 全面切换完成后再移除重复入口逻辑；不得为了 M1 进度提前删除旧路径。

验收：API 可创建任务并通过 SSE 完整观察运行；刷新页面可续传；取消能够在规定时间内停止模型、工具及子 Agent。

## 5. Phase 2：React Web 前端

### 5.1 工程基础

- [ ] 使用 pnpm workspace 管理前端，并锁定 Node/pnpm 版本。
- [ ] 初始化 Vite + React + TypeScript。
- [ ] 集成 Ant Design、TailwindCSS、路由、请求层和测试框架。
- [ ] 生成 OpenAPI TypeScript client，避免手工重复维护 DTO。
- [ ] 配置 ESLint、格式化、类型检查、单元测试和生产构建。

### 5.2 M1 必需工作区

- [ ] 左侧任务/会话列表，首期支持新建、选择和恢复。
- [ ] 中央对话流渲染用户消息、真实模型输出、工具调用和错误状态；M1 支持 `message.completed`，并为后续真实 `message.delta` 保留增量渲染能力。
- [ ] 任务状态区展示运行、等待审批、取消中、完成和失败。
- [ ] 工具调用支持折叠详情、耗时、输入摘要和输出摘要；明确显示 `policy_boundary`，不得伪装成 Docker 隔离。
- [ ] 审批请求提供批准、拒绝及风险说明，防止重复提交。
- [ ] 首期以简洁事件流展示 LangGraph route/research/execute/review 状态，不承诺完整计划树或 Agent 树。
- [ ] 文件变更首期展示 affected paths 和 artifact 链接。
- [ ] 支持停止按钮；发送取消后立即显示 `cancelling`，等待服务端确认。
- [ ] 处理 SSE 重连、事件去重、乱序保护和历史事件补齐。
- [ ] 固定显示“开发模式：工具在宿主机执行”的安全警告，直到 `container_sandbox` 真正启用。

### 5.3 M1.1 增强工作区

- [ ] 会话支持重命名、搜索、归档和分页筛选。
- [ ] 计划视图展示步骤、依赖、负责人、状态及失败原因。
- [ ] Agent 视图展示 Coordinator、worker/sub-agent 的父子关系和运行状态。
- [ ] 文件变更视图展示统一 diff、单文件查看和 Review 结果。
- [ ] 容器沙盒上线后显示 `container_sandbox` id、镜像、网络状态和资源限制摘要。

### 5.4 前端安全与体验

- [ ] Markdown 与代码渲染执行严格 HTML 清理。
- [ ] 不在浏览器持久化模型 API Key。
- [ ] 明确刷新、断网、后端重启和任务已结束时的恢复体验。
- [ ] 为桌面和移动宽度设置稳定布局；首期以桌面工作流为主要验收目标。
- [ ] 完成键盘可访问性、焦点管理和审批对话框可访问性测试。

验收：用户可从 Web UI 完成创建任务、观察流式执行、审批、取消、恢复会话和查看变更的完整闭环。

## 6. Phase 3：标准化模型与工具合同

### 6.1 内部合同

- [ ] 定义 provider 无关的 `ModelRequest/ModelResponse/ToolCall/ToolResult/Usage` 数据模型。
- [ ] 使用标准 JSON Schema（Draft 2020-12 子集）定义工具参数。
- [ ] 用 Pydantic 或等价验证器在执行前校验参数，拒绝额外字段和非法范围。
- [ ] Tool Result 统一包含 call id、状态、结构化输出、错误、耗时、`policy_boundary` metadata 和可选 `container_sandbox` metadata。
- [ ] 区分可重试模型协议错误、工具业务错误、审批拒绝、`policy_violation`、`container_sandbox_violation` 和系统错误。

### 6.2 Right Code / OpenAI-compatible adapter

- [ ] 扩展现有 `/responses` 客户端，发送原生 `tools` 和 `tool_choice`。
- [ ] 解析原生 function/tool calls，不再要求模型输出 `<tool>` XML。
- [ ] 正确回传 tool result，并维持 provider response/call id 关联。
- [ ] 支持流式文本与流式 tool arguments；对不完整参数做严格失败处理。
- [ ] 保留 prompt cache、usage 和脱敏审计元数据。
- [ ] 使用 fake server 编写 provider 合同测试，真实 Right Code 测试作为可选集成测试。

### 6.3 Tool Registry

- [ ] 建立集中式 Tool Registry，统一注册、Schema、权限、审批、执行和审计。
- [ ] 将模型可见的内置执行工具收敛为 `read`、`write`、`edit`、`bash`。
- [ ] `read` 通过结构化 `operation=read/list/search` 聚合文件读取、列目录与搜索；read-only Agent 不因检索获得 `bash` 权限。
- [ ] `write` 负责完整文件写入并提供大小、编码和覆盖保护。
- [ ] `edit` 使用可审计的精确 patch/diff 语义，避免模糊字符串替换。
- [ ] 生产/无人值守模式的 `bash` 只能在 `container_sandbox` 内运行，并施加命令超时、输出上限和进程树终止；M1 宿主机开发例外遵守 §15 的硬限制。
- [ ] 将 `delegate` 从普通文件工具中移出，作为编排层的 Agent 调度动作。
- [ ] legacy 工具名通过兼容映射迁移，不立即破坏已有测试和 benchmark。

### 6.4 MCP（非首期阻塞项）

- [ ] 定义 MCP client 与 Tool Registry 的适配边界。
- [ ] 对 MCP server 设置独立 allowlist、凭据、超时、审批和审计策略。
- [ ] 外部 MCP 工具不得默认继承内置工具或宿主机权限。
- [ ] 先完成一个只读 MCP 集成验证，再评估写权限工具。

验收：Right Code GPT 使用原生工具调用完成读、写、编辑和测试任务；AgentLoop 不再依赖模型生成手工 XML/JSON 合同。

## 7. Phase 4：Docker 工具沙盒

### 7.1 威胁模型

- [ ] 编写威胁模型文档：不信任模型输出、仓库内容、依赖脚本和工具参数；信任宿主机管理员与 Docker/OS 边界。
- [ ] 明确非目标：首期不承诺抵御 Docker daemon、容器运行时或内核零日漏洞。
- [ ] 所有模型 API Key、FastAPI 凭据和宿主机敏感环境变量不得进入 `container_sandbox`。

### 7.2 Container Sandbox 生命周期

- [ ] 定义 `ContainerSandboxManager` 接口：create/exec/cancel/destroy/inspect。
- [ ] 每个任务或 worker 使用独立临时容器，禁止跨任务复用可写层。
- [ ] 使用非 root 用户、只读 rootfs、`no-new-privileges` 并删除非必要 capabilities。
- [ ] 禁止 privileged、host network、host PID/IPC 和 Docker Socket 挂载。
- [ ] 只挂载任务工作区及受控临时目录；验证 bind mount 的宿主机真实路径。
- [ ] 默认关闭网络；按任务和域名策略审批临时网络能力。
- [ ] 限制 CPU、内存、PIDs、临时磁盘、命令时间和输出大小。
- [ ] 取消或结束任务时销毁容器并清理资源。
- [ ] Docker 不可用时 fail closed，不静默回退到宿主机执行。

### 7.3 文件与变更隔离

- [ ] 为并行 worker 创建独立 Git worktree 或独立 workspace snapshot。
- [ ] `container_sandbox` 仅对分配工作区可写，源码仓库主工作区不直接暴露给并行 Agent。
- [ ] worker 以 patch/commit artifact 返回变更，不直接覆盖 Coordinator 工作区。
- [ ] 合并前检查路径权限、冲突、测试结果和审批要求。

### 7.4 验证

- [ ] 增加路径逃逸、符号链接、挂载逃逸和环境变量泄漏测试。
- [ ] 增加 fork bomb、无限输出、超时、后台进程和取消清理测试。
- [ ] 增加无网络、网络审批和 DNS/代理绕过测试。
- [ ] 增加跨 worker 读取、写入和进程干扰测试。

验收：所有内置工具执行均可证明发生在指定 `container_sandbox` 中；策略越权产生 `policy_violation`，容器边界拒绝产生 `container_sandbox_violation`；任何异常退出不遗留 worker 容器。

## 8. Phase 5：结构化 Planning

- [ ] 新增 Planner 节点，但不请求、存储或展示模型原始 Chain of Thought。
- [ ] 定义结构化 Plan Schema：目标、步骤、依赖、状态、负责人、输入、预期产物和验收条件。
- [ ] 简单 conversation/read-only 请求允许跳过重型计划。
- [ ] code change 和 swarm 任务在执行前生成可验证计划。
- [ ] 计划必须支持增量修订，并记录 revision 与修订原因。
- [ ] 首期计划只展示并允许暂停/取消；后续增加可选的执行前批准，批准等待必须支持取消和超时。
- [ ] Coordinator 只能调度依赖已满足且权限允许的步骤。
- [ ] 将计划状态写入 TaskState/EventSink，并通过 SSE 推送。
- [ ] Review 结果可以生成修复步骤，但必须受重试和预算上限约束。
- [ ] 建立计划质量评测：覆盖率、可执行性、依赖正确性、返工次数和计划漂移。

验收：前端能展示可解释的任务计划和实际进度；系统不依赖暴露 CoT 才能调度或审计。

## 9. Phase 6：单机多 Worker 编排

- [ ] 定义 Scheduler、Worker、Lease、Heartbeat、Retry 和 Result contracts。
- [ ] 选择本机队列/状态后端，并保证 FastAPI 重启后任务状态可恢复。
- [ ] 使用独立 worker 进程或容器执行图节点，避免长任务阻塞 API event loop。
- [ ] 支持 fan-out/fan-in、并发上限、优先级、取消传播和超时。
- [ ] 为任务、步骤、worker、`container_sandbox` 和 model call 统一关联 ID。
- [ ] worker 丢失时回收 lease；只有幂等或有检查点的步骤允许自动重试。
- [ ] 对模型 token、并发 worker、工具时间和总 wall time 设置预算。
- [ ] 定义背压策略，避免大量 SSE 事件或任务压垮 API。
- [ ] 记录队列等待、执行耗时、失败类型、重试与资源使用指标。

验收：一台机器可并发执行多个互不干扰的步骤；任一 worker 失败不会破坏其他任务；取消能够传播到所有后代步骤与容器。

## 10. Phase 7：Agent Swarm

- [ ] 在现有 Coordinator/Research/Review 基础上定义可扩展角色合同，而不是复制多个无边界 AgentLoop。
- [ ] Coordinator 根据结构化计划拆分任务并确定并行依赖。
- [ ] 优先并行只读调研；写任务按模块/文件所有权分配。
- [ ] 每个子 Agent 使用最小工具 allowlist、独立上下文、预算和 `container_sandbox`。
- [ ] 子 Agent 不得自行扩大权限或无限递归创建 Agent。
- [ ] 对最大深度、最大 fan-out、总 token 和总工具调用设置硬限制。
- [ ] 子 Agent 返回结构化产物、证据、patch、测试结果和未解决风险。
- [ ] Coordinator 执行确定性的结果聚合与冲突检测。
- [ ] 变更合并后统一运行集成测试，再交给独立 Review Agent。
- [ ] Review 不直接修改代码；需要修复时生成有界修复步骤。
- [ ] 前端展示 Agent 树、负责人、状态、耗时和取消范围。
- [ ] 增加串行/并行消融评测，证明 swarm 对质量或耗时确有收益。

验收：至少一个可拆分任务能由多个 Agent 并行完成并安全合并；冲突、失败、取消和预算耗尽都有确定终态。

## 11. Phase 8：Skills API 与自进化

### 11.1 Skills 合同与注册表

- [ ] 明确 Tool 与 Skill 的边界：Tool 是原子执行能力；Skill 是可复用的任务方法、提示编排、工具约束和验收合同。
- [ ] 定义 `SkillManifest`：稳定 id、名称、版本、说明、输入/输出 JSON Schema、适用条件、允许工具、资源引用、预算和验收方式。
- [ ] Skill 版本不可原地覆盖；状态至少包含 `draft/candidate/active/deprecated/rejected`。
- [ ] 建立 Skills Registry，支持发现、按版本解析、启停、权限过滤和依赖检查。
- [ ] API、CLI、Coordinator 和前端复用同一个 Skills application service。
- [ ] Skill 选择采用按需检索，不把全部 Skill 正文塞入每次模型上下文。
- [ ] 显式调用 Skill 时仍创建普通 task/run，并复用取消、审批、SSE、审计和 artifact 合同。
- [ ] Skill 声明的工具集合只能缩小调用方已有权限，不能扩大用户、Agent、`policy_boundary` 或 `container_sandbox` 权限。
- [ ] Skill 资源、模板和脚本必须经过路径校验；可执行内容只能在 `container_sandbox` 中运行。
- [ ] 为 Skill 列表、详情、版本、调用、发布和回滚建立 OpenAPI 与合同测试。
- [ ] `GET /api/v1/skills`：分页查询当前调用方可见的 Skill 及启用状态。
- [ ] `GET /api/v1/skills/{skill_id}`：查询 Skill 元数据、当前版本、输入输出合同和权限摘要。
- [ ] `GET /api/v1/skills/{skill_id}/versions`：查询不可变版本、来源、评测结果和发布状态。
- [ ] `POST /api/v1/skills/{skill_id}/runs`：显式调用已发布 Skill，并返回标准 task id。
- [ ] 管理端 API 支持创建候选版本、启停、发布和回滚；普通任务调用方不得直接修改 Skill。

### 11.2 Skills 前端

- [ ] 增加 Skills 列表与详情视图，展示说明、版本、来源、权限、评测和启用状态。
- [ ] 支持从已发布 Skill 创建任务，并根据输入 Schema 生成表单或结构化参数编辑器。
- [ ] 管理视图展示候选版本 diff、风险变化、评测对比和发布/拒绝操作。
- [ ] 版本回滚必须二次确认，并说明将影响的新任务；已运行任务继续绑定原版本。

### 11.3 受控自进化

- [ ] 自进化只生成新的 candidate 版本，不得修改 active 版本或静默发布。
- [ ] 定义候选来源：用户明确要求、重复失败模式、评测回归、缺失工作流或管理员反馈。
- [ ] 生成候选时记录父版本、触发证据、模型、prompt 摘要、数据来源和变更理由。
- [ ] 禁止将 secret、完整私有对话或未经脱敏的项目内容固化进 Skill。
- [ ] 对 manifest、Schema、引用、权限变化和脚本执行做静态验证。
- [ ] 在隔离 `container_sandbox` 中运行单元测试、合同测试、安全用例和代表性 eval 数据集。
- [ ] 与 active 版本对比成功率、耗时、token、工具调用、权限范围和回归结果。
- [ ] 自动候选不得增加工具、网络、目录、secret 或 Agent fan-out 权限；权限扩大必须由管理员显式修改并审批。
- [ ] 首期发布必须人工批准；后续即使支持自动晋升，也只能针对权限不增、达到阈值且通过 canary 的低风险变更。
- [ ] 发布使用原子指针切换，保留完整 provenance、签名/摘要、审批人、时间和评测 artifact。
- [ ] 支持一键回滚、自动回归熔断和 candidate 隔离；失败候选不得影响 active Skill。
- [ ] 多用户阶段增加 Skill 所有者、租户可见性、共享范围和跨租户训练数据隔离。

验收：外部调用方可通过 REST/CLI 发现并运行固定版本 Skill；系统能从失败证据生成候选，经 `container_sandbox` 与 eval 验证、人工批准后发布，并能无损回滚。

## 12. Phase 9：Docker Compose、交付与运维

- [ ] 提供后端、前端、状态/队列组件的开发与生产 Dockerfile。
- [ ] 提供 `compose.yaml`，区分控制面容器与不可信 worker/`container_sandbox`。
- [ ] 前端生产构建由 Nginx 或等价静态服务托管，并正确关闭 SSE 代理缓冲。
- [ ] 配置持久卷，仅保存会话、事件、run artifacts 和必要缓存。
- [ ] 提供 `.env.example`，区分公开配置、服务端 secret 和 `container_sandbox` 可见变量。
- [ ] 增加数据库迁移、启动检查和兼容版本检查。
- [ ] 增加结构化日志、metrics、trace correlation 和基础告警。
- [ ] CI 执行 Python、前端、API contract、container sandbox integration 和端到端测试。
- [ ] 编写本地启动、升级、备份、恢复和故障排查文档。

### 12.1 身份与部署演进门槛

- [ ] 里程碑 A：服务只监听 loopback 或受控本地网络，按单用户模式交付，不宣称具备公网安全性。
- [ ] 里程碑 B：远程单用户模式加入 TLS、登录、会话管理、CSRF/CORS、安全 Cookie、限流和安全响应头。
- [ ] 里程碑 C：多用户模式加入 PostgreSQL、用户与租户模型、逐租户工作区/凭据/任务隔离、配额和管理员审计。
- [ ] 从 B 进入 C 前完成 SQLite 到 PostgreSQL 的迁移演练、回滚方案和数据一致性验证。
- [ ] 对每个多租户 API、SSE 订阅、artifact 下载和 worker 调度路径编写跨租户越权测试。

### 12.2 模型配置演进门槛

- [ ] 里程碑 A：Right Code base URL、模型名和 API Key 仅从服务端环境读取，secret 不下发浏览器。
- [ ] 里程碑 B：管理员维护模型 allowlist，普通用户只能选择允许的模型；API Key 仍由服务端持有。

验收：新环境可通过文档化命令启动完整系统；升级后旧会话与审计记录仍可读取。

## 13. 跨阶段质量门槛

- [ ] 每阶段合并前必须通过原有回归测试。
- [ ] 新增公开合同必须有 schema/contract test。
- [ ] 权限边界必须由代码强制，不能依赖 prompt 自律。
- [ ] 日志、事件和 artifact 必须经过 secret redaction。
- [ ] 所有后台任务必须具备取消、超时和资源清理路径。
- [ ] 每项并行能力必须有竞争、幂等和故障注入测试。
- [ ] 未完成 `container_sandbox` 前，不开放模型生成的任意 `bash` 给无人值守模式；有人值守 M1 例外必须同时满足 §15 的全部限制。
- [ ] Skills 自进化必须遵守“候选隔离、权限不自动扩大、评测先行、可审计发布、可回滚”。

## 14. 已确认的设计决策

以下选择已经确认。箭头表示分阶段演进顺序，不表示首个里程碑必须一次完成所有阶段。

### Q1：身份与部署范围

- 已选：`A -> B -> C`。
- 落地：先本机单用户，再增加远程单用户鉴权，最后增加多用户与租户隔离。

- 选项 A：首期只支持单用户、本机访问，不实现登录和租户隔离。
- 选项 B：支持远程访问和单用户登录，需要 TLS、鉴权、CSRF/CORS、安全 Cookie 和暴露面加固。
- 选项 C：支持多用户，需要进一步实现用户、租户、工作区、凭据和任务级数据隔离。
- 建议：首期选择 A。架构中保留 `owner_id` 扩展位，但不提前实现完整多租户。

### Q2：工作区来源

- 已选：`A -> B`。
- 落地：先选择安全根目录中的已有项目，后续增加受控 Git URL 克隆，不实现 ZIP 上传。

- 选项 A：只允许从后端配置的安全根目录中选择已有目录。
- 选项 B：在 A 的基础上支持输入 Git URL，由后端克隆为新工作区。
- 选项 C：再支持浏览器上传 ZIP；需要处理大小限制、Zip Slip、恶意文件和存储清理。
- 建议：首期选择 A，第二阶段增加 B，暂缓 C。无论哪种方式都不得接受未经校验的任意宿主机绝对路径。

### Q3：数据持久化

- 已选：`A`。
- 落地：首期使用 SQLite；由于 Q1 最终进入多用户，届时以 PostgreSQL 迁移作为多用户上线门槛，而不是继续强行使用 SQLite。

- 选项 A：SQLite，部署简单，适合本机单实例；高并发写入和多服务共享能力有限。
- 选项 B：PostgreSQL，并发、查询和迁移能力更强，但开发部署与运维成本更高。
- 建议：首期选择 SQLite，并通过 repository abstraction 隔离数据库；真正进入多用户或多 API 实例时迁移 PostgreSQL。大体积 token/event 内容仍应采用追加事件或 artifact 存储，避免把所有原文塞入单行记录。

### Q4：任务队列与 worker 调度

- 已选：`A -> B`。
- 落地：先使用进程内调度与持久状态，进入单机多 worker 阶段时切换 Redis 队列和独立 worker。

- 选项 A：Phase 1 使用 FastAPI 进程内调度，任务状态落 SQLite；实现最快，但 API 进程退出时正在运行的任务需要恢复或标记失败。
- 选项 B：从一开始使用 Redis 队列和独立 worker；故障隔离更好，但需要额外服务和分布式状态设计。
- 选项 C：自建 SQLite 持久队列；依赖少，但 lease、抢占、心跳和竞争控制都要自行实现。
- 建议：Phase 1 选择 A，Phase 6 切换 B。application service 与 Scheduler 接口必须提前抽象，避免切换时重写 API。

### Q5：container sandbox 网络策略

- 已选：`B`。
- 落地：默认禁网，用户批准后按任务、目标和有效期临时开放。

- 选项 A：始终完全禁网，安全性最高，但 Agent 无法安装依赖、查询远程仓库或调用网络工具。
- 选项 B：默认禁网，用户批准后为当前操作临时开放；可结合域名/端口白名单和出口代理。
- 选项 C：默认联网，仅拦截已知危险目标；体验方便，但难以防止源码、凭据和数据外传。
- 建议：选择 B。允许规则应绑定任务、工具调用、有效期和目标范围；云元数据地址、局域网及宿主机管理端口始终拒绝。

### Q6：工具审批粒度

- 已选：`B`。
- 落地：采用风险分级审批，允许任务级同类授权，但不允许绕过 `policy_boundary` 或 `container_sandbox` 硬边界。

- 选项 A：所有写入、命令和联网均逐次审批，最保守但频繁打断任务。
- 选项 B：风险分级。`read` 自动；分配工作区内的 `write/edit` 可按任务授权；安全命令可按规则自动；危险 `bash`、越界路径和网络始终审批或拒绝。
- 选项 C：用户可为任务设置全部自动批准，体验最快但风险最大。
- 建议：选择 B。允许“本任务允许同类操作”，但不提供绕过 `policy_boundary`、`container_sandbox`、宿主机挂载、特权容器或 secret 边界的全局放行。

### Q7：四工具的能力边界

- 已选：`B`。
- 落地：只暴露 `read/write/edit/bash`，其中 `read` 通过结构化 operation 聚合 read/list/search。

- 选项 A：严格照 Pi 暴露 `read/write/edit/bash`，列目录和搜索通过 `bash` 的 `ls/rg` 完成。
- 选项 B：仍只暴露四个工具名，但让 `read` 通过结构化 `operation=read/list/search` 提供只读聚合能力；`bash` 只用于真正的命令执行。
- 选项 C：保留当前 `list_files/read_file/search/run_shell/write_file/patch_file` 等细粒度工具。
- 建议：选择 B。它保持四工具的模型界面，同时让 read-only Agent 不必为了搜索而获得 shell 权限。`delegate` 继续属于编排控制面，不计入四个执行工具。

### Q8：并行 worker 的写入与合并

- 已选：`B/C`。
- 落地：Git 仓库使用 worktree，非 Git 工作区使用目录快照；禁止共享可写目录。

- 选项 A：所有 worker 共享同一工作目录，实现简单但会发生覆盖、脏读和不可复现冲突。
- 选项 B：每个 worker 使用独立 Git worktree，返回 commit/patch，由 Coordinator 检测并合并。
- 选项 C：每个 worker 使用完整目录快照，适用于非 Git 工作区，但磁盘和复制成本更高。
- 建议：Git 仓库选择 B；非 Git 工作区回退 C。禁止 A。Coordinator 合并后必须统一运行测试并进入 Review。

### Q9：Planning 的用户交互

- 已选：`A -> B`。
- 落地：先只展示计划并允许暂停/取消，后续增加可选的执行前批准，不实现直接表格式编辑。

- 选项 A：计划只展示，用户可以暂停或取消，不能直接修改步骤。
- 选项 B：执行前要求用户批准计划；安全但每个复杂任务都会增加等待。
- 选项 C：允许用户编辑、增删和重新排序步骤；控制力最强，但需要计划版本、重新校验和依赖修复。
- 建议：首期选择 A，并提供“要求修改计划”的自然语言入口；后续增加可选 B。暂不实现直接表格式编辑 C。

### Q10：Swarm 并行策略

- 已选：`C`。
- 落地：系统自动判断并行，用户配置最大 Agent 数、并发和预算上限。

- 选项 A：系统根据计划和依赖自动决定是否并行，用户只观察结果。
- 选项 B：完全由用户选择并行开关、Agent 数量和角色。
- 选项 C：系统自动决策，但用户可配置最大 Agent 数、预算并手动降低并发。
- 建议：选择 C。默认最大并发应保守，只有相互独立的步骤才能 fan-out；写入步骤还必须满足文件所有权约束。

### Q11：Right Code 模型与凭据配置

- 已选：`A -> B`。
- 落地：先使用服务端环境变量，后续由管理员配置模型 allowlist 供前端选择；API Key 不下发浏览器。

- 选项 A：首期由服务端 `.env` 固定 base URL、模型名和 API Key，前端不显示 secret。
- 选项 B：服务端保存 API Key，前端只能从管理员配置的模型 allowlist 中选择模型。
- 选项 C：用户可在设置页填写 base URL、模型名和 API Key；需要加密存储、凭据所有权、SSRF 防护和日志脱敏。
- 建议：首期选择 A，但不要把代码写死为 `gpt-5.4`；通过环境变量配置。需要多模型切换时升级为 B，暂缓 C。

### Q12：支持的操作系统

- 已选：`B`。
- 落地：Linux Docker 是生产与安全验收基准，Windows Docker Desktop 支持本地开发和基本冒烟测试。

- 选项 A：开发与生产仅支持 Linux Docker。
- 选项 B：生产以 Linux Docker 为基准，同时支持 Windows + Docker Desktop 作为本地开发环境。
- 选项 C：Windows 和 Linux 都作为正式生产目标，需要分别处理路径、权限、挂载、信号和容器行为差异。
- 建议：选择 B。CI 和安全验收以 Linux 为准，Windows Docker Desktop 提供开发文档和基本冒烟测试，不承诺 Windows 原生容器。

### 已收敛的兼容决策

- [x] CLI 作为 Web 的并列入口长期保留，两者共享 application service。
- [x] `native` backend 当前不弃用；未来是否弃用必须独立评估，不能因 Web 稳定而自动触发。

最终答案：`Q1 A->B->C, Q2 A->B, Q3 A, Q4 A->B, Q5 B, Q6 B, Q7 B, Q8 B/C, Q9 A->B, Q10 C, Q11 A->B, Q12 B`。

## 15. 建议首个可交付里程碑

首个里程碑 M1 只覆盖 Phase 0、Phase 1 和 Phase 2 的核心闭环：

```text
React Web -> FastAPI -> Application Service -> LangGraph backend -> SSE
```

### 15.1 M1 范围

- Web 默认 backend 固定为 `langgraph`，使用现有 route/research/execute/review 事件支撑基础状态流。
- `native` backend 继续用于 CLI、回归和显式兼容测试，不作为 M1 Web 默认路径。
- M1 包含 Workspace/Session/Task/Run 合同、核心 REST、SSE、`per_call_only` 异步审批、可靠取消、SQLite 控制状态、LangGraph 正式打包和基本 Web 工作区。
- M1 的模型客户端仍可使用非流式 `/responses`；此时 SSE 只推送真实 lifecycle 与 `message.completed`，不伪造 token delta。
- M1 不包含 Skills API、标准四工具迁移、Docker `container_sandbox`、完整计划树、Agent 树、多 worker 或 swarm。
- 不为未实现的 Skills 管理 API 提供空壳、假数据或 `501` 占位；Phase 8 实现时再加入 OpenAPI。

### 15.2 M1 宿主机开发模式安全限制

M1 暂时复用现有宿主机工具执行器，只允许有人值守的本地开发，不视为生产 sandbox。以下条件必须同时成立：

1. FastAPI 默认只监听 loopback，不开放公网或不受信任局域网。
2. Web UI 持续显示“工具在宿主机执行，尚无容器隔离”的醒目警告。
3. 每次 risky 写入、shell 或网络操作都必须经 Web UI 单次审批；M1 不提供“本任务全部允许”。
4. 没有可用审批端、审批超时或状态无法恢复时必须 fail closed，工具保持等待或拒绝，不能自动继续。
5. 继续强制现有路径、只读、allowlist、环境变量过滤和输出审计等 `policy_boundary`。
6. SSE 断开不自动取消安全步骤，但新的 risky 操作必须等待审批端恢复；用户仍可通过 REST 显式取消。
7. M1 不允许后台定时任务、无人值守 Agent 或 Skills 自进化触发宿主机 `bash`。

M1 验收后优先完成 Phase 3 标准工具合同与 Phase 4 Docker `container_sandbox`，再开放无人值守、多 worker、swarm 和 Skills 自进化。
