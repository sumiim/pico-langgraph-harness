import pytest

from pico.evaluation.backends import (
    HarnessModelClientAdapter,
    ModelBoundaryError,
    build_backend_runner,
)
from pico.providers.clients import FakeModelClient


def test_harness_model_adapter_classifies_without_exposing_message():
    adapter = HarnessModelClientAdapter(FakeModelClient([]))

    with pytest.raises(ModelBoundaryError) as caught:
        adapter.complete("prompt", 10)

    assert caught.value.stop_reason == "model_error"
    assert str(caught.value) == "model call failed: RuntimeError"
    assert "outputs" not in str(caught.value)


def test_native_backend_does_not_import_optional_langgraph(monkeypatch):
    imported = []
    original_import = __import__

    def recording_import(name, *args, **kwargs):
        imported.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", recording_import)
    runner = build_backend_runner("native")

    assert runner.__class__.__name__ == "NativeBackendRunner"
    assert not any(name.startswith("langgraph_pico") for name in imported)


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown backend"):
        build_backend_runner("missing")
