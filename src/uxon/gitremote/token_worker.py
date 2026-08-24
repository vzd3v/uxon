# SPDX-License-Identifier: MIT
"""Token-file and HTTP worker executed wholly inside an execution backend."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from uxon.domain.git_profiles import GitRemoteProfile
from uxon.gitremote.backend_gh import BackendError
from uxon.gitremote.backend_token import _create_with_token, _preflight_with_token, default_http


def _profile(raw: Any) -> GitRemoteProfile:
    if not isinstance(raw, dict) or set(raw) != {
        "name",
        "host",
        "owner",
        "auth",
        "creds_user",
        "token_file",
        "visibility",
    }:
        raise ValueError("invalid profile payload")
    return GitRemoteProfile(**{key: str(value) for key, value in raw.items()})


def execute(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != {"operation", "profile", "repo_name"}:
        raise ValueError("invalid worker request")
    operation = request["operation"]
    repo_name = request["repo_name"]
    if operation not in {"preflight", "create"} or not isinstance(repo_name, str) or not repo_name:
        raise ValueError("invalid worker operation")
    profile = _profile(request["profile"])
    token = Path(profile.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise BackendError("token file is empty", stage="preflight")
    try:
        if operation == "preflight":
            _preflight_with_token(profile, repo_name, token, http=default_http)
            return {"ok": True, "ssh_url": ""}
        return {
            "ok": True,
            "ssh_url": _create_with_token(profile, repo_name, token, http=default_http),
        }
    except Exception as exc:
        message = str(exc).replace(token, "***")
        stage = exc.stage if isinstance(exc, BackendError) else operation
        return {"ok": False, "stage": stage, "error": message}
    finally:
        del token


def main() -> int:
    try:
        request = json.load(sys.stdin)
        response = execute(request)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        response = {"ok": False, "stage": "worker", "error": str(exc)}
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
