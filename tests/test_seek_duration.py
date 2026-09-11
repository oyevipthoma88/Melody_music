import ast
from pathlib import Path


_SOURCE = Path(__file__).resolve().parents[1] / "melody/plugins/music/seek.py"
_TREE = ast.parse(_SOURCE.read_text(encoding="utf-8"))
_FUNCTION = next(
    node for node in _TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "_parse_duration"
)
_re = __import__("re")
_NAMESPACE = {
    "re": _re,
    "_DURATION_TOKEN": _re.compile(
        r"(?P<value>\d+(?:\.\d+)?)(?P<unit>[hms]?)", _re.IGNORECASE
    ),
}
exec(compile(ast.Module(body=[_FUNCTION], type_ignores=[]), str(_SOURCE), "exec"), _NAMESPACE)
_parse_duration = _NAMESPACE["_parse_duration"]


def test_parse_plain_seconds_for_backwards_compatibility():
    assert _parse_duration(["/seek", "60"]) == 60
    assert _parse_duration(["/seek", "0"]) == 0


def test_parse_single_units():
    assert _parse_duration(["/seek", "1s"]) == 1
    assert _parse_duration(["/seek", "1m"]) == 60
    assert _parse_duration(["/seek", "2h"]) == 7200


def test_parse_combined_duration_tokens():
    assert _parse_duration(["/seek", "2h", "1m", "4s"]) == 7264
    assert _parse_duration(["/seek", "1m", "30s"]) == 90
    assert _parse_duration(["/seek", "2H", "1M", "4S"]) == 7264


def test_reject_malformed_or_ambiguous_input():
    assert _parse_duration(["/seek"]) is None
    assert _parse_duration(["/seek", "1hour"]) is None
    assert _parse_duration(["/seek", "2hfoo"]) is None
    assert _parse_duration(["/seek", "1", "2"]) is None
    assert _parse_duration(["/seek", "1.5s"]) is None
