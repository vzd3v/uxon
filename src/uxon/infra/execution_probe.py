# SPDX-License-Identifier: MIT
"""Fixed target-user probe executed inside an execution backend."""

from __future__ import annotations

import json
import os


def main() -> int:
    egid = os.getegid()
    result = {"euid": os.geteuid(), "egid": egid, "groups": sorted({egid, *os.getgroups()})}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
