"""Pure-data LangGraph orchestration for Pico's three roles."""

from copy import deepcopy
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from pico.delegates import RoleDelegateSpec, create_role_delegate, normalize_review_result
from pico.runtime import Pico
from pico.session_store import InMemorySessionStore

MAX_FIX_ATTEMPTS = 2


class AgentState(TypedDict):
    task: str
    acceptance: str
    step_budget: int
    coordinator_steps_used: int
    requires_research: bool
    research_result: str
    execution_result: str
    affected_paths: list[str]
    review_focus_paths: list[str]
    review_status: str
    review_issues: str
    fix_attempts: int
    terminal_reason: str
    delegate_failures: int
    final_result: str


def _record_graph_delegate_call(agent):
    agent.current_task_state.record_tool("delegate")
    agent.run_store.write_task_state(agent.current_task_state)


def _require_graph_delegate_permission(agent):
    if agent.allowed_tools is not None and "delegate" not in agent.allowed_tools:
        raise PermissionError("langgraph task does not allow delegate")


def _call_graph_role_delegate(agent, spec):
    _require_graph_delegate_permission(agent)
    _record_graph_delegate_call(agent)
    try:
        child, text = create_role_delegate(agent, spec)
        return {"ok": True, "text": text, "child": child}
    except Exception as exc:
        return {"ok": False, "text": "", "error_type": type(exc).__name__}


def plan_node(state: AgentState) -> AgentState:
    return state


def route_after_plan(state: AgentState):
    return "research" if state["requires_research"] else "execute"


def research_node(state: AgentState, config: RunnableConfig) -> AgentState:
    if state["step_budget"] - state["coordinator_steps_used"] < 3:
        return {**state, "terminal_reason": "budget_exhausted"}
    agent = config["configurable"]["agent"]
    call = _call_graph_role_delegate(
        agent,
        RoleDelegateSpec(
            role="research",
            task=state["task"],
            allowed_tools=("list_files", "read_file", "search"),
            max_steps=3,
        ),
    )
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


def route_after_research(state: AgentState):
    return "finalize" if state["terminal_reason"] else "execute"


def execute_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    remaining = state["step_budget"] - state["coordinator_steps_used"]
    if remaining <= 1:
        return {**state, "terminal_reason": "budget_exhausted"}
    executor_budget = remaining - 1
    exec_allowed = [
        tool for tool in (agent.allowed_tools or list(agent.tools.keys())) if tool != "delegate"
    ]
    executor = Pico(
        model_client=agent.model_client,
        workspace=agent.workspace,
        session_store=InMemorySessionStore(),
        session=deepcopy(agent.session),
        run_store=agent.run_store,
        approval_policy=agent.approval_policy,
        max_steps=executor_budget,
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth,
        max_depth=agent.max_depth,
        allowed_tools=exec_allowed,
        event_sink=agent.event_sink,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
        progress_callback=agent.progress_callback,
        feature_flags=agent.feature_flags,
        allow_checkpoint=False,
        allow_durable_memory_write=False,
    )
    review_context = ""
    fix_attempts = state["fix_attempts"]
    if state["review_status"] == "needs_fix":
        fix_attempts += 1
        agent.emit_trace(
            agent.current_task_state,
            "review_retry_started",
            {"backend": "langgraph", "attempt": fix_attempts},
        )
        review_context = "\n\nReview issues to fix:\n" + state["review_issues"]
    prompt = state["task"] + "\n\nResearch findings:\n" + state["research_result"]
    prompt += review_context
    try:
        result = executor.ask(prompt)
    finally:
        if executor.current_task_state is not None:
            config["configurable"]["node_child_states"].append(executor.current_task_state)

    affected = sorted(set(state["affected_paths"]) | set(executor.current_task_state.affected_paths))
    review_focus_paths = affected or list(state["review_focus_paths"])
    terminal_reason = "" if review_focus_paths else "no_changes_to_review"
    return {
        **state,
        "execution_result": result,
        "affected_paths": affected,
        "review_focus_paths": review_focus_paths,
        "review_status": terminal_reason,
        "review_issues": "",
        "terminal_reason": terminal_reason,
        "fix_attempts": fix_attempts,
        "coordinator_steps_used": state["coordinator_steps_used"] + executor.current_task_state.tool_steps,
    }


def route_after_execute(state: AgentState):
    return "finalize" if state["terminal_reason"] else "review"


def review_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    if state["step_budget"] - state["coordinator_steps_used"] < 1:
        return {**state, "terminal_reason": "budget_exhausted"}
    call = _call_graph_role_delegate(
        agent,
        RoleDelegateSpec(
            role="review",
            task="Review whether the requested change is complete.",
            allowed_tools=("read_file", "search"),
            focus_paths=tuple(state["review_focus_paths"]),
            acceptance=state["acceptance"],
            context_summary=state["execution_result"],
            max_steps=3,
        ),
    )
    if not call["ok"]:
        return {
            **state,
            "review_status": "",
            "review_issues": "delegate_failed",
            "terminal_reason": "delegate_failed",
            "delegate_failures": state["delegate_failures"] + 1,
            "coordinator_steps_used": state["coordinator_steps_used"] + 1,
        }
    review = normalize_review_result(call["text"])
    return {
        **state,
        "review_status": review["status"],
        "review_issues": review["text"],
        "coordinator_steps_used": state["coordinator_steps_used"] + 1,
    }


def route_finish_or_fix(state: AgentState):
    if state["terminal_reason"] or state["review_status"] == "pass":
        return "finalize"
    return "execute" if state["fix_attempts"] < MAX_FIX_ATTEMPTS else "finalize"


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


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("research_delegate", research_node)
    builder.add_node("execute", execute_node)
    builder.add_node("review_delegate", review_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges(
        "plan", route_after_plan, {"research": "research_delegate", "execute": "execute"}
    )
    builder.add_conditional_edges(
        "research_delegate", route_after_research, {"execute": "execute", "finalize": "finalize"}
    )
    builder.add_conditional_edges(
        "execute", route_after_execute, {"review": "review_delegate", "finalize": "finalize"}
    )
    builder.add_conditional_edges(
        "review_delegate", route_finish_or_fix, {"execute": "execute", "finalize": "finalize"}
    )
    builder.add_edge("finalize", END)
    return builder.compile()
