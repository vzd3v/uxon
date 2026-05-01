#!/usr/bin/env python3
"""Install ``uxon`` system-wide on a shared host.

Lead behaviour (post-restructure): create a dedicated venv at
``--venv-dir`` (default ``/opt/uxon/venv``), ``pip install`` the
checkout into it, then symlink the generated console script to
``--install-path``. This preserves the multi-host symlink rollout
documented in ``docs/deployment.md`` while picking up dependencies
(textual, tomlkit) automatically — no system packages required.

Compatibility: the previous CLI accepted ``--repo-dir`` and
``--install-path``; both are still supported. The old behaviour was a
plain symlink from ``<repo-dir>/bin/uxon`` to ``--install-path``; that
path no longer exists, so this script now installs the package itself.

Run as root (or via ``sudo``) when ``--install-path`` is under
``/usr/local/bin``. The script honours ``--dry-run`` for safe preview.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def fail(msg: str) -> int:
    print(f"install_uxon.py: {msg}", file=sys.stderr)
    return 2


def run(cmd: list[str], *, dry_run: bool) -> int:
    print("+ " + " ".join(cmd))
    if dry_run:
        return 0
    cp = subprocess.run(cmd)
    return cp.returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        required=True,
        help="path to the uxon repo checkout (the directory containing pyproject.toml)",
    )
    parser.add_argument(
        "--install-path",
        required=True,
        help="symlink target, typically /usr/local/bin/uxon",
    )
    parser.add_argument(
        "--venv-dir",
        default="/opt/uxon/venv",
        help="dedicated venv to install uxon into (default: /opt/uxon/venv)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter to use when bootstrapping the venv (default: current)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the steps without executing",
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="force a reinstall (`pip install --force-reinstall`)",
    )
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    if not (repo_dir / "pyproject.toml").exists():
        return fail(f"--repo-dir does not contain pyproject.toml: {repo_dir}")
    if not (repo_dir / "src" / "uxon").is_dir():
        return fail(f"--repo-dir does not contain src/uxon/: {repo_dir}")

    venv_dir = Path(args.venv_dir).resolve()
    install_path = Path(args.install_path)
    venv_python = venv_dir / "bin" / "python"
    venv_uxon = venv_dir / "bin" / "uxon"

    # 1. Create venv if missing.
    if not venv_python.exists():
        rc = run([args.python, "-m", "venv", str(venv_dir)], dry_run=args.dry_run)
        if rc != 0:
            return fail(f"failed to create venv at {venv_dir} (rc={rc})")
    else:
        print(f"# venv already exists at {venv_dir}")

    # 2. Upgrade pip (best-effort) and install the package.
    pip_cmd = [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"]
    run(pip_cmd, dry_run=args.dry_run)

    install_cmd = [str(venv_python), "-m", "pip", "install"]
    if args.reinstall:
        install_cmd.append("--force-reinstall")
    install_cmd.append(str(repo_dir))
    rc = run(install_cmd, dry_run=args.dry_run)
    if rc != 0:
        return fail(f"pip install failed (rc={rc})")

    # 3. Symlink the venv-generated console script to --install-path.
    if not args.dry_run and not venv_uxon.exists():
        return fail(
            f"expected console script at {venv_uxon} after install, not found "
            f"(check the venv: {venv_dir})"
        )

    install_path.parent.mkdir(parents=True, exist_ok=True)
    if install_path.is_symlink() or install_path.exists():
        if args.dry_run:
            print(f"+ rm {install_path}")
        else:
            install_path.unlink()
    if args.dry_run:
        print(f"+ ln -s {venv_uxon} {install_path}")
    else:
        os.symlink(venv_uxon, install_path)

    # 4. Sanity probe.
    which = shutil.which("uxon")
    print(f"# done. uxon → {install_path} → {venv_uxon}")
    if which is None:
        print(
            "# note: 'uxon' not found on PATH yet — confirm "
            f"{install_path.parent} is in PATH for the target users.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
