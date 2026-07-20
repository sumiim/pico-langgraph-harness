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

from .graph import build_graph


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

        collector = EventCollector()
        event_sink = CompositeSink(collector, self.event_sink_factory(run_store))
        agent = Pico(
            model_client=HarnessModelClientAdapter(model_client),
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
            allowed_tools=task["allowed_tools"],
            event_sink=event_sink,
        )
        task_input = task["prompt"]
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
        node_child_states = []
        budget_task_states = [task_state]
        initial_state_snapshot = {}
        final_answer = ""

        explicit_focus = list(task.get("focus_paths") or [])
        explicit_artifact = str(task.get("artifact_path", "")).strip()
        review_paths = explicit_focus or ([explicit_artifact] if explicit_artifact else [])
        graph_state = {
            "task": task_input,
            "acceptance": task.get("acceptance", task_input),
            "step_budget": int(task["step_budget"]),
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
            _apply_task_setup(agent, task, fixture_copy_root)
            memory_state = agent.memory.to_dict()
            initial_state_snapshot = {
                "initial_history_empty": len(agent.session["history"]) == 0,
                "initial_memory_empty": memorylib.is_effectively_empty(memory_state),
                "initial_task_summary_empty": not str(memory_state["working"]["task_summary"]).strip(),
                "initial_episodic_notes_empty": not memory_state["episodic_notes"],
            }
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

        return BackendRunResult(
            task_state=task_state,
            final_answer=final_answer,
            agent=agent,
            child_task_states=[*agent.child_task_states, *node_child_states],
            budget_task_states=budget_task_states,
            initial_state=initial_state_snapshot,
            events=collector.snapshot(),
        )
