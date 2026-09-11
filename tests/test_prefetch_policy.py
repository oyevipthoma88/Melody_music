import ast
from pathlib import Path
from types import SimpleNamespace


_SOURCE = Path(__file__).resolve().parents[1] / "melody/core/call.py"
_TREE = ast.parse(_SOURCE.read_text(encoding="utf-8"))
_FUNCTION = next(
    node for node in _TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "_prefetch_size_estimate"
)
_NAMESPACE = {}
exec(compile(ast.Module(body=[_FUNCTION], type_ignores=[]), str(_SOURCE), "exec"), _NAMESPACE)
_prefetch_size_estimate = _NAMESPACE["_prefetch_size_estimate"]


def test_audio_prefetch_estimate_is_duration_based():
    track = SimpleNamespace(duration=300, video=False)
    assert _prefetch_size_estimate(track) == 9_600_000


def test_video_prefetch_estimate_is_more_conservative():
    audio = _prefetch_size_estimate(SimpleNamespace(duration=600, video=False))
    video = _prefetch_size_estimate(SimpleNamespace(duration=600, video=True))
    assert video > audio


def test_unknown_duration_is_safe_for_metadata_only_policy():
    assert _prefetch_size_estimate(SimpleNamespace(duration=0, video=False)) == 0
