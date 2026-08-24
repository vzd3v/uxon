# SPDX-License-Identifier: MIT
"""Render a validated operator config without loading the installed config."""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

from uxon.domain.args import ParsedArgs
from uxon.errors import fail
from uxon.infra import audit
from uxon.infra.config_renderer import render_config


def _write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def do_config_render(args: ParsedArgs) -> int:
    """Render strict JSON to TOML; audit modes, never config content or paths."""
    source = args.config_json
    destination = args.output or "-"
    if source is None:
        fail("config render requires --config-json <path|->")
    try:
        if source == "-":
            payload = json.load(sys.stdin)
        else:
            with Path(source).open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        if not isinstance(payload, dict):
            raise ValueError("config payload must be a JSON object")
        rendered = render_config(payload)
        if destination == "-":
            sys.stdout.write(rendered)
        else:
            _write_output(Path(destination), rendered)
    except (OSError, ValueError) as exc:
        audit.audit(
            "config.render",
            outcome="error",
            input="stdin" if source == "-" else "file",
            output="stdout" if destination == "-" else "file",
            error_type=exc.__class__.__name__,
        )
        fail(f"config render failed: {exc}")
    audit.audit(
        "config.render",
        outcome="ok",
        input="stdin" if source == "-" else "file",
        output="stdout" if destination == "-" else "file",
    )
    return 0
