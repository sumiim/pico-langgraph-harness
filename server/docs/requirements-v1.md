# Pico Web V1 需求文档

> 文档状态：V1 实施基线
>
> 目标：先交付一个可在本机使用的单用户 Web Coding Agent。V1 优先复用现有稳定能力，只实现能够形成完整使用闭环的功能；架构升级和高级 Agent 能力留到后续版本。

## 1. 产品目标

V1 为现有 Pico 增加一个 React Web 界面和 FastAPI 服务，使用户可以在浏览器中：

1. 选择一个允许访问的本地项目。
2. 创建或恢复会话。
3. 向 Agent 提交任务。
4. 查看模型回答、工具执行和任务状态。
5. 批准或拒绝危险工具调用。
6. 请求停止正在运行的 Agent。
7. 查看受影响文件和运行审计产物。

V1 是本地开发工具，不作为公网、多用户或强安全隔离产品交付。

## 2. V1 实施原则

- 复用现有 `native` AgentLoop、prompt prefix、context manager、memory、checkpoint、SessionStore、RunStore、EventSink 和安全策略。
- V1 Web 默认使用 `native` backend，继续使用现有 `<tool>...</tool>` 合同；不在本版本迁移原生 provider tool calling。
- FastAPI 只负责 API、任务调度和事件传输，Agent 行为继续由 Pico runtime 控制。
- React 不复制 Agent 状态机，只消费 REST 快照和 SSE 事件。
- V1 只允许一个 active root Task，避免现有可变 runtime 出现并发状态竞争。
- 不伪造模型 token streaming。模型客户端返回完整结果时，只发送 `message.completed`。
- 所有已知限制必须在 UI 和文档中明确展示，不以占位功能伪装成已实现能力。

## 3. 用户与运行环境

### 3.1 用户模型

- 仅支持单用户。
- 默认只监听 `127.0.0.1`。
- V1 不提供登录、用户注册、角色和租户隔离。
- V1 不允许直接暴露到公网。

### 3.2 支持环境

- Python 3.10+。
- Node.js 与 pnpm 的具体版本在工程初始化时锁定。
- 支持 Windows 本地开发。
- 提供 Linux Docker Compose 运行方式。
- Windows Docker Desktop 只要求完成基本开发冒烟验证。

## 4. V1 范围

### 4.1 必须实现

- React + Vite + TypeScript Web 前端。
- Ant Design 组件；TailwindCSS 仅用于布局和少量原子样式。
- FastAPI REST API。
- SSE 任务事件流。
- Right Code GPT 配置与调用。
- 本地工作区选择和路径边界校验。
- 会话创建、列表、详情和恢复。
- 任务创建、状态查询和结果查看。
- 单次危险操作审批。
- Agent 停止请求。
- 工具调用与错误事件展示。
- 运行 artifacts 查询。
- 本地开发启动命令和 Docker Compose。
- 后端、前端和核心端到端测试。

### 4.2 明确不实现

- Electron 或 Tauri 桌面端。
- 登录、远程访问和多用户。
- PostgreSQL、Redis 和多进程 worker 集群。
- 单机多 worker、分布式图执行和 Agent Swarm。
- 独立 Docker `container_sandbox` 和网络白名单。
- OpenAI/Anthropic/Ollama 原生 tool calling。
- 四工具重构和 MCP。
- 独立 Planning 层、计划编辑和计划审批。
- Skills API、Skills 管理和 Skills 自进化。
- Git URL 克隆、ZIP 上传和远程工作区。
- 真正的 token/chunk streaming。
- 运行中追加消息或改变 Agent 指令。
- 多个任务并发执行。

## 5. 逻辑数据模型

### 5.1 Workspace

- Workspace 表示 Agent 被允许访问的项目目录。
- 后端通过 `PICO_ALLOWED_WORKSPACE_ROOTS` 配置一个或多个安全根目录。
- 前端只能选择安全根目录内的已有目录。
- 后端必须解析真实路径并拒绝 `..`、符号链接或 junction 越界。
- 若选择目录位于 Git 仓库内，V1 的有效执行根目录不得自动扩大到安全根目录之外。

### 5.2 Session

- Session 保存连续对话、memory 和 checkpoint。
- 一个 Session 绑定一个 Workspace。
- 一个 Session 可以包含多个按时间串行执行的 Task。
- V1 不允许修改 Session 的 Workspace；切换项目需要创建新 Session。

### 5.3 Task 与 Run

- Task 表示一次用户提交的逻辑请求。
- Run 表示 Task 的一次实际执行。
- V1 每个 Task 只创建一个 Run，但 API 和持久化中保留独立 `task_id` 与 `run_id`。
- V1 不提供 Task 重试 API；用户再次提交时创建新 Task。
- Research/delegate 等内部执行只作为当前 Run 的审计信息展示，不在 V1 建立完整 child Task API。

## 6. 后端需求

### 6.1 Application Service

- 提供创建 Session、提交 Task、查询状态、审批和取消的统一服务接口。
- 每个 Run 创建独立 Pico runtime，不在不同任务间共享 `current_task_state` 等可变运行字段。
- V1 全局最多运行一个 root Task；其他任务请求返回 `409 agent_busy`。
- FastAPI event loop 不得直接运行同步 AgentLoop；AgentLoop 在受控后台执行单元中运行。
- 服务关闭时，活动 Run 标记为 `interrupted`。

### 6.2 REST API

#### 健康检查

- `GET /health/live`
- `GET /health/ready`

#### Workspace

- `GET /api/v1/workspaces`
  - 返回允许选择的 Workspace。
  - 不返回安全根目录之外的路径。

#### Session

- `POST /api/v1/sessions`
  - 输入：`workspace_id`。
  - 输出：新 Session 摘要。
- `GET /api/v1/sessions`
  - 返回按更新时间倒序的 Session 列表。
- `GET /api/v1/sessions/{session_id}`
  - 返回 Session、消息历史和最近 Task 摘要。

#### Task

- `POST /api/v1/tasks`
  - 输入：`session_id`、`message`。
  - 创建 Task 和 Run。
  - 有 active Task 时返回 `409 agent_busy`。
- `GET /api/v1/tasks/{task_id}`
  - 返回 Task、Run、最终回答、受影响文件和错误摘要。
- `POST /api/v1/tasks/{task_id}/cancel`
  - 幂等提交停止请求。
- `POST /api/v1/tasks/{task_id}/approvals/{approval_id}`
  - 输入：`approve` 或 `reject`。
  - 只处理尚未决策且属于该 Task 的审批。
- `GET /api/v1/runs/{run_id}/artifacts`
  - 返回 `task_state.json`、`trace.jsonl`、`report.json` 等可用产物的元数据和读取地址。

### 6.3 错误合同

错误响应统一包含：

```json
{
  "error": {
    "code": "agent_busy",
    "message": "Another task is running.",
    "details": {},
    "request_id": "req_xxx"
  }
}
```

V1 至少定义：

- `invalid_request`
- `workspace_not_found`
- `workspace_forbidden`
- `session_not_found`
- `task_not_found`
- `agent_busy`
- `approval_not_found`
- `approval_already_resolved`
- `task_not_cancellable`
- `provider_error`
- `runtime_error`

## 7. SSE 事件需求

### 7.1 连接

- `GET /api/v1/tasks/{task_id}/events`
- Content-Type 为 `text/event-stream`。
- SSE 连接断开不得自动取消 Task。
- V1 在当前服务进程生命周期内保存有限事件缓冲，支持短暂断线重连。
- 服务重启后不保证 SSE 精确续传，前端必须重新读取 Task 快照。

### 7.2 事件信封

```json
{
  "event_id": "evt_xxx",
  "task_id": "task_xxx",
  "run_id": "run_xxx",
  "sequence": 12,
  "type": "tool.started",
  "timestamp": "2026-08-02T12:00:00Z",
  "data": {}
}
```

### 7.3 V1 事件类型

- `task.started`
- `task.status_changed`
- `model.started`
- `message.completed`
- `tool.requested`
- `tool.started`
- `tool.completed`
- `tool.failed`
- `approval.required`
- `approval.resolved`
- `policy.violation`
- `task.cancel_requested`
- `task.completed`
- `task.failed`
- `task.cancelled`
- `heartbeat`

V1 不发送伪造的 `message.delta`，也不发送 `container_sandbox.*`。

## 8. 审批需求

- V1 使用 `per_call_only`。
- `read_file/list_files/search` 等只读工具不需要审批。
- `write_file/patch_file/run_shell` 每次调用都需要审批。
- 后端创建 pending approval 后暂停当前 Agent 执行并发送 `approval.required`。
- 前端显示工具名、参数摘要和风险说明。
- 用户批准后仅允许执行该次精确调用，不授权后续调用。
- 用户拒绝后将拒绝结果返回 Agent，由 Agent 决定继续回答或结束。
- 审批等待期间可以取消 Task。
- 审批超时后默认拒绝。
- 服务重启会使 pending approval 失效，并将 Run 标记为 `interrupted`。

## 9. 停止需求

- 用户可以在 Task 处于运行或等待审批时点击停止。
- 后端立即记录 `cancel_requested`，前端显示“正在停止”。
- AgentLoop 在模型调用前后、工具调用前后和每轮循环边界检查停止状态。
- `run_shell` 必须从 `subprocess.run` 改为可跟踪的 `Popen`，并支持终止对应进程树。
- 对已经发出的同步模型 HTTP 请求，V1 不承诺立即中断；请求返回或超时后不得继续执行工具。
- 模型请求超时必须可配置，并设置有限默认值。
- Task 最终进入 `cancelled` 或带明确原因的 `failed`，不得长期停留在 `cancelling`。

## 10. 前端需求

### 10.1 页面布局

- 左侧栏：Session 列表和新建 Session。
- 主区域：对话消息、模型回答、工具事件和错误。
- 顶部或状态栏：当前 Workspace、模型、任务状态和开发模式标识。
- 输入区：任务输入框、发送按钮和停止按钮。

### 10.2 任务交互

- 提交任务后立即显示运行状态。
- 通过 SSE 更新事件，不使用轮询模拟实时输出。
- 收到 `message.completed` 后一次性渲染完整模型文本。
- 工具事件使用可折叠区域展示参数摘要、状态、耗时和输出摘要。
- pending approval 使用明确的批准/拒绝按钮。
- 禁止重复提交同一审批决定。
- SSE 断线时显示重连状态；重连失败时读取 Task 快照。
- 停止按钮在终态后禁用。

### 10.3 安全提示

V1 必须持续显示：

> 开发模式：工具直接在后端运行环境中执行，尚未提供独立任务级容器沙盒。请勿加载不可信项目或开放公网访问。

## 11. 模型与配置

### 11.1 Right Code

- 使用现有 OpenAI-compatible `/responses` 客户端。
- 配置来自服务端环境变量：
  - `PICO_OPENAI_API_BASE`
  - `PICO_OPENAI_API_KEY`
  - `PICO_OPENAI_MODEL`
- API Key 不得通过 REST、SSE、日志或前端配置返回。
- V1 不提供前端模型切换和 API Key 编辑。
- Provider 错误应转换为稳定错误码，原始响应在脱敏后进入审计记录。

### 11.2 Agent 配置

V1 后端至少支持：

- 最大 Agent 步数。
- 模型请求超时。
- 最大输出 token。
- Workspace 安全根目录。
- 审批超时。
- SSE 心跳间隔。
- 是否允许 `run_shell`；默认允许但逐次审批。

## 12. 持久化与审计

- V1 继续使用现有 JSON SessionStore 和 RunStore。
- Session 历史和 memory 继续写入 Session JSON。
- Run artifacts 继续包含 `task_state.json`、`trace.jsonl` 和 `report.json`。
- 新增轻量 Task 索引时也使用原子 JSON 写入，不引入 SQLite。
- 日志、SSE 和 artifacts 必须执行现有 secret redaction。
- UI 中的 `policy.violation` 只表示路径、只读、allowlist 或审批等策略违规，不得展示为容器逃逸。
- V1 不承诺服务崩溃后继续原 Run；重启时遗留的 running Task 标记为 `interrupted`。

## 13. Docker 与启动方式

### 13.1 本地开发

- 后端和前端可分别启动。
- Vite 代理 `/api` 和 SSE 到 FastAPI。
- FastAPI 默认监听 `127.0.0.1`。

### 13.2 Docker Compose

- 提供 frontend 和 backend 服务。
- Workspace 通过显式 bind mount 挂载到 backend。
- backend 中执行工具不等于独立 `container_sandbox`；该容器同时运行 API 和 Agent，V1 不将其描述为安全沙盒。
- Docker Compose 默认不映射到公网地址。
- secret 只注入 backend，不进入 frontend 构建产物。

## 14. Python 与前端工程依赖

### 14.1 Python

主项目增加 `web` 可选依赖组，至少包含：

- `fastapi`
- `uvicorn`
- `sse-starlette`
- 当前 V1 不引入 SQLAlchemy、aiosqlite、Redis 或 PostgreSQL driver。

### 14.2 前端

- `react`
- `react-dom`
- `vite`
- `typescript`
- `antd`
- `tailwindcss`
- 路由、Markdown 渲染、测试库由实现阶段选择并锁定版本。

## 15. 验收标准

### 15.1 核心闭环

1. 用户可以选择允许的 Workspace 并创建 Session。
2. 用户可以提交任务，后端创建 Task/Run 并调用 Right Code。
3. 前端通过 SSE 看到任务、模型和工具状态。
4. 只读任务可以完成并展示最终答案。
5. 写文件或 Shell 调用会暂停并请求单次审批。
6. 批准后工具执行，拒绝后工具不执行。
7. 用户可以请求停止，Agent 不再开始新的模型或工具步骤。
8. 页面刷新后可以通过 REST 恢复 Session、Task 最终状态和 artifacts。

### 15.2 安全验收

- Workspace 越界路径被拒绝。
- 符号链接或 junction 逃逸被拒绝。
- 未批准的危险工具不执行。
- API Key 不出现在 REST、SSE、前端状态或 artifacts 中。
- 服务不配置额外参数时只监听 loopback。
- UI 始终展示宿主机/后端环境执行警告。

### 15.3 测试验收

- 现有 Python 回归测试通过。
- 新增 REST contract tests。
- 新增 SSE 顺序和断线恢复测试。
- 新增审批批准、拒绝、超时和取消测试。
- 新增 Shell 进程终止测试。
- 新增 Workspace API 路径安全测试。
- 新增 React 组件测试。
- 新增至少一条浏览器端到端流程：创建 Session -> 提交 Task -> 审批 -> 完成。

## 16. 建议实施顺序

1. 冻结 V1 API、事件与状态枚举。
2. 实现 Workspace、Session 和 Task application service。
3. 实现后台 AgentRunner、事件桥接、审批和停止。
4. 实现 FastAPI REST 与 SSE。
5. 初始化 React 工程并完成只读任务闭环。
6. 完成审批、停止和工具事件 UI。
7. 增加 Docker Compose。
8. 完成回归、端到端测试和使用文档。

## 17. 后续版本候选

以下内容只登记方向，不作为 V1 验收条件：

- V1.1：真实 provider token streaming、会话搜索、统一 diff。
- V1.2：SQLite 控制状态、可靠事件重放、Task 重试和 Run 恢复。
- V2：原生 tool calling、标准 JSON Schema 和四工具 Registry。
- V2：正式 LangGraph Web backend、结构化 Planning 和 Agent 视图。
- V3：独立 Docker sandbox、受控网络和并行 Workspace。
- V3：单机多 worker、Agent Swarm 和结果合并。
- V4：Skills API、Skill 版本管理和受控自进化。
- 远期：远程单用户、多用户、PostgreSQL、Redis 和桌面端。
