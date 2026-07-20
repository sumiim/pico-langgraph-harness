import shutil
from pathlib import Path

from pico.evaluation.backends import build_backend_runner
from pico.evaluation.evaluator import _scripted_outputs_for_task, load_benchmark
from pico.event_sink import NullSink
from pico.providers.clients import FakeModelClient
from pico.run_store import RunStore
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


def _run_task(tmp_path, task, outputs, *, null_sink=False):
    source = Path(task["fixture_repo"])
    fixture_root = tmp_path / source.name
    shutil.copytree(source, fixture_root)
    workspace = WorkspaceContext.build(fixture_root, repo_root_override=fixture_root)
    session_store = SessionStore(fixture_root / ".pico" / "sessions")
    run_store = RunStore(fixture_root / ".pico" / "runs")
    kwargs = {"event_sink_factory": (lambda _: NullSink())} if null_sink else {}
    runner = build_backend_runner("langgraph", **kwargs)
    result = runner.run_task(
        task,
        workspace,
        session_store,
        run_store,
        fixture_root,
        model_client=FakeModelClient(outputs),
    )
    return result, fixture_root


def test_langgraph_research_execute_review_uses_isolated_children_and_memory_events(tmp_path):
    task = load_benchmark("benchmarks/delegate_tasks.json")["tasks"][0]
    result, fixture_root = _run_task(
        tmp_path,
        task,
        _scripted_outputs_for_task(task, "langgraph"),
        null_sink=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.agent.session["history"] == []
    assert len(result.budget_task_states) == 2
    assert sum(state.tool_steps for state in result.budget_task_states) == 3
    assert len(result.child_task_states) == 3
    assert not result.agent.run_store.trace_path(result.task_state).exists()
    events = list(result.events)
    assert sum(event.get("event") == "delegate_started" for event in events) == 1
    assert sum(event.get("event") == "review_requested" for event in events) == 1
    assert any(event.get("event") == "review_passed" for event in events)
    assert "delegated research" in (fixture_root / "README.md").read_text(encoding="utf-8")


def test_langgraph_no_review_path_stops_without_calling_reviewer(tmp_path):
    task = {
        "id": "no_review_path",
        "prompt": "Inspect without changing files.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file"],
        "step_budget": 3,
        "expected_artifact": "no change",
        "category": "contract",
        "requires_research": False,
    }
    result, _ = _run_task(tmp_path, task, ["<final>No changes.</final>"])

    assert result.task_state.stop_reason == "no_changes_to_review"
    assert not any(event.get("event") == "review_requested" for event in result.events)


def test_langgraph_budget_exhaustion_starts_no_delegate_or_model(tmp_path):
    task = {
        "id": "budget_exhaustion",
        "prompt": "Research and patch README.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file", "patch_file"],
        "step_budget": 2,
        "expected_artifact": "no change",
        "category": "contract",
        "requires_research": True,
        "artifact_path": "README.md",
    }
    result, _ = _run_task(tmp_path, task, [])

    assert result.task_state.stop_reason == "budget_exhausted"
    assert result.task_state.tool_steps == 0
    assert not any(event.get("event") in {"delegate_started", "review_requested"} for event in result.events)


def test_langgraph_model_failure_retains_executor_budget_state(tmp_path):
    task = {
        "id": "executor_model_failure",
        "prompt": "Patch README.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file", "patch_file"],
        "step_budget": 3,
        "expected_artifact": "README",
        "category": "contract",
        "requires_research": False,
        "artifact_path": "README.md",
    }
    result, _ = _run_task(tmp_path, task, [])

    assert result.task_state.stop_reason == "model_error"
    assert len(result.budget_task_states) == 2
    assert result.budget_task_states[1].attempts == 1
    assert result.budget_task_states[1] in result.child_task_states


def test_langgraph_children_do_not_write_back_parent_setup_session(tmp_path):
    task = {
        "id": "isolated_setup",
        "prompt": "Inspect README without changing it.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file"],
        "step_budget": 3,
        "expected_artifact": "README",
        "category": "contract",
        "requires_research": False,
        "artifact_path": "README.md",
        "acceptance": "README remains readable.",
        "setup": {"kind": "context_reduction", "history_count": 2, "note_count": 1},
    }
    result, fixture_root = _run_task(
        tmp_path,
        task,
        [
            "<final>README inspected without changes.</final>",
            "<final>status: pass\nissues: none\nverify_targets: README.md</final>",
        ],
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert len(result.agent.session["history"]) == 2
    assert all("benchmark-history" in item["content"] for item in result.agent.session["history"])
    assert not (fixture_root / ".pico" / "memory").exists()


def test_langgraph_review_retry_limit_has_stable_terminal_reason(tmp_path):
    task = {
        "id": "review_retry_limit",
        "prompt": "Inspect README and satisfy review.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file"],
        "step_budget": 6,
        "expected_artifact": "README",
        "category": "contract",
        "requires_research": False,
        "artifact_path": "README.md",
        "acceptance": "Reviewer must pass.",
    }
    outputs = []
    for attempt in range(3):
        outputs.extend(
            [
                f"<final>Execution attempt {attempt + 1}.</final>",
                "<final>status: needs_fix\nissue: still incomplete\nverify_targets: README.md</final>",
            ]
        )
    result, _ = _run_task(tmp_path, task, outputs)

    assert result.task_state.stop_reason == "review_retry_limit_reached"
    assert sum(event.get("event") == "review_retry_started" for event in result.events) == 2


def test_langgraph_graph_state_contains_only_declared_data_fields():
    from langgraph_pico.graph import AgentState

    assert "agent" not in AgentState.__annotations__
    assert "model_client" not in AgentState.__annotations__


def test_run_agent_reuses_cli_runtime_and_records_parent_session(tmp_path):
    from langgraph_pico import run_agent
    from pico import Pico

    task = load_benchmark("benchmarks/delegate_tasks.json")["tasks"][0]
    source = Path(task["fixture_repo"])
    fixture_root = tmp_path / source.name
    shutil.copytree(source, fixture_root)
    model_client = FakeModelClient(_scripted_outputs_for_task(task, "langgraph"))
    event_sink = NullSink()
    agent = Pico(
        model_client=model_client,
        workspace=WorkspaceContext.build(fixture_root, repo_root_override=fixture_root),
        session_store=SessionStore(fixture_root / ".pico" / "sessions"),
        run_store=RunStore(fixture_root / ".pico" / "runs"),
        approval_policy="auto",
        max_steps=5,
        event_sink=event_sink,
    )

    result = run_agent(
        agent,
        task["prompt"],
        acceptance=task["acceptance"],
        step_budget=task["step_budget"],
        requires_research=True,
        focus_paths=[task["artifact_path"]],
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert [item["role"] for item in agent.session["history"]] == ["user", "assistant"]
    assert agent.model_client is model_client
    assert agent.event_sink is event_sink
    assert any(event.get("event") == "review_passed" for event in result.events)
    assert "delegated research" in (fixture_root / "README.md").read_text(encoding="utf-8")
