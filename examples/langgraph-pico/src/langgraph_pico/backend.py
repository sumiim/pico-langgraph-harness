"""LangGraph backend adapter for Pico's benchmark harness."""

import time

from pico.evaluation.backends import (
    BackendRunResult,
    HarnessModelClientAdapter,
    default_event_sink_factory,
)
from pico.evaluation.evaluator import _apply_task_setup
from pico.event_sink import CompositeSink, EventCollector
from pico.features import memory as memorylib
from pico.run_lifecycle import finalize_run
from pico.runtime import Pico
from pico.task_state import (
    STATUS_FAILED,
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_DELEGATE_FAILED,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_NO_CHANGES_TO_REVIEW,
    STOP_REASON_PERSISTENCE_ERROR,
    STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
    STOP_REASON_RUNTIME_ERROR,
    TaskState,
)
from pico.workspace import now

from .graph import build_graph


def _initial_state_snapshot(agent):
    memory_state = agent.memory.to_dict()
    return {
        "initial_history_empty": len(agent.session["history"]) == 0,
        "initial_memory_empty": memorylib.is_effectively_empty(memory_state),
        "initial_task_summary_empty": not str(memory_state["working"]["task_summary"]).strip(),
        "initial_episodic_notes_empty": not memory_state["episodic_notes"],
    }


def _normalized_focus_paths(agent, focus_paths):
    normalized = []
    for raw_path in focus_paths or ():
        path = agent.tool_context().path(str(raw_path).strip())
        relative = path.relative_to(agent.root).as_posix()
        if relative and relative not in normalized:
            normalized.append(relative)
    return normalized


def run_agent(
    agent,
    task_input,
    *,
    acceptance=None,
    step_budget=None,
    requires_research=True,
    focus_paths=None,
    record_session=True,
):
    """Run the LangGraph workflow with an already configured Pico instance."""
    task_input = str(task_input).strip()
    if not task_input:
        raise ValueError("task_input must not be empty")
    if not isinstance(requires_research, bool):
        raise ValueError("requires_research must be a boolean")
    step_budget = int(agent.max_steps if step_budget is None else step_budget)
    if step_budget < 1:
        raise ValueError("step_budget must be positive")
    review_paths = _normalized_focus_paths(agent, focus_paths)
    initial_state = _initial_state_snapshot(agent)

    original_sink = agent.event_sink
    original_model_client = agent.model_client
    collector = EventCollector()
    agent.event_sink = CompositeSink(collector, original_sink)
    if not isinstance(original_model_client, HarnessModelClientAdapter):
        agent.model_client = HarnessModelClientAdapter(original_model_client)

    task_state = None
    node_child_states = []
    budget_task_states = []
    final_answer = ""
    try:
        if record_session:
            agent.memory.set_task_summary(task_input)
            agent.session["memory"] = agent.memory.to_dict()

        task_state = TaskState.create(
            run_id=agent.new_run_id(),
            task_id=agent.new_task_id(),
            user_request=task_input,
        )
        agent.current_task_state = task_state
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.child_task_states = []
        agent.emit_trace(task_state, "run_started", {"user_request": task_input, "backend": "langgraph"})
        started_at = time.monotonic()
        budget_task_states = [task_state]
        graph_state = {
            "task": task_input,
            "acceptance": str(acceptance or task_input),
            "step_budget": step_budget,
            "coordinator_steps_used": 0,
            "delegate_failures": 0,
            "requires_research": requires_research,
            "research_result": "",
            "execution_result": "",
            "affected_paths": [],
            "review_focus_paths": review_paths,
            "review_status": "",
            "review_issues": "",
            "fix_attempts": 0,
            "terminal_reason": "",
            "final_result": "",
        }

        try:
            result = build_graph().invoke(
                graph_state,
                config={"configurable": {"agent": agent, "node_child_states": node_child_states}},
            )
            budget_task_states = [task_state, *node_child_states]
            measured_steps = sum(state.tool_steps for state in budget_task_states)
            if measured_steps != result["coordinator_steps_used"]:
                raise RuntimeError("graph budget counter drift")
            final_answer = result["final_result"]
            if result["review_status"] == "pass" and not result["terminal_reason"]:
                task_state.finish_success(final_answer)
            else:
                stop_reason = {
                    "no_changes_to_review": STOP_REASON_NO_CHANGES_TO_REVIEW,
                    "review_retry_limit_reached": STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
                    "budget_exhausted": STOP_REASON_BUDGET_EXHAUSTED,
                    "delegate_failed": STOP_REASON_DELEGATE_FAILED,
                }[result["terminal_reason"]]
                task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final_answer)
        except Exception as exc:
            final_answer = f"LangGraph execution failed: {type(exc).__name__}"
            stop_reason = getattr(exc, "stop_reason", STOP_REASON_RUNTIME_ERROR)
            if stop_reason not in {
                STOP_REASON_MODEL_ERROR,
                STOP_REASON_RUNTIME_ERROR,
                STOP_REASON_PERSISTENCE_ERROR,
            }:
                stop_reason = STOP_REASON_RUNTIME_ERROR
            task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final_answer)
        finally:
            budget_task_states = [task_state, *node_child_states]
            if task_state.status == "running":
                task_state.stop(
                    STOP_REASON_RUNTIME_ERROR,
                    status=STATUS_FAILED,
                    final_answer=final_answer,
                )
            finalize_run(
                agent,
                task_state,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            final_answer = task_state.final_answer

        if record_session:
            agent.record({"role": "user", "content": task_input, "created_at": now()})
            agent.record({"role": "assistant", "content": final_answer, "created_at": now()})

        return BackendRunResult(
            task_state=task_state,
            final_answer=final_answer,
            agent=agent,
            child_task_states=[*agent.child_task_states, *node_child_states],
            budget_task_states=budget_task_states,
            initial_state=initial_state,
            events=collector.snapshot(),
        )
    finally:
        agent.event_sink = original_sink
        agent.model_client = original_model_client


class LangGraphBackendRunner:
    def __init__(self, max_new_tokens=64, model_client_factory=None, event_sink_factory=None):
        self.max_new_tokens = int(max_new_tokens)
        self.model_client_factory = model_client_factory
        self.event_sink_factory = event_sink_factory or default_event_sink_factory

    def run_task(self, task, workspace, session_store, run_store, fixture_copy_root, model_client=None):
        if "delegate" not in task["allowed_tools"]:
            raise ValueError("langgraph backend requires allowed_tools to include delegate")
        requires_research = task.get("requires_research", True)
        if not isinstance(requires_research, bool):
            raise ValueError("requires_research must be a boolean")
        if model_client is None and self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        if model_client is None:
            raise ValueError("LangGraphBackendRunner requires model_client or model_client_factory")

        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
            allowed_tools=task["allowed_tools"],
            event_sink=self.event_sink_factory(run_store),
        )
        _apply_task_setup(agent, task, fixture_copy_root)
        explicit_focus = list(task.get("focus_paths") or [])
        explicit_artifact = str(task.get("artifact_path", "")).strip()
        review_paths = explicit_focus or ([explicit_artifact] if explicit_artifact else [])
        return run_agent(
            agent,
            task["prompt"],
            acceptance=task.get("acceptance", task["prompt"]),
            step_budget=int(task["step_budget"]),
            requires_research=requires_research,
            focus_paths=review_paths,
            record_session=False,
        )
