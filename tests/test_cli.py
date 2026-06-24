import subprocess
import sys
from pathlib import Path


def test_main_help():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "main.py", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "run" in proc.stdout
