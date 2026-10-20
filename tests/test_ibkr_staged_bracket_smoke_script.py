"""Keep the zero-network IBKR staged-bracket smoke harness runnable."""

import runpy
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ibkr_staged_bracket_smoke.py"


def test_staged_bracket_smoke_script_passes(capsys):
    module = runpy.run_path(str(SCRIPT))
    assert module["main"]() == 0
    output = capsys.readouterr().out
    assert output.count("PASS ") == 3
    assert "FAIL" not in output
