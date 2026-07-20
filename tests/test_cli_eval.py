from pathlib import Path

from pico import cli


def test_public_run_parser_keeps_legacy_shape(tmp_path):
    args = cli.build_arg_parser().parse_args(["legacy prompt", "--cwd", str(tmp_path)])

    assert args.prompt == ["legacy prompt"]
    assert not hasattr(args, "command")


def test_internal_parser_supports_run_and_eval():
    run_args = cli._build_command_parser().parse_args(["run", "hello"])
    eval_args = cli._build_command_parser().parse_args(
        ["eval", "--backend", "langgraph", "--tasks", "benchmarks/delegate_tasks.json"]
    )

    assert run_args.command == "run"
    assert run_args.prompt == ["hello"]
    assert eval_args.command == "eval"
    assert eval_args.backend == "langgraph"


def test_eval_main_returns_two_when_no_tasks_apply(monkeypatch, tmp_path, capsys):
    output = tmp_path / "eval.json"

    def fake_run_fixed_benchmark(**kwargs):
        assert kwargs["backend"] == "langgraph"
        assert Path(kwargs["artifact_path"]) == output
        return {
            "summary": {
                "total_tasks": 1,
                "eligible_tasks": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": 0.0,
            }
        }

    monkeypatch.setattr("pico.evaluation.evaluator.run_fixed_benchmark", fake_run_fixed_benchmark)

    exit_code = cli.main(["eval", "--backend", "langgraph", "--out", str(output)])

    assert exit_code == 2
    assert "tasks:1" in capsys.readouterr().out
