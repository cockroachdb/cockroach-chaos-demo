"""Runs the booth recorder's Node unit tests (tools/booth). It skips when the recorder or Node is absent."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_booth_recorder_timing_caption_and_camera_math():
    tests = sorted((ROOT / "tools" / "booth" / "test").glob("*.test.mjs"))
    node = shutil.which("node")
    if not tests or not node:
        return
    proc = subprocess.run([node, "--test", *map(str, tests)], capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
