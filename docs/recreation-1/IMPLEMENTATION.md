# pico 重构执行文档 v2

> 对应需求：[REQUIREMENTS.md](REQUIREMENTS.md)
> v1 已作废，本文档是修订后的可执行版本。

---

## 执行顺序

```
阶段 0：修复 ToolExecutor 循环导入  ← 已完成
阶段 1：TaskState 补字段
阶段 2：EventSink 抽象
阶段 3：LangGraph role delegate 基础  ← wrapper 三角色核心
阶段 4：sandbox 指标接入        ← 接现有约束，不重建基础能力
阶段 5：CLI 子命令重构 + pico eval + BackendRunner
阶段 6：Harness 补任务
阶段 7：LangGraph 可选 wrapper  ← 完全独立，不阻塞前面
```

---

## 阶段 0：修复 ToolExecutor 循环导入（已完成）

**文件**：`pico/tool_executor.py`

当前代码已经通过 postponed annotations 和 `TYPE_CHECKING` 消除了运行时循环导入：

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import Pico
```

把构造函数注解改为字符串或依赖 postponed annotations：

```python
def __init__(self, agent: Pico):
    self.agent = agent
```

阶段验收已完成：`python -c "import pico"` 成功。后续阶段不再把循环导入视为待解决的冷启动阻塞。

---

## 阶段 1：TaskState 补字段

**文件**：`pico/task_state.py`

**新增字段**（dataclass 里）

```python
from dataclasses import field

sandbox_violations: int = 0
malformed_output_recovered: int = 0
affected_paths: list[str] = field(default_factory=list)
```

**新增 LangGraph 停机原因**

```python
STOP_REASON_REVIEW_RETRY_LIMIT_REACHED = "review_retry_limit_reached"
STOP_REASON_NO_CHANGES_TO_REVIEW = "no_changes_to_review"
STOP_REASON_BUDGET_EXHAUSTED = "budget_exhausted"
STOP_REASON_RUNTIME_ERROR = "runtime_error"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
```

**新增方法**

```python
def record_sandbox_violation(self):
    self.sandbox_violations += 1
    return self

def record_malformed_output_recovered(self):
    self.malformed_output_recovered += 1
    return self

def record_affected_paths(self, paths):
    self.affected_paths = sorted(set(self.affected_paths) | {str(path) for path in (paths or [])})
    return self
```

**`from_dict` 补读**

```python
sandbox_violations=int(data.get("sandbox_violations", 0)),
malformed_output_recovered=int(data.get("malformed_output_recovered", 0)),
affected_paths=[str(path) for path in data.get("affected_paths", [])],
```

**`to_dict` 补写**

```python
"sandbox_violations": self.sandbox_violations,
"malformed_output_recovered": self.malformed_output_recovered,
"affected_paths": list(self.affected_paths),
```

---

## 阶段 2：EventSink 抽象

### 新建 `pico/event_sink.py`

```python
from copy import deepcopy

class EventSink:
    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        raise NotImplementedError

class JsonlSink(EventSink):
    def __init__(self, run_store):
        self.run_store = run_store

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        self.run_store.append_trace(task_state, payload)
        return payload

class NullSink(EventSink):
    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        return payload

class EventCollector(EventSink):
    def __init__(self):
        self._events = []

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        self._events.append(deepcopy(payload))
        return payload

    def snapshot(self):
        return tuple(deepcopy(event) for event in self._events)

class CompositeSink(EventSink):
    def __init__(self, collector, *sinks):
        self.collector = collector
        self.sinks = tuple(sinks)

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        self.collector.emit(task_state, event_type, payload)
        for sink in self.sinks:
            try:
                sink.emit(task_state, event_type, payload)
            except Exception as exc:
                # 旁路 sink 失败不能改变 Agent 控制流；不记录异常消息，避免泄露。
                self.collector.emit(task_state, "event_sink_failed", {
                    "event": "event_sink_failed",
                    "created_at": payload.get("created_at"),
                    "source_event": event_type,
                    "sink": type(sink).__name__,
                    "error_type": type(exc).__name__,
                })
        return payload
```

### 修改 `pico/runtime.py`

**import 加**

```python
from .event_sink import EventSink, JsonlSink
```

**`Pico.__init__` 参数末尾加**

```
event_sink=None,
```

**`self.run_store = ...` 之后加**（注意顺序：run_store 先赋值）

```python
self.event_sink: EventSink = event_sink if event_sink is not None else JsonlSink(self.run_store)
```

**`emit_trace` 方法改为**

```python
def emit_trace(self, task_state, event, payload=None):
    payload = self.redact_artifact(payload or {})
    payload["event"] = event
    payload["created_at"] = now()
    self.event_sink.emit(task_state, event, payload)
    return payload
```

EventSink 只负责旁路观测，业务控制流不得读取 sink 的文件或导出结果。`CompositeSink` 先写内存 collector，再 best-effort 写 configured sink；旁路失败只追加不含异常消息的 `event_sink_failed` 内存事件，不能中断 Agent。`affected_paths` 等执行事实写入 `TaskState`；evaluator 只消费 collector 快照，不能读取 `trace.jsonl` 计算指标。

---

## 阶段 3：LangGraph role delegate 基础

这是 LangGraph 三角色编排的核心。native 的公开 `DELEGATE_TOOL_SPEC`、`AgentLoop` 控制流和旧 `spawn_delegate(task, max_steps)` 合同保持不变；新增能力集中在主包的非公开 child factory 和 `examples/langgraph-pico/` wrapper。

### 角色设计回顾

| 角色 | 实现方式 | 可写工具 | prompt 契约 |
|---|---|---|---|
| Coordinator | LangGraph `plan/execute/finalize` 节点 + Pico runtime | 全部 | 图 state 和节点路由 |
| Research | wrapper 内部 `create_role_delegate(RoleDelegateSpec(...))` | `list_files, read_file, search` | 固定输出格式 |
| Review | wrapper 内部 `create_role_delegate(RoleDelegateSpec(...))` | `read_file, search` | 必须首行输出 `status:` |

### 保持 `pico/tools.py` 的 native delegate schema 不变

```python
DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}
```

`TOOL_EXAMPLES["delegate"]` 也保持旧示例；Research/Review 的 role spec 只存在于 wrapper 内部，不进入 native 初始 prompt、工具签名或模型可见 schema。

```python
"delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
```

### 新增 wrapper 可复用的 role child factory

native `pico/tools.py` 的 `DELEGATE_TOOL_SPEC`、`validate_tool()`、`AgentLoop` 控制流和公开
`spawn_delegate(task, max_steps)` 合同保持不变。三角色所需的 `RoleDelegateSpec` 和 role child
factory 是供 LangGraph wrapper 调用的内部能力，不能替换或扩展 native 模型可见的 delegate schema。

主包可以增加 child state 收集、失败收口、`InMemorySessionStore` 和 EventSink 注入等向后兼容基础设施；
这些能力默认不改变 native delegate 的行为。

先在 `Pico.__init__` 中初始化未合并的子运行状态，供 backend/evaluator 统一聚合：

```python
self.child_task_states = []
```

同时在 `pico/session_store.py` 新增可复用的 `InMemorySessionStore`，实现与 `SessionStore` 相同的 `save/load/latest` 接口并对数据做深拷贝。它属于主包基础设施，不能放在 LangGraph example 中，因为 wrapper 的 research/review role child 和 executor 都需要它。`save()` 返回稳定的内存路径标记，满足 `Pico.session_path` 的现有合同。

`Pico.__init__` 增加默认开启的 `allow_checkpoint=True`、`allow_durable_memory_write=True`。`AgentLoop` 在每个 `create_checkpoint()` / `promote_durable_memory()` 调用点前检查对应开关，关闭 checkpoint 时不得发出虚假的 `checkpoint_created` 事件。普通 Pico 和旧 delegate 不传参数，行为保持不变。

child 在模型异常处提前退出时使用主包公共 finalizer，不能从 runtime 反向 import evaluator。该 helper 所在模块需要显式导入 `STATUS_RUNNING`、`STATUS_FAILED`、`STOP_REASON_MODEL_ERROR`、`STOP_REASON_RUNTIME_ERROR` 和 `STOP_REASON_PERSISTENCE_ERROR`：

```python
def _finalize_failed_run(agent, task_state, *, error_type, duration_ms, stop_reason=STOP_REASON_RUNTIME_ERROR):
    """把尚未收口的 child run 标为 failed，并尽力补齐生命周期 artifact。"""
    if stop_reason not in {
        STOP_REASON_MODEL_ERROR,
        STOP_REASON_RUNTIME_ERROR,
        STOP_REASON_PERSISTENCE_ERROR,
    }:
        stop_reason = STOP_REASON_RUNTIME_ERROR
    if task_state.status == STATUS_RUNNING:
        task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=f"agent run failed: {error_type}")
    persistence_error = False
    try:
        agent.run_store.write_task_state(task_state)
    except Exception:
        persistence_error = True
    try:
        agent.emit_trace(task_state, "run_finished", {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "error_type": str(error_type),
            "run_duration_ms": int(duration_ms),
        })
    except Exception:
        persistence_error = True
    if persistence_error:
        task_state.stop(
            STOP_REASON_PERSISTENCE_ERROR,
            status=STATUS_FAILED,
            final_answer="agent run finalization failed: persistence error",
        )
    try:
        report = agent.redact_artifact(agent.build_report(task_state))
        report["run_duration_ms"] = int(duration_ms)
        agent.run_store.write_report(task_state, report)
    except Exception:
        task_state.stop(
            STOP_REASON_PERSISTENCE_ERROR,
            status=STATUS_FAILED,
            final_answer="agent run finalization failed: persistence error",
        )
        try:
            agent.run_store.write_task_state(task_state)
        except Exception:
            pass
```

实际实现中应把 `delegate_started_at` 计算出的耗时传入 `duration_ms`；`error_type` 只允许异常类型或稳定 stop reason，不允许原始异常消息。公共 finalizer 需要在 `runtime.py` 或独立的主包 runtime helper 中定义，native delegate 和 LangGraph executor 都复用它。

```python
from dataclasses import dataclass
import time

@dataclass(frozen=True)
class RoleDelegateSpec:
    role: str
    task: str
    allowed_tools: tuple[str, ...]
    focus_paths: tuple[str, ...] = ()
    acceptance: str = ""
    context_summary: str = ""
    max_steps: int = 3

_RESEARCH_ALLOWED = ("list_files", "read_file", "search")
_REVIEW_ALLOWED   = ("read_file", "search")

_RESEARCH_PREFIX = (
    "你是只读调查 agent。不得修改任何文件。\n"
    "调查完毕后，输出格式如下（不得省略）：\n"
    "Findings: <发现>\n"
    "Candidate files: <候选文件列表>\n"
    "Suggested action: <建议下一步>\n"
)

_REVIEW_PREFIX_TMPL = (
    "你是只读审查 agent。不得修改任何文件。\n"
    "只检查以下文件：{focus_paths}\n"
    "通过标准：{acceptance}\n"
    "执行上下文：{context_summary}\n"
    "输出格式如下（第一行必须是 status 行）：\n"
    "status: pass\n"
    "  或\n"
    "status: needs_fix\n"
    "issues: <问题列表>\n"
    "verify_targets: <建议复查路径>\n"
)

def _normalize_review_result(text):
    """wrapper 使用的幂等 review 结果规范化函数。"""
    raw = str(text or "").lstrip()
    lines = raw.splitlines()
    first_line = lines[0].strip().lower() if lines else ""
    issue_codes = [
        "malformed_review_status"
        for line in lines
        if line.strip().lower() in {
            "issue: malformed_review_status",
            "issues: malformed_review_status",
        }
    ]
    if first_line == "status: pass":
        return {"status": "pass", "text": raw, "issue_codes": issue_codes, "recovered": False}
    if first_line == "status: needs_fix":
        return {"status": "needs_fix", "text": raw, "issue_codes": issue_codes, "recovered": False}

    normalized = "status: needs_fix\nissue: malformed_review_status"
    if raw:
        normalized += "\n" + raw
    return {
        "status": "needs_fix",
        "text": normalized,
        "issue_codes": ["malformed_review_status"],
        "recovered": True,
    }
```

wrapper 在构造 `RoleDelegateSpec` 时校验 role、路径、验收标准、工具 allowlist 和 `[1, 12]` 步数上限；
native `validate_tool()` 只继续校验旧的 `task/max_steps` 参数。

role child factory 的核心伪代码如下：

```python
def create_role_delegate(parent, spec: RoleDelegateSpec):
    validate_role_delegate_spec(spec)
    isolated = spec.role in {"research", "review"}
    if spec.role == "research":
        prompt = _RESEARCH_PREFIX + spec.task
    elif spec.role == "review":
        prompt = _REVIEW_PREFIX_TMPL.format(
            focus_paths=", ".join(spec.focus_paths),
            acceptance=spec.acceptance,
            context_summary=spec.context_summary,
        ) + spec.task
    else:
        raise ValueError("unknown wrapper role")

    child = None
    delegate_error = None
    started_at = time.monotonic()
    emit_role_started(parent, spec)
    try:
        child = Pico(
            model_client=parent.model_client,
            workspace=parent.workspace,
            session_store=InMemorySessionStore(),
            run_store=parent.run_store,
            approval_policy="never",
            max_steps=spec.max_steps,
            read_only=True,
            allowed_tools=spec.allowed_tools,
            event_sink=parent.event_sink,
            allow_checkpoint=False,
            allow_durable_memory_write=False,
        )
        child.agent_role = spec.role
        child.session["memory"]["task"] = spec.task
        child.session["memory"]["notes"] = [clip(parent.history_text(), 300)]
        result = child.ask(prompt)
    except Exception as exc:
        delegate_error = exc
        result = ""
    finally:
        child_state = child.current_task_state if child is not None else None
        collect_child_state(parent, child, child_state)
        if delegate_error is not None:
            finalize_child_failure_if_needed(
                child, child_state, type(delegate_error).__name__,
                int((time.monotonic() - started_at) * 1000),
            )
            emit_delegate_failed(parent, spec, child_state, delegate_error)
    if delegate_error is not None:
        raise delegate_error
    emit_role_finished(parent, spec, child, result, int((time.monotonic() - started_at) * 1000))
    return child, result
```

`collect_child_state()`、`finalize_child_failure_if_needed()` 和 `emit_delegate_failed()` 是主包的通用
生命周期辅助函数；它们不改变旧 `spawn_delegate()` 的参数或默认权限。LangGraph 节点负责把
`RoleDelegateSpec` 映射为图状态，review 节点再调用 wrapper 的 `_normalize_review_result()`。
`emit_role_started/finished()` 按 `spec.role` 生成 `delegate_started/delegate_finished` 或
`review_requested/review_passed|review_failed`，因此 evaluator 不需要从 JSONL 反推角色结果。

旧 native `spawn_delegate()` 仍保持以下行为：

- 只接受旧的 `task/max_steps` 工具参数；不识别 wrapper 的 `role/mode/focus_paths` 字段。
- 继续使用原有 session store、checkpoint、memory 和默认 child 权限语义。
- native 不自动触发 Research -> Execute -> Review 路由，也不增加 `review_fix_pending` retry 状态。

如果实现者需要复用 native 的 child 构造代码，应抽取私有 helper 并让旧 `spawn_delegate()` 继续走
原路径；不能把下面的 role spec 反向塞回 `DELEGATE_TOOL_SPEC`。

```python
# LangGraph research/review node only:
child, raw_result = create_role_delegate(parent, spec)
```

review 输出由 wrapper 的 `_normalize_review_result(text)` 处理：严格首行分别映射为 `pass` / `needs_fix`；
其他首行统一改写为 `status: needs_fix`，并在 issues 中加入 `malformed_review_status`。规范化函数必须幂等；
LangGraph 的路由使用规范化结果，`malformed_output_recovered` 只计一次。

### 不修改 native `pico/agent_loop.py` 的 review retry

native `AgentLoop` 不新增 `review_fix_pending`、`review_retry_started` 或自动 review 路由。LangGraph
`execute` / `review` 节点之间的 retry 由图条件边和 graph-level state 控制；只有图实际再次进入 execute
时才发出 `review_retry_started`。普通 native `Pico.ask()` 仍按原有 tool loop 运行。

---

## Wrapper 事件口径

以下事件由共享的 EventSink 基础设施记录；`review_*` 和 `review_retry_started` 只由 LangGraph
wrapper 的图节点发出，native baseline 不自动生成这些事件。

**新增 trace event 说明**

| event | 触发时机 |
|---|---|
| `delegate_started` | legacy native delegate 或 wrapper role child 启动前 |
| `delegate_finished` | legacy native delegate 或 wrapper role child 返回后 |
| `delegate_failed` | 子 agent 异常退出；payload 只记录异常类型，不记录异常消息 |
| `review_requested` | LangGraph review node 启动前 |
| `review_passed` | LangGraph review node 首个非空行严格等于 `status: pass` |
| `review_failed` | LangGraph review node 规范化后的状态为 `needs_fix` |
| `review_retry_started` | LangGraph 图在 review 失败后实际再次进入 execute |
| `event_sink_failed` | configured sink 写入失败；仅由 CompositeSink 追加到内存 collector |

## 阶段 4：sandbox 指标接入

**不重建任何基础能力。** `tool_run_shell` 已有 timeout，`shell_env()` 已过滤 allowlist，`security.py` 已做脱敏。这里只把现有拦截事件接入 task_state 计数 **和 EventSink**。

### 修改 `pico/tool_executor.py`

在 `execute()` 里找到以下4处拦截/失败路径，各补计数+trace 两行（抽一个辅助函数减少重复）：

**建议在 ToolExecutor 里加私有辅助方法**：

```python
def _record_sandbox_violation(self, tool_error_code, security_event_type, tool_name):
    agent = self.agent
    if agent.current_task_state is None:
        return
    agent.current_task_state.record_sandbox_violation()
    # 用 agent_role 属性区分 research / review / coordinator，而不是靠 read_only——
    # research delegate 和 review delegate 都是 read_only，单靠 read_only 无法区分。
    agent.emit_trace(agent.current_task_state, "sandbox_violation", {
        "tool": tool_name,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "agent_role": getattr(agent, "agent_role", "coordinator"),
    })
```

**配套修改 wrapper role child factory**（在 child Pico 构造之后，`child.session["memory"]` 赋值之前）：

```python
child.agent_role = spec.role
```

这样 research/review 的 violation 会被正确标为对应 role；native 旧 delegate 继续使用原有的
`delegate` 标识，主 agent 无此属性时回退到 `coordinator`。

**1. 工具不在白名单**（`tool_error_code="tool_not_allowed"`）之前：

```python
self._record_sandbox_violation("tool_not_allowed", "tool_not_allowed", name)
```

**2. path_escape 参数校验失败**（`security_event_type = "path_escape"` 的 validate except 块）：

```python
if security_event_type == "path_escape":
    self._record_sandbox_violation("invalid_arguments", "path_escape", name)
```

**3. 工具执行时抛出 path_escape 异常**（except 块里）：

```python
if security_event_type == "path_escape":
    self._record_sandbox_violation("tool_failed", "path_escape", name)
```

**4. read_only_block（只读 delegate 尝试写入）**

```python
if agent.read_only:
    self._record_sandbox_violation("approval_denied", "read_only_block", name)
```

对应代码位置（`tool_executor.py` 约第 102 行，完整块）：

```python
if tool["risky"] and not agent.approve(name, args):
    if agent.read_only:
        self._record_sandbox_violation("approval_denied", "read_only_block", name)
    return ToolExecutionResult(
        content=f"error: approval denied for {name}",
        metadata=_metadata(
            "rejected",
            tool_error_code="approval_denied",
            security_event_type="read_only_block" if agent.read_only else "approval_denied",
            risk_level="high",
            read_only=False,
        ),
    )
```

### 修改 `pico/agent_loop.py`

在工具执行完成并取得 `tool_result.metadata` 后，把执行事实写入 TaskState；EventSink 仍只负责旁路记录：

```python
task_state.record_affected_paths(tool_result.metadata.get("affected_paths", []))
agent.run_store.write_task_state(task_state)
```

在 `kind == "retry"` 分支里补计数（一行）：

```python
if kind == "retry":
    task_state.record_malformed_output_recovered()  # ← 补这一行
    agent.record({"role": "assistant", "content": payload, "created_at": now()})
    ...
```

---

## 阶段 5：CLI 子命令重构 + BackendRunner + pico eval

### 5a：argparse 重构

`cli.py` 当前是扁平的 positional + flags，需要改成 subcommand 结构。

**重构方式**：先把现有参数注册提取为 `_add_run_arguments(parser)`。保留公开的 `build_arg_parser()`，让直接调用 `build_arg_parser().parse_args(["--cwd", ...])` 的现有代码和测试继续得到 run 参数；另新增 `build_cli_parser()` 负责 `run/eval` 根 subcommand，`main()` 只使用后者。

```
pico run   [现有 REPL/one-shot 行为，参数不变]
pico eval  [新增]
```

为保持向后兼容，`pico <prompt>` 不带子命令时默认走 `run` 行为。argparse subparser 不会自动 fallback，需要在解析前规范化 argv：

```python
def build_arg_parser():
    """保留原有公开契约：直接解析 run 参数，不要求显式写 run。"""
    parser = argparse.ArgumentParser(...)
    _add_run_arguments(parser)
    return parser

def build_cli_parser():
    parser = argparse.ArgumentParser(...)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    _add_run_arguments(run_parser)
    _add_eval_subcommand(subparsers)
    return parser

def parse_cli_args(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    known_subcommands = {"run", "eval"}
    if not argv:
        argv = ["run"]
    elif argv[0] not in known_subcommands:
        # 包括 -h/--help：保持旧行为，显示完整 run 参数帮助。
        argv = ["run"] + argv
    return build_cli_parser().parse_args(argv)

def main(argv=None):
    args = parse_cli_args(argv)
    # 必须在 build_agent(args) 之前分发；eval namespace 没有 cwd/provider 等 run 字段。
    if args.command == "eval":
        return _run_eval(args)
    return _run_native(args)  # 包含现有 build_agent + one-shot/REPL 逻辑
```

> 空参数显式走 `run`（REPL），`pico --help` 等价于 `pico run --help`。`run` 和 `eval` 现在是首词保留字；若任务文本本身以这两个词开头，使用 `pico run <prompt>`。需要新增测试覆盖 `build_arg_parser().parse_args(["--cwd", ...])`、旧 prompt/flags、`--help` 中仍出现 `--provider`、显式 `run` 和 `eval`。

### 5b：BackendRunner 适配层

**新建 `pico/evaluation/backends.py`**

```python
from copy import deepcopy
from typing import Protocol

from ..event_sink import CompositeSink, EventCollector, JsonlSink
from ..features import memory as memorylib
from ..task_state import STATUS_FAILED, STOP_REASON_MODEL_ERROR, STOP_REASON_RUNTIME_ERROR

class BackendRunner(Protocol):
    def run_task(self, task: dict, workspace, session_store, run_store, fixture_copy_root, model_client=None):
        """跑一个 benchmark task，返回 BackendRunResult。"""
        ...

def default_event_sink_factory(run_store):
    return JsonlSink(run_store)

class ModelBoundaryError(RuntimeError):
    def __init__(self, message, stop_reason):
        super().__init__(message)
        self.stop_reason = stop_reason

class HarnessModelClientAdapter:
    """只改变 harness 内的错误分类，不修改主包 provider client。"""
    def __init__(self, delegate):
        self.delegate = delegate

    def complete(self, *args, **kwargs):
        try:
            return self.delegate.complete(*args, **kwargs)
        except ModelBoundaryError:
            raise
        except Exception as exc:
            # 不把 provider 原始异常消息带入 benchmark artifact；只保留稳定异常类型。
            raise ModelBoundaryError(
                f"model call failed: {type(exc).__name__}",
                stop_reason=STOP_REASON_MODEL_ERROR,
            ) from exc

    def __getattr__(self, name):
        return getattr(self.delegate, name)

class BackendRunResult:
    def __init__(
        self,
        task_state,
        final_answer,
        agent,
        child_task_states=None,
        budget_task_states=None,
        initial_state=None,
        events=None,
    ):
        self.task_state = task_state
        self.final_answer = final_answer
        self.agent = agent
        # 两种 backend 都返回未合并的 child states；只允许 evaluator 聚合一次。
        self.child_task_states = list(child_task_states or [])
        # 预算状态只包含 Coordinator 自己的工作，不包含 research/review child 内部步骤。
        self.budget_task_states = list([task_state] if budget_task_states is None else budget_task_states)
        self.initial_state = dict(initial_state or {})
        # 只读事件快照供 evaluator 统计；不能由 evaluator 反向读取 trace.jsonl。
        self.events = tuple(deepcopy(event) for event in (events or ()))

# evaluator 聚合逻辑说明（BenchmarkEvaluator.run_task 在消费 BackendRunResult 时）：
#
# | 指标                      | 聚合方式                                               |
# |---------------------------|-------------------------------------------------------|
# | tool_steps                | task_state + sum(child.tool_steps)                    |
# | attempts                  | task_state + sum(child.attempts)                      |
# | sandbox_violations        | task_state + sum(child.sandbox_violations)             |
# | malformed_output_recovered| task_state + sum(child.malformed_output_recovered)    |
# | stop_reason               | task_state.stop_reason（graph-level，不聚合 child）   |
# | within_budget             | sum(budget_task_states.tool_steps) <= step_budget       |
# | passed                    | 由 verifier、artifact、coordinator 预算和 stop_reason 判定 |
#
# native 的 budget_task_states=[task_state]；LangGraph 为 graph-level task_state
# 加全部 execute executor states。row.tool_steps 仍写包含所有 child 的聚合总数。

class NativeBackendRunner:
    """直接用 Pico + AgentLoop 跑，与现有 BenchmarkEvaluator.run_task 逻辑一致。

    model_client 由 evaluator 层创建后注入，backend 不自己决定用 Fake 还是真实模型。
    """
    def __init__(self, max_new_tokens=64, model_client_factory=None, event_sink_factory=None):
        self.max_new_tokens = max_new_tokens
        # model_client_factory(task, workspace) -> model_client
        # 不传时由调用方在 run_task 里直接传 model_client
        self.model_client_factory = model_client_factory
        self.event_sink_factory = event_sink_factory or default_event_sink_factory

    def run_task(self, task, workspace, session_store, run_store, fixture_copy_root, model_client=None):
        # model_client 由外部传入（evaluator 层决定 Fake vs 真实）
        # 如果提供了 factory，用 factory 创建；否则要求调用方直接传 model_client
        from ..runtime import Pico
        from .evaluator import _apply_task_setup
        if model_client is None and self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        if model_client is None:
            raise ValueError("NativeBackendRunner requires model_client or model_client_factory")
        model_client = HarnessModelClientAdapter(model_client)
        event_collector = EventCollector()
        configured_sink = self.event_sink_factory(run_store)
        event_sink = CompositeSink(event_collector, configured_sink)
        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
            allowed_tools=task["allowed_tools"],
            event_sink=event_sink,
        )
        _apply_task_setup(agent, task, fixture_copy_root)

        initial_memory_state = agent.memory.to_dict()
        initial_state = {
            "initial_history_empty": len(agent.session["history"]) == 0,
            "initial_memory_empty": memorylib.is_effectively_empty(initial_memory_state),
            "initial_task_summary_empty": not str(initial_memory_state["working"]["task_summary"]).strip(),
            "initial_episodic_notes_empty": not initial_memory_state["episodic_notes"],
        }
        final_answer = ""
        task_state = None
        try:
            final_answer = agent.ask(task["prompt"])
        except Exception as exc:
            # 只在 harness runner 内收口；普通 pico.ask() 的异常行为不变。
            task_state = agent.current_task_state or _start_failed_task_state(agent, task)
            agent.current_task_state = task_state
            stop_reason = getattr(exc, "stop_reason", STOP_REASON_RUNTIME_ERROR)
            error_text = f"harness execution failed: {type(exc).__name__}"
            task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=error_text)
            _best_effort_finalize(agent, task_state, error_text)
            final_answer = error_text
        return BackendRunResult(
            task_state or agent.current_task_state,
            final_answer,
            agent,
            child_task_states=agent.child_task_states,
            budget_task_states=[task_state or agent.current_task_state],
            initial_state=initial_state,
            events=event_collector.snapshot(),
        )
```

`_start_failed_task_state()` 仅处理异常发生在 AgentLoop 创建状态之前的情况；`_best_effort_finalize()` 与 LangGraph finalizer 共用一套实现，逐项尝试 `write_task_state → run_finished → write_report`，不得因前一项失败跳过后一项。若持久化本身失败，内存状态改为 `persistence_error`，evaluator 仍生成当前 task 的 failed row。runner 外的 workspace/setup/agent 构造错误由 evaluator 的 task boundary 记录为 `harness_error`。

evaluator 不再直接读取 `task_state.tool_steps` 判预算：

```python
coordinator_tool_steps = sum(state.tool_steps for state in backend_result.budget_task_states)
within_budget = coordinator_tool_steps <= int(task["step_budget"])
```

> `BenchmarkEvaluator.run_task` 重构为调用 `NativeBackendRunner`，再由 evaluator 统一组装 row、执行 verifier、写 artifact。不要让 backend 直接拼最终 JSON row，否则 native/langgraph 结果口径容易漂移。

`backends.py` 还需提供单一选择入口。`BenchmarkEvaluator` 构造函数新增 `backend="native"` 和 `event_sink_factory=None`（也可直接注入 runner）；`run_fixed_benchmark()` 原样向下传递，测试据此注入 NullSink：

```python
def build_backend_runner(name, **kwargs):
    if name == "native":
        return NativeBackendRunner(**kwargs)
    if name == "langgraph":
        # 必须留在分支内部；native import/运行不能依赖 example 或 langgraph。
        try:
            from langgraph_pico.backend import LangGraphBackendRunner
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "langgraph backend is optional; install examples/langgraph-pico first"
            ) from exc
        return LangGraphBackendRunner(**kwargs)
    raise ValueError(f"unknown backend: {name}")
```

native 和 LangGraph runner 都必须把同一 graph-level `EventCollector.snapshot()` 放入 `BackendRunResult.events`。evaluator 只按以下公式计算事件指标，不能各 backend 自行解释：

```text
delegate_calls = count(delegate_started) + count(review_requested)
delegate_failures = count(delegate_failed)
research_calls = count(delegate_started where agent_role == "research")
review_calls = count(review_requested)
review_passed = 最后一个 review_passed/review_failed；无 review 时为 null
review_retries = count(review_retry_started)
```

child task state 只参与数值计数聚合，不再用跨 run 时间线或 `trace.jsonl` 猜测 retry。`duration_ms` 在调用 `runner.run_task()` 的外层用 `time.monotonic()` 计量。

summary 聚合必须显式排除 skipped row：

```python
skipped_rows = [row for row in rows if row.get("status") == "skipped"]
eligible_rows = [row for row in rows if row.get("status") != "skipped"]
passed = sum(1 for row in eligible_rows if row.get("passed"))
failed = len(eligible_rows) - passed
summary = {
    "total_tasks": len(rows),
    "eligible_tasks": len(eligible_rows),
    "skipped_tasks": len(skipped_rows),
    "passed": passed,
    "failed": failed,
    "pass_rate": passed / len(eligible_rows) if eligible_rows else 0.0,
}
```

`within_budget_rate` 和 `verifier_pass_rate` 也只能以 `eligible_rows` 为分母；skipped row 的这些字段应为 `null`，不能写成 `false`。

evaluation result artifact 与 benchmark 输入使用独立版本常量，避免新增字段仍被旧消费者按 v1 解释：

```python
BENCHMARK_SCHEMA_VERSION = 1
EVALUATION_ARTIFACT_SCHEMA_VERSION = 2

artifact = {
    "schema_version": EVALUATION_ARTIFACT_SCHEMA_VERSION,
    "captured_at": _now_in_timezone(...),
    "runtime": existing_runtime_metadata,
    "benchmark": {
        "schema_version": benchmark["schema_version"],
        "source": ...,
        "task_count": len(benchmark["tasks"]),
    },
    "backend": backend_name,
    "reproducibility": existing_reproducibility_metadata,
    "summary": summary,
    "failure_category_counts": summary["failure_category_counts"],
    "rows": rows,
}
```

writer 从本阶段开始默认只写 v2，并保留 v1 的 provenance 和所有既有 row 字段；result reader 按顶层 `schema_version` 分派，v1 先规范化成 v2 内部结构再交给 metrics/report 消费，未知版本直接拒绝。实施时同步更新 `tests/test_evaluator.py` 的新写入断言，另保留一个 v1 fixture 验证兼容读取；并审计 `pico/evaluation/metrics.py` 及其他读取历史 artifact 的脚本。默认输出使用带时间戳的 `*-eval.json`，不再用文件名承载 schema 版本。

`BenchmarkEvaluator.run()` 不能继续使用会在首个异常处中断的列表推导。改成逐 task 的 `for` 循环：runner/setup/agent 构造异常由 evaluator 生成 `harness_error` failed row，已有 TaskState 的执行异常由 runner 收口；两者都继续后续任务，并最终写出完整 artifact。

verifier 抽成单独的 `VerifierRunner`：优先读取结构化 `verifier_argv`；历史 `verifier` 字符串只对仓库内受信 v1 benchmark 做兼容解析，使用 `shlex.split()` 转为 argv，把首项 `python3/python` 替换为 `sys.executable`，始终 `shell=False`。`verifier_timeout_s` 默认 10 秒并限制在 `[1, 60]`；超时返回 `failure_category="verifier_timeout"`，stdout/stderr 先裁剪、再走现有脱敏函数。任意 verifier 异常只失败当前 task。

benchmark loader 必须同步修改任务必填字段：`REQUIRED_TASK_KEYS` 不再无条件包含 `verifier`，校验阶段要求 `verifier` 和 `verifier_argv` 恰好出现一个；两者都缺失或同时存在都拒绝。规范化后的 task 保留实际提供的字段，evaluator 统一通过 `VerifierRunner` 执行，不能继续直接读取 `task["verifier"]`。

### 5c：`pico eval` 子命令

**`cli.py` 新增**

```python
def _add_eval_subcommand(subparsers):
    p = subparsers.add_parser("eval", help="run eval harness against benchmark tasks")
    p.add_argument("--tasks", default="benchmarks/coding_tasks.json")
    p.add_argument("--out", default=None, help="output JSON (default: benchmarks/results/<ts>-eval.json)")
    p.add_argument("--backend", choices=["native", "langgraph"], default="native")
```

**`_run_eval()` 处理 eval**

```python
def _run_eval(args):
    from pathlib import Path
    from datetime import datetime
    from pico.evaluation.evaluator import run_fixed_benchmark  # ← 必须显式 import
    out = args.out or f"benchmarks/results/{datetime.now().strftime('%Y%m%d-%H%M%S')}-eval.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    result = run_fixed_benchmark(
        benchmark_path=args.tasks,
        artifact_path=out,
        backend=args.backend,
    )
    s = result["summary"]
    print(f"tasks:{s['total_tasks']}  passed:{s['passed']}  failed:{s['failed']}  ({s['pass_rate']:.0%})")
    print(f"→ {out}")
    if s["eligible_tasks"] == 0:
        return 2  # 选定 backend 对全部 task 都不适用，属于无可执行 benchmark
    return 0 if s["failed"] == 0 else 1
```

---

## 阶段 6：Harness 补任务

新建独立的 `benchmarks/delegate_tasks.json`，在其 `tasks` 数组中放4个任务。不要修改或追加现有 `benchmarks/coding_tasks.json`，默认 12 题及历史评测口径保持不变：

| task id | 目的 | 验收 |
|---|---|---|
| `research_then_patch` | coordinator 先发 research delegate 收集证据，再 patch | trace 有 `delegate_started` + `delegate_finished`；文件被正确修改 |
| `review_catches_incomplete_fix` | reviewer 发现 patch 不完整，返回 `status: needs_fix`；coordinator 补修 | trace 有 `review_requested` + `review_failed`；最终 artifact 满足 acceptance |
| `delegate_write_denied` | research delegate（有 allowlist）尝试调用 `write_file`，被 `tool_not_allowed` 拦截 | evaluator 聚合后的 row 满足 `sandbox_violations >= 1`；violation 事件的 `tool_error_code="tool_not_allowed"`；最终任务仍成功 |
| `default_delegate_write_readonly_block` | default delegate（无 allowlist，`read_only=True`）尝试调用 `write_file`，被 `read_only_block` 拦截 | `backends=["native"]`；evaluator 聚合后的 row 满足 `sandbox_violations >= 1`；violation 事件的 `security_event_type="read_only_block"`；最终任务仍成功 |

> ⚠️ **`delegate_write_denied` 只覆盖 `tool_not_allowed`**：LangGraph research role 的 `allowed_tools=("list_files","read_file","search")`，`write_file` 会先被 allowlist 检查拦截（`tool_not_allowed`），到达不了 `approval_policy="never"` 的 `read_only_block` 检查。要覆盖 `read_only_block` 路径，使用 native-only 的旧 delegate（无 wrapper role spec），见 `default_delegate_write_readonly_block` 任务。

每个任务结构沿用现有 schema（`id / prompt / fixture_repo / allowed_tools / step_budget / expected_artifact / category`），并要求 `verifier` 与 `verifier_argv` 二选一；可增加 `acceptance`、`requires_research`、`focus_paths`、`artifact_path`、`verifier_timeout_s` 和 `backends`。`acceptance` 缺省时使用 `prompt`，`requires_research` 缺省为 `true` 且只接受 JSON boolean；`focus_paths/artifact_path` 只接受工作区内的非空相对路径，loader 必须拒绝绝对路径和规范化后越出 fixture root 的路径。历史任务缺省 `backends=["native"]`，需要 LangGraph 的任务必须显式声明 `backends=["langgraph"]`，需要双后端对比时显式声明 `["native", "langgraph"]`；只允许这两个值。不在任务 `backends` 中的后端输出 `status="skipped"`、`failure_category="backend_not_applicable"`，不计入 pass/fail 分母；review 路由只使用显式 `focus_paths/artifact_path`，不能调用 legacy fixture 映射；evaluator 的 artifact 验证仍可为旧 v1 task 使用 `_artifact_path_for_task()`。LangGraph role task 的父级 `allowed_tools` 必须显式包含 `delegate`，同时保留 Coordinator 完成任务所需的读写工具；native-only 的旧 delegate 回归任务只验证原有行为。

deterministic 脚本必须按 `(backend, task_id)` 分开，不能继续只按 task id 保存：

```python
SCRIPTED_MODEL_OUTPUTS = {
    "native": {
        "research_then_patch": [
            # native 只覆盖旧 delegate 合同；不模拟三角色 role routing。
            ...,
        ],
    },
    "langgraph": {
        "research_then_patch": [
            # LangGraph wrapper 直接调用 research role child；没有 native delegate tool 这一轮。
            ...,
        ],
    },
}

def _scripted_outputs_for_task(task, backend):
    return list(SCRIPTED_MODEL_OUTPUTS[backend][task["id"]])
```

evaluator 根据已选择的 backend 创建对应 `FakeModelClient` 后传给 runner。真实 provider 的 `model_client_factory(task, workspace)` 公共签名保持不变，不强迫外部 factory 接收 backend 参数。

artifact 验证仍需要确定文件路径，因此 `_artifact_path_for_task(task)` 改为“显式 `task["artifact_path"]` 优先、旧 fixture 映射兜底”；这个 helper 只供 evaluator 验证 artifact，不能供 LangGraph review 路由使用。这样新 delegate fixture 不必修改全局映射，旧 12 题也无需改 JSON。

> ⚠️ **FakeModelClient 共享问题**：native legacy delegate 和 LangGraph role child 都复用父级注入的 model client。FakeModelClient 内部维护一个响应队列，父子调用会**按实际调用顺序消费**同一个队列。
>
> 因此，每个 backend/task 的 `SCRIPTED_MODEL_OUTPUTS` 序列必须覆盖该 backend 中 parent 和 child 的全部响应，按实际调用顺序排列，例如下面只属于 `native`：
>
> ```python
> "research_then_patch": [
>     # native 旧 delegate 工具调用；不添加 mode 字段
>     '<tool>{"name":"delegate","args":{"task":"find where README is","max_steps":3}}</tool>',
>     # native legacy child round 1：调查
>     '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
>     # 子 agent（research）round 2：返回结论
>     "<final>Findings: README.md is at repo root.\nCandidate files: README.md\nSuggested action: patch line 1</final>",
>     # native parent round 2：执行 patch
>     '<tool name="patch_file" path="README.md"><old_text>...</old_text><new_text>...</new_text></tool>',
>     # 父 agent round 3：最终答案
>     "<final>Done.</final>",
> ],
> ```
>
> 如果序列长度或顺序不对，FakeModelClient 会耗尽响应（抛出 `RuntimeError`）或返回错误内容，导致任务意外失败；harness 在 backend 边界将该异常分类为 `model_error`，普通 native CLI 不改变原异常合同。

---

## 阶段 7：LangGraph 可选 wrapper

**位置**：`examples/langgraph-pico/`，独立 `pyproject.toml`，不影响主包。

> 目标：把 research → execute → review 这条多 agent 流程显式化为 LangGraph 节点，为后续接入持久 checkpointer/replay 提供边界；本期 smoke 不宣称已经启用持久化 replay。
> 如果只把 coordinator.ask() 套一层图，LangGraph 没有显式编排价值——所以 research_delegate 和 review_delegate 必须是独立节点。

首版 LangGraph 后端只支持 `pico eval --backend langgraph --tasks benchmarks/delegate_tasks.json`。runner 在建图前检查每个任务的 `allowed_tools` 显式包含 `delegate`；默认 `benchmarks/coding_tasks.json` 的 12 题保持原权限和 native 评测语义，不自动获得图编排能力。

smoke test 通过后，完成 `build_backend_runner()` 中只在选择 langgraph 时执行的懒加载分支，并把 CLI `--backend` choices 扩展为 `["native", "langgraph"]`。禁止在 `pico/evaluation/backends.py` 顶层 import example 或 LangGraph。

example 必须提供稳定的 Python 导入入口，目录至少为：

```text
examples/langgraph-pico/
  pyproject.toml
  src/langgraph_pico/__init__.py
  src/langgraph_pico/backend.py
  src/langgraph_pico/graph.py
```

其中 `langgraph_pico.backend` 导出 `LangGraphBackendRunner`，供主包的懒加载分支使用。

`LangGraphBackendRunner` 与 NativeRunner 接受同一 `event_sink_factory` 和已由 evaluator 选好的 model client。它先用主包 `HarnessModelClientAdapter` 包装 model client，再以 `event_sink_factory(run_store)` 创建 configured sink；后续 graph-level agent、research/review 和 executor 共用 `CompositeSink(event_collector, configured_sink)`。因此 NullSink 测试不需要修改图代码。

### 与之前设计的关键差异

- State 不放 Pico 实例（会导致 LangGraph checkpointer 序列化失败）
- **5 个节点**，research/review 是显式节点，不再内嵌在 coordinator.ask() 里
- Pico 通过 `config["configurable"]` 注入，不进 state

### 图结构

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

### State

```python
from typing import TypedDict

MAX_FIX_ATTEMPTS = 2

class AgentState(TypedDict):
    task: str
    acceptance: str          # task.get("acceptance", task["prompt"])
    step_budget: int
    coordinator_steps_used: int  # delegate 调度 + 所有 executor tool steps
    requires_research: bool  # task.get("requires_research", True)，plan 的唯一判断来源
    research_result: str    # plan/research_delegate 输出，传给 execute
    execution_result: str   # execute 节点输出摘要，传给 review_delegate
    affected_paths: list[str]  # execute 节点写入的文件列表
    review_focus_paths: list[str]  # task focus_paths/artifact path 后备，execute 后优先换成 affected_paths
    review_status: str      # "pass" | "needs_fix" | "no_changes_to_review" | ""
    review_issues: str      # review_delegate 返回的 issues
    fix_attempts: int       # 防止无限修复循环
    terminal_reason: str    # "" | "review_retry_limit_reached" | "no_changes_to_review" | "budget_exhausted" | "delegate_failed"
    delegate_failures: int
    final_result: str
```

### Pico 注入方式

```python
from langgraph.graph import StateGraph
from langchain_core.runnables import RunnableConfig

def _record_graph_delegate_call(agent):
    # native 路径由 AgentLoop.record_tool("delegate") 计数；图节点直接调用时要显式补一次。
    agent.current_task_state.record_tool("delegate")
    agent.run_store.write_task_state(agent.current_task_state)

def _require_graph_delegate_permission(agent):
    # allowed_tools=None 表示未启用 allowlist；否则必须和 native 一样显式允许 delegate。
    if agent.allowed_tools is not None and "delegate" not in agent.allowed_tools:
        raise PermissionError("langgraph task does not allow delegate")

def _call_graph_role_delegate(agent, spec: RoleDelegateSpec):
    _require_graph_delegate_permission(agent)
    _record_graph_delegate_call(agent)
    try:
        child, text = create_role_delegate(agent, spec)
        return {"ok": True, "text": text, "child": child}
    except Exception as exc:
        return {"ok": False, "text": "", "error_type": type(exc).__name__}

def research_node(state: AgentState, config: RunnableConfig) -> AgentState:
    # research 调度、至少一次 execute 工具、最终 review 各预留 1 步。
    if state["step_budget"] - state["coordinator_steps_used"] < 3:
        return {**state, "terminal_reason": "budget_exhausted"}
    agent = config["configurable"]["agent"]   # 从 config 拿，不存 state
    call = _call_graph_role_delegate(agent, RoleDelegateSpec(
        role="research",
        task=state["task"],
        allowed_tools=("list_files", "read_file", "search"),
        max_steps=3,
    ))
    if not call["ok"]:
        return {
            **state,
            "research_result": "research delegate failed; continue using workspace evidence",
            "delegate_failures": state["delegate_failures"] + 1,
            "coordinator_steps_used": state["coordinator_steps_used"] + 1,
        }
    return {
        **state,
        "research_result": call["text"],
        "coordinator_steps_used": state["coordinator_steps_used"] + 1,
    }
```

> ⚠️ **parent trace 问题**：LangGraph role child factory 只有在 `agent.current_task_state is not None` 时才会把 role 事件写入父 trace。LangGraph 节点在 `agent.ask()` 外直接运行时，必须先创建 graph-level TaskState，否则 role 事件会被静默丢弃。
>
> **修复方式**：`LangGraphBackendRunner` 在 `graph.invoke()` 之前，先手动创建一个 graph-level `TaskState` 并把它赋给 `agent.current_task_state`，同时调用 `agent.run_store.start_run(task_state)` 初始化 run dir，然后再 invoke 图：
>
> ```python
> import time
>
> from pico.task_state import (
>     STATUS_FAILED,
>     STOP_REASON_MODEL_ERROR,
>     STOP_REASON_NO_CHANGES_TO_REVIEW,
>     STOP_REASON_BUDGET_EXHAUSTED,
>     STOP_REASON_DELEGATE_FAILED,
>     STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
>     STOP_REASON_RUNTIME_ERROR,
>     STOP_REASON_PERSISTENCE_ERROR,
>     TaskState,
> )
>
> if "delegate" not in task["allowed_tools"]:
>     raise ValueError("langgraph backend requires allowed_tools to include delegate")
>
> task_input = task["prompt"]
> acceptance = task.get("acceptance", task_input)
> requires_research = task.get("requires_research", True)
> if not isinstance(requires_research, bool):
>     raise ValueError("requires_research must be a boolean")
> explicit_focus_paths = list(task.get("focus_paths") or [])
> explicit_artifact_path = str(task.get("artifact_path", "")).strip()
> initial_review_paths = explicit_focus_paths or ([explicit_artifact_path] if explicit_artifact_path else [])
> initial_state = {
>     "task": task_input,
>     "acceptance": acceptance,
>     "step_budget": int(task["step_budget"]),
>     "coordinator_steps_used": 0,
>     "delegate_failures": 0,
>     "requires_research": requires_research,
>     "research_result": "",
>     "execution_result": "",
>     "affected_paths": [],
>     "review_focus_paths": initial_review_paths,
>     "review_status": "",
>     "review_issues": "",
>     "fix_attempts": 0,
>     "terminal_reason": "",
>     "final_result": "",
> }
>
> event_collector = EventCollector()
> agent.event_sink = CompositeSink(event_collector, agent.event_sink)
> agent.child_task_states = []
> # 所有 delegate/executor 继承同一个 sink，确保一个 task 只有一条内存事件流。
> task_state = TaskState.create(
>     run_id=agent.new_run_id(),
>     task_id=agent.new_task_id(),
>     user_request=task_input,
> )
> agent.current_task_state = task_state
> agent.current_run_dir = agent.run_store.start_run(task_state)
> run_started_at = time.monotonic()
> agent.emit_trace(task_state, "run_started", {"user_request": task_input})
> node_child_states = []       # 只收 execute executor states
> budget_task_states = [task_state]
> initial_state_snapshot = {}
> result = None
> final_answer = ""
>
> try:
>     _apply_task_setup(agent, task, fixture_copy_root)
>     initial_memory_state = agent.memory.to_dict()
>     initial_state_snapshot = {
>         "initial_history_empty": len(agent.session["history"]) == 0,
>         "initial_memory_empty": memorylib.is_effectively_empty(initial_memory_state),
>         "initial_task_summary_empty": not str(initial_memory_state["working"]["task_summary"]).strip(),
>         "initial_episodic_notes_empty": not initial_memory_state["episodic_notes"],
>     }
>     result = graph.invoke(initial_state, config={
>         "configurable": {
>             "agent": agent,
>             "node_child_states": node_child_states,
>         }
>     })
>     budget_task_states = [task_state, *node_child_states]
>     measured_steps = sum(item.tool_steps for item in budget_task_states)
>     if measured_steps != result["coordinator_steps_used"]:
>         raise RuntimeError("graph budget counter drift")
>     final_answer = result["final_result"]
>     if result["review_status"] == "pass" and not result["terminal_reason"]:
>         task_state.finish_success(final_answer)
>     else:
>         stop_reason = {
>             "no_changes_to_review": STOP_REASON_NO_CHANGES_TO_REVIEW,
>             "review_retry_limit_reached": STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
>             "budget_exhausted": STOP_REASON_BUDGET_EXHAUSTED,
>             "delegate_failed": STOP_REASON_DELEGATE_FAILED,
>         }[result["terminal_reason"]]
>         task_state.stop(
>             stop_reason,
>             status=STATUS_FAILED,
>             final_answer=final_answer,
>         )
> except Exception as exc:
>     final_answer = f"LangGraph execution failed: {type(exc).__name__}"
>     # 模型调用边界抛出的异常必须携带 stop_reason=model_error；
>     # 未分类的节点/路由异常按 runtime_error 处理；持久化写入异常按 persistence_error 处理。
>     stop_reason = getattr(exc, "stop_reason", STOP_REASON_RUNTIME_ERROR)
>     if stop_reason not in {STOP_REASON_MODEL_ERROR, STOP_REASON_RUNTIME_ERROR, STOP_REASON_PERSISTENCE_ERROR}:
>         stop_reason = STOP_REASON_RUNTIME_ERROR
>     task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final_answer)
> finally:
>     # graph.invoke 的节点、模型或 checkpointer 异常也必须经过同一收尾路径。
>     if task_state.status == "running":
>         task_state.stop(STOP_REASON_RUNTIME_ERROR, status=STATUS_FAILED, final_answer=final_answer)
>     run_duration_ms = int((time.monotonic() - run_started_at) * 1000)
>     agent.run_store.write_task_state(task_state)
>     agent.emit_trace(task_state, "run_finished", {
>         "status": task_state.status,
>         "stop_reason": task_state.stop_reason,
>         "final_answer": final_answer,
>         "run_duration_ms": run_duration_ms,
>     })
>     agent.run_store.write_report(
>         task_state,
>         agent.redact_artifact(agent.build_report(task_state)),
>     )
>
> return BackendRunResult(
>     task_state,
>     final_answer,
>     agent,
>     child_task_states=[*agent.child_task_states, *node_child_states],
>     budget_task_states=budget_task_states,
>     initial_state=initial_state_snapshot,
>     events=event_collector.snapshot(),
> )
> ```

`finally` 中的 task state、`run_finished` 和 report 写入应封装为逐项 best-effort finalizer：某一项持久化失败时，把内存状态改为现有 `persistence_error`，继续尝试其余 artifact，最后再向上报告异常；不能因为第一项写入失败而跳过全部收尾。

`HarnessModelClientAdapter`、native runner 和 LangGraph runner 对外只允许写入异常类型、稳定 stop reason 和裁剪脱敏后的诊断；不得把原始 provider exception message 写入 `final_answer`、trace、report 或 evaluation artifact。verifier 的 benchmark 定义视为受信配置；`shell=False` 只能消除 shell 解释，不等于阻止任意可执行文件，后续如支持不受信 benchmark 必须增加 verifier executable allowlist 或独立 sandbox。

模型异常分类由 `pico.evaluation.backends.HarnessModelClientAdapter` 在 harness 内统一完成，native/LangGraph runner 都使用它；LangGraph 的 research/review delegate 和 executor 共享同一个已包装实例。主包 `pico/providers/clients.py` 和普通 native CLI 不修改。其他节点和路由异常归为 `runtime_error`，持久化写入失败归为 `persistence_error`；不要依赖错误消息字符串猜异常类型。

这里 `task` 始终是 benchmark 字典，进入 graph state 的 `task` 必须是 `task["prompt"]` 字符串；`acceptance` 单独初始化为 `task.get("acceptance", task["prompt"])`。不要再把整个任务字典传给声明为 `str` 的字段。

> 不要写 `graph.compile(configurable=...)`。对象注入发生在 invoke 的 `config["configurable"]`，或者在构建节点函数时通过闭包注入。

### 节点

| 节点 | 实现 | 说明 |
|---|---|---|
| `plan` | 读取已校验的 `state["requires_research"]`，不调用模型或 delegate | `true` → research_delegate，`false` → execute。**不调 `spawn_delegate`**，避免和 `research_delegate` 节点重复 research |
| `research_delegate` | 预算至少剩 3 步时 `_call_graph_role_delegate(agent, RoleDelegateSpec(role="research", ...))` | 调度计 1 步；不足时不调用 role child，设置 `budget_exhausted` |
| `route_after_research` | 条件边 | budget exhausted → finalize；否则 → execute |
| `execute` | 从剩余预算中预留 1 步 review，其余作为 executor `max_steps` | 去掉 `delegate` 工具；按 executor 实际 tool steps 累加 `coordinator_steps_used`，内部状态不持久回写 |
| `review_delegate` | 剩余至少 1 步时调用 review，再调用共享 `_normalize_review_result()` | 调度计 1 步；不足时不调用 delegate，设置 `budget_exhausted` |
| `route_after_execute` | 条件边 | 任意 `terminal_reason` → finalize；否则 review_focus_paths 非空 → review_delegate；条件函数不修改 state |
| `route_finish_or_fix` | 条件边 | 任意 `terminal_reason` 或 pass → finalize；`needs_fix` 且 `fix_attempts < MAX_FIX_ATTEMPTS` → execute；达到上限 → finalize |
| `finalize` | 根据纯数据状态写 `state["terminal_reason"]` 和 `state["final_result"]` | pass 返回 execution result；retry、无路径、预算不足和 delegate failure 分别保留明确诊断。节点不得自行修改 graph-level TaskState，runner 统一 finish/stop |

`finalize` 的终态转换固定为：

```python
def finalize_node(state: AgentState) -> AgentState:
    if state["terminal_reason"] == "budget_exhausted":
        return {**state, "final_result": "Coordinator step budget was exhausted."}
    if state["terminal_reason"] == "delegate_failed":
        return {**state, "final_result": "Review delegate failed; result could not be verified."}
    if state["terminal_reason"] == "no_changes_to_review":
        return {**state, "final_result": "No reviewable path was produced."}
    if state["review_status"] == "pass":
        return {**state, "terminal_reason": "", "final_result": state["execution_result"]}
    if state["review_status"] == "needs_fix" and state["fix_attempts"] >= MAX_FIX_ATTEMPTS:
        return {
            **state,
            "terminal_reason": "review_retry_limit_reached",
            "final_result": state["review_issues"],
        }
    raise RuntimeError("finalize received a non-terminal graph state")
```

完整建图必须显式写出节点、条件边和 compile；以下组装代码在实际 `graph.py` 中必须放在全部 node/route 函数定义之后。第一版不启用 checkpointer：

```python
from langgraph.graph import END, START, StateGraph

def plan_node(state):
    return state

def route_after_plan(state):
    return "research" if state["requires_research"] else "execute"

def route_after_research(state):
    return "finalize" if state["terminal_reason"] else "execute"

def route_after_execute(state):
    return "finalize" if state["terminal_reason"] else "review"

def route_finish_or_fix(state):
    if state["terminal_reason"] or state["review_status"] == "pass":
        return "finalize"
    return "execute" if state["fix_attempts"] < MAX_FIX_ATTEMPTS else "finalize"

builder = StateGraph(AgentState)
builder.add_node("plan", plan_node)
builder.add_node("research_delegate", research_node)
builder.add_node("execute", execute_node)
builder.add_node("review_delegate", review_node)
builder.add_node("finalize", finalize_node)
builder.add_edge(START, "plan")
builder.add_conditional_edges("plan", route_after_plan, {
    "research": "research_delegate", "execute": "execute",
})
builder.add_conditional_edges("research_delegate", route_after_research, {
    "execute": "execute", "finalize": "finalize",
})
builder.add_conditional_edges("execute", route_after_execute, {
    "review": "review_delegate", "finalize": "finalize",
})
builder.add_conditional_edges("review_delegate", route_finish_or_fix, {
    "execute": "execute", "finalize": "finalize",
})
builder.add_edge("finalize", END)
graph = builder.compile()
```

启用持久 checkpointer/replay 属于后续阶段；当前 `configurable` 中的 agent、collector 和可变 child-state collector 都不是可跨进程恢复的数据，不能据此宣称 durable execution 已完成。

execute 节点的正确构造方式示例：

阶段 3 已在主包增加默认开启的两个状态写入开关，并提供核心 `InMemorySessionStore`。LangGraph executor 复用这些能力，不在 example 内重复实现。

```python
def execute_node(state: AgentState, config: RunnableConfig) -> AgentState:
    from copy import deepcopy

    agent = config["configurable"]["agent"]
    remaining = state["step_budget"] - state["coordinator_steps_used"]
    if remaining <= 1:
        return {**state, "terminal_reason": "budget_exhausted"}
    executor_budget = remaining - 1  # 为 review 调度预留 1 步
    exec_allowed = [t for t in (agent.allowed_tools or list(agent.tools.keys())) if t != "delegate"]
    executor = Pico(
        model_client=agent.model_client,
        workspace=agent.workspace,
        session_store=InMemorySessionStore(),               # 主包实现，不写父 session 文件
        session=deepcopy(agent.session),                     # 只读取父 task setup/history/memory/checkpoint 快照
        run_store=agent.run_store,
        approval_policy=agent.approval_policy,
        max_steps=executor_budget,
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth,
        max_depth=agent.max_depth,
        allowed_tools=exec_allowed,
        event_sink=agent.event_sink,
        secret_env_names=agent.secret_env_names,           # 父配置继承，保证 secret 处理一致
        shell_env_allowlist=agent.shell_env_allowlist,     # 父配置继承，保证 shell env 过滤一致
        progress_callback=agent.progress_callback,         # 父配置继承，保证进度输出一致
        feature_flags=agent.feature_flags,                 # 父配置继承，保证 memory 等功能行为一致
        allow_checkpoint=False,                            # 可读快照中的 checkpoint，但不创建/回写
        allow_durable_memory_write=False,                  # 禁止写 workspace .pico/memory
    )
    # ⚠️ 不要在这里赋值 executor.current_task_state。
    # executor.ask() 内部的 AgentLoop.run() 会立即创建新的 TaskState 并覆盖它，
    # 所以这里赋值无效，"继承 parent trace"的说法是错误的。
    # 父 Coordinator 持有正式 session；executor 只返回 TaskState 和 graph state 数据。
    review_context = ""
    fix_attempts = state["fix_attempts"]
    if state["review_status"] == "needs_fix":
        fix_attempts += 1
        agent.emit_trace(agent.current_task_state, "review_retry_started", {
            "backend": "langgraph",
            "attempt": fix_attempts,
        })
        review_context = "\n\nReview issues to fix:\n" + state["review_issues"]
    prompt = state["task"] + "\n\nResearch findings:\n" + state["research_result"]
    prompt += review_context
    try:
        result = executor.ask(prompt)
    finally:
        # 失败的 executor state 也必须进入总指标和 Coordinator 预算。
        if executor.current_task_state is not None:
            config["configurable"]["node_child_states"].append(executor.current_task_state)
    affected = sorted(
        set(state["affected_paths"])
        | set(executor.current_task_state.affected_paths)
    )
    review_focus_paths = affected or list(state["review_focus_paths"])
    terminal_reason = "" if review_focus_paths else "no_changes_to_review"
    executor_steps = int(executor.current_task_state.tool_steps)
    return {
        **state,
        "execution_result": result,
        "affected_paths": affected,
        "review_focus_paths": review_focus_paths,
        "review_status": terminal_reason,
        "review_issues": "",
        "terminal_reason": terminal_reason,
        "fix_attempts": fix_attempts,
        "coordinator_steps_used": state["coordinator_steps_used"] + executor_steps,
    }
```

`affected_paths` 直接来自 executor 的 `TaskState`，不能从 trace 反推。review 路径优先使用实际 `affected_paths`，为空时只使用 task 中显式校验过的 `focus_paths/artifact_path`；不能调用 legacy `_artifact_path_for_task()`，否则当前 fixture 总会得到路径或提前抛错，`no_changes_to_review` 分支不可达。三者都为空时由 `route_after_execute` 进入失败终态。`expected_artifact` 是验收描述，不得直接当路径。`InMemorySessionStore` 来自主包；两个写入开关负责阻断 checkpoint 和 durable memory 副作用。

review 节点以解析后的 `review_focus_paths`、`state["acceptance"]` 和本轮 `execution_result` 构成非空 review packet，并复用 wrapper 的规范化函数：

```python
def review_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    if state["step_budget"] - state["coordinator_steps_used"] < 1:
        return {**state, "terminal_reason": "budget_exhausted"}
    call = _call_graph_role_delegate(agent, RoleDelegateSpec(
        role="review",
        task="Review whether the requested change is complete.",
        allowed_tools=("read_file", "search"),
        focus_paths=tuple(state["review_focus_paths"]),
        acceptance=state["acceptance"],
        context_summary=state["execution_result"],
        max_steps=3,
    ))
    if not call["ok"]:
        return {
            **state,
            "review_status": "",
            "review_issues": "delegate_failed",
            "terminal_reason": "delegate_failed",
            "delegate_failures": state["delegate_failures"] + 1,
            "coordinator_steps_used": state["coordinator_steps_used"] + 1,
        }
    text = call["text"].removeprefix("delegate_result:\n")
    review = _normalize_review_result(text)
    return {
        **state,
        "review_status": review["status"],
        "review_issues": review["text"],
        "delegate_failures": state["delegate_failures"],
        "coordinator_steps_used": state["coordinator_steps_used"] + 1,
    }
```

route 回到 execute 时保留 `review_status="needs_fix"` 和 `review_issues`；execute 消费它们、递增 `fix_attempts` 并发出一次 `review_retry_started`。`finalize_node` 对 pass、retry limit、no changes、budget exhausted 和 delegate failed 生成固定终态；runner 是唯一把纯数据终态映射到 graph-level TaskState stop reason 的位置。成功返回前还必须断言 `coordinator_steps_used == sum(budget_task_states.tool_steps)`，防止图状态与评测口径漂移。

阶段 7 至少新增以下回归测试：

1. 缺少 `delegate` 的任务在 graph invoke 前失败，research/review 节点不能绕过 allowlist。
2. `within_budget` 包含 graph-level 调度和所有 executor state，但不包含 research/review child 内部步骤。
3. executor 能从隔离快照读取 `_apply_task_setup()` 写入父 session 的 history、memory、checkpoint/freshness 状态，且执行后父 session 未被覆盖；research/review delegate 也不新增持久 session/checkpoint/durable memory。
4. LangGraph wrapper 的 malformed review 得到 `needs_fix` 和 `malformed_review_status`；native 不自动发起 review。
4a. delegate 异常时 child state、`delegate_failed` 事件和 `delegate_failures` 指标均保留；LangGraph research/review 节点按约定终态收口。
5. 节点、模型或 checkpointer 抛错后 graph-level state 为 failed，并写出带 `run_duration_ms` 的 `run_finished` 和 report。
6. 使用 `NullSink` 时 `affected_paths` 和 review 路由与默认 JsonlSink 一致。
7. 使用 `NullSink` 时 `BackendRunResult.events` 仍能得到与 JsonlSink 相同的 delegate/review/retry 指标。
8. `requires_research=false` 跳过 research；缺省/true 恰好执行一次 research，且 plan 不调用模型。
9. 实际 affected paths 为空时只回退 task 显式 `focus_paths/artifact_path`；全部为空时 reviewer 调用数为 0，stop reason 为 `no_changes_to_review`，且测试不会调用 legacy `_artifact_path_for_task()`。
10. executor 执行前后父 session/checkpoint 及 workspace durable memory 内容完全一致。
11. provider 异常在 native/LangGraph harness 中都记为 `model_error`；同一原始 client 通过普通 `Pico.ask()` 使用时仍保持原异常合同。
12. pass、retry 耗尽、无审查路径和预算不足分别映射到 success、`review_retry_limit_reached`、`no_changes_to_review` 和 `budget_exhausted`。
13. 同一 task 的 native/LangGraph FakeModel 序列互不复用，并分别按各自调用顺序跑通。
14. 通过 `event_sink_factory` 注入 NullSink，runner 不生成 JSONL 但事件指标保持一致。
15. native model exception 生成 failed row 和完整生命周期 artifact，后续 task 仍执行。
16. verifier 使用 `sys.executable`、`shell=False`；超时得到 `verifier_timeout` 且不阻塞后续 task。
17. v2 artifact 保留 v1 的 runtime/reproducibility/failure_category_counts 和既有 row 字段；v1 fixture 可兼容读取。
18. 图运行中 `coordinator_steps_used` 不超过 `step_budget`；预算不足时相关 node 未被调用。
19. configured sink 主动抛错时 Agent 仍完成，`BackendRunResult.events` 含脱敏 `event_sink_failed`。

### `pyproject.toml` 版本锁

先不锁具体版本，先跑通 smoke test 后再确认：

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "pico-langgraph-example"
version = "0.1.0"
dependencies = [
    "langgraph",      # smoke test 后替换为已验证的版本范围
]

[tool.setuptools.packages.find]
where = ["src"]
```

不要在 PEP 508 dependency 中写相对 `file://` URI；它不会可靠地按 example 当前目录解析。该 example 的本地开发安装契约固定为：

```bash
cd examples/langgraph-pico
python -m pip install -e ../.. -e .
```

前一个 editable target 安装仓库根的 `pico`，后一个安装 example 及其 `langgraph` 依赖。CI 使用相同的工作目录和命令。smoke test 跑通后，把 `langgraph` 替换成实际验证过的版本范围（例如 `"langgraph>=X.Y,<X.Z"`）。如果未来发布 example 包，再把 `pico` 改为可解析的发布版本依赖；本地开发文档不伪造不可移植的相对 URL。

---

## 改动文件速查

| 文件 | 改动类型 | 阶段 |
|---|---|---|
| `pico/task_state.py` | 补3字段+3方法+review retry limit/no changes/budget/runtime error 停机原因 | 1 |
| `pico/event_sink.py` | 新建 EventSink/JsonlSink/NullSink/EventCollector/CompositeSink | 2 |
| `pico/session_store.py` | 新增深拷贝隔离的 InMemorySessionStore | 3 |
| `pico/runtime.py` | `event_sink` 注入 + 向后兼容 child state 收集/失败收口 + 默认开启的状态写入开关；不改变 native delegate schema | 2, 3, 7 |
| `pico/tools.py` | 保持 native `DELEGATE_TOOL_SPEC`、校验和 example 不变 | 回归门禁 |
| `pico/tool_executor.py` | 循环导入修复已完成；4处补 `record_sandbox_violation()` + `emit_trace` | 0, 4 |
| `pico/agent_loop.py` | 补恢复计数和 checkpoint/durable memory 写入开关；LangGraph retry 由 wrapper 图控制，native 不新增 review 路由 | 4, 7 |
| `pico/cli.py` | argparse 子命令重构 + eval 命令 | 5 |
| `pico/evaluation/backends.py` | BackendRunner、sink factory、harness model adapter、NativeRunner 异常收口及 langgraph 懒加载 | 5, 7 |
| `pico/evaluation/verifier.py` | 新建结构化 verifier runner、Python 解释器规范化和 timeout | 5 |
| `pico/evaluation/evaluator.py` | 逐 task 容错、backend-specific scripted outputs、result v1/v2 reader/writer | 5, 6 |
| `pico/evaluation/metrics.py` | 审计历史 result 消费点并统一走 v1/v2 reader | 5 |
| `tests/test_evaluator.py` | v2/v1、逐 task 容错、backend scripts、sink factory 和 verifier 回归 | 5, 6 |
| `tests/test_delegate.py` | research/review 内存 session、checkpoint 和 durable memory 隔离 | 3 |
| `tests/test_langgraph_backend.py` | 图 wiring、预算、终态和 backend-specific 调用顺序 | 7 |
| `benchmarks/delegate_tasks.json` | 新建4个 delegate/review/sandbox 任务 | 6 |
| `examples/langgraph-pico/` | 新建目录 | 7 |

---

## 原生兼容回归门禁

以下测试全部通过后，才能声称本次重构不影响原生 Pico 的正常使用：

1. 在未安装 `langgraph` 和 `pico-langgraph-example` 的环境中，`python -c "import pico"`、裸 `pico`、`pico <prompt>` 和完整 native 测试正常。
2. `build_arg_parser().parse_args(["--cwd", ...])` 公共用法保持不变；`pico --help` 仍显示 `--provider/--cwd` 等 run 参数。
3. 旧 delegate 的 `task/max_steps` 参数和正整数 `max_steps`（包括大于 12）仍可用；`RoleDelegateSpec` 的 `[1, 12]` 上限只在 LangGraph wrapper 生效。
4. 同一 `Pico` 连续执行两次 `ask()` 时，第二轮 `child_task_states` 不包含第一轮数据，session/history 仍按原生 REPL 规则延续。
5. 默认 `JsonlSink` 下既有事件名和 JSONL 字段保持兼容；`NullSink` 不创建 trace 文件。
6. native 新建 session 的初始 prompt 仍只包含旧 delegate schema；LangGraph role spec 不进入 native prompt。旧 checkpoint 的 session/history 不丢失。
7. `pico eval --backend native` 不导入 `langgraph_pico`；只有显式选择 langgraph backend 时，缺少可选包才返回清楚的安装提示。

`run` / `eval` 作为首词现在是 CLI 保留字，这是新增子命令不可避免的显式例外，不应再描述为百分之百字面兼容；普通 prompt 和原有 flags 必须保持兼容。

---

## 不需要改动的文件

| 文件 | 原因 |
|---|---|
| `pico/features/memory.py` | LayeredMemory 三层已完整 |
| `pico/checkpoint.py` | checkpoint/resume 已完整 |
| `pico/security.py` | 基础约束已有，阶段4只接计数，不改此文件 |
| `benchmarks/coding_tasks.json` | 默认 12 题保持原样；新增任务放入 `delegate_tasks.json` |

---

## 注意事项

1. 默认 sink 必须在 `self.run_store` 赋值之后创建，并用 `event_sink is not None` 判断，不能把合法但 falsey 的自定义 sink 替换掉。
2. native `spawn_delegate` 和 wrapper role factory 在单元测试直接调用时都可能没有父 `TaskState`，emit_trace 前必须判断。
3. `RoleDelegateSpec` 的字段校验必须留在 wrapper/factory；native `validate_tool` 不识别 `mode`、`role` 或 review packet 字段。
4. native `spawn_delegate` 保留现有 session store、memory、checkpoint 和默认权限语义；wrapper role child 才使用内存 session 和关闭的写入开关。
5. child state 收集是新增的旁路能力；每次 wrapper graph task 开始清空 collector。所有计数只在 evaluator 聚合一次，禁止同时写回父 TaskState。
6. LangGraph 图里 research/review 节点通过 `_call_graph_role_delegate` 检查权限后调用内部 `create_role_delegate`，不要调用 native `spawn_delegate` 或在 coordinator 节点内部再次驱动 role。
7. `tool_executor.py` 的循环导入修复已经完成；后续只需保留 `python -c "import pico"` 回归测试。
8. `langgraph_pico` 只能在 `name == "langgraph"` 分支内导入；native 模块顶层不得出现 LangGraph/example import。
