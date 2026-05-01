"""Allow ``python -m uxon`` to invoke the CLI entrypoint.

The console-script generated from ``[project.scripts]`` calls
``uxon.cli:main`` directly; this module exists so the same entrypoint
works under ``python -m uxon`` (used by the pty test harness and by
in-tree development without re-installing the script).
"""

from uxon.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
