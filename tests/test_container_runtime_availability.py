"""A Compose client is usable only when its provider can query the engine."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def test_runtime_probe_requires_provider_api_not_just_client_metadata(tmp_path, monkeypatch):
    binary = tmp_path / "engine"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        "if sys.argv[1:] in (['info'], ['compose', 'version']):\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1] == 'compose' and 'ps' in sys.argv:\n"
        "    sys.stdin.read()\n"
        "    raise SystemExit(0 if pathlib.Path(__file__).with_name('api-ready').exists() else 1)\n"
        "raise SystemExit(2)\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", os.defpath))
    fixture_path = Path(__file__).parent / "integration/runtime/conftest.py"
    probe = runpy.run_path(str(fixture_path))["_runtime_usable"]
    assert not probe("engine")
    (tmp_path / "api-ready").touch()
    assert probe("engine")
