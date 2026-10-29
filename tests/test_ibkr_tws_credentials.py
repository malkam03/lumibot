"""Credential import safety for optional IBKR TWS backtesting settings."""

import os
import subprocess
import sys
from pathlib import Path


def test_invalid_optional_client_id_does_not_break_credentials_import():
    env = os.environ.copy()
    env["IBKR_BACKTEST_CLIENT_ID"] = "not-an-integer"
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import lumibot.credentials"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "malformed optional client ID must not break package import"
