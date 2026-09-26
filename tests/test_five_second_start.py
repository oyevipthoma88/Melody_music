from pathlib import Path

ytdl = Path("melody/core/ytdl.py").read_text()
call = Path("melody/core/call.py").read_text()


def test_direct_resolve_budget_fits_five_second_start():
    assert 'RESOLVE_TIMEOUT", "3.0"' in ytdl
    assert 'DIRECT_RESOLVE_MAX", "3.5"' in ytdl


def test_slow_resolve_failure_is_not_retried():
    assert "RESOLVE_RETRY_IF_UNDER" in call
    assert "if _fast_fail and not force" in call
