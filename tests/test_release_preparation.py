"""Publication inputs fail closed before any distribution or notes are uploaded."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/prepare_release.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, timeout=5)


def _artifacts(root: Path, wheel_version: str, sdist_version: str) -> None:
    with zipfile.ZipFile(root / "dist/uxon.whl", "w") as archive:
        archive.writestr("uxon.dist-info/METADATA", f"Name: uxon\nVersion: {wheel_version}\n\n")
    data = f"Name: uxon\nVersion: {sdist_version}\n\n".encode()
    with tarfile.open(root / "dist/uxon.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("uxon/PKG-INFO")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


def _tree(root: Path, version: str = "4.0.0") -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Alice")
    _git(root, "config", "user.email", "alice@example.test")
    _git(root, "commit", "--allow-empty", "-qm", "Initial state")
    _git(root, "update-ref", "refs/tags/v3.6.0", "HEAD")
    (root / "src/uxon").mkdir(parents=True)
    (root / "src/uxon/__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "CHANGELOG.md").write_text(
        f"## [{version}] — 2026-10-29\n\n### Fixed\n- Fixed launch.\n"
        "- Read [migration notes](docs/migrations.md#session-authority), "
        "[CLI](./docs/reference/cli.md) and [upstream](https://example.test/guide#go).\n"
    )
    _git(root, "add", "src", "CHANGELOG.md")
    _git(root, "commit", "-qm", "Prepare release")
    # Disposable local refs only; the source checkout and all remotes are untouched.
    _git(root, "update-ref", f"refs/tags/v{version}", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    (root / "dist").mkdir()
    _artifacts(root, version, version)


def _prepare(root: Path, tag: str = "v4.0.0", *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), tag, "example/uxon", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("version", ["4.0.0", "4.0.0rc1"])
def test_release_preparation_reuses_exact_curated_section(tmp_path: Path, version: str) -> None:
    _tree(tmp_path, version)
    result = _prepare(tmp_path, f"v{version}")
    assert result.returncode == 0, result.stderr
    notes = (tmp_path / "release/release-notes.md").read_text()
    assert "- Fixed launch." in notes
    assert notes.index("## What changed") < notes.index("## Install / upgrade")
    assert f"uxon=={version}" in notes
    assert f"v3.6.0...v{version}" in notes
    assert (
        f"[migration notes](https://github.com/example/uxon/blob/v{version}/"
        "docs/migrations.md#session-authority)"
    ) in notes
    assert f"[CLI](https://github.com/example/uxon/blob/v{version}/docs/reference/cli.md)" in notes
    assert "[upstream](https://example.test/guide#go)" in notes
    assert set(path.suffix for path in (tmp_path / "dist").iterdir()) == {".whl", ".gz"}


@pytest.mark.parametrize(
    ("defect", "reason"),
    [
        ("tag-version", "source version"),
        ("tag-target", "checked-out commit"),
        ("unmerged", "ancestor of origin/main"),
        ("missing-entry", "nonempty"),
        ("lookalike-entry", "nonempty"),
        ("empty-entry", "nonempty"),
        ("duplicate-entry", "nonempty"),
        ("wheel-version", "expected Name"),
        ("sdist-version", "expected Name"),
        ("extra-artifact", "exactly one wheel"),
        ("notes-in-dist", "outside"),
    ],
)
def test_invalid_release_cannot_produce_publishable_notes(
    tmp_path: Path, defect: str, reason: str
) -> None:
    _tree(tmp_path)
    tag, args = "v4.0.0", []
    if defect == "tag-version":
        tag = "v4.0.1"
    elif defect == "tag-target":
        _git(tmp_path, "update-ref", "refs/tags/v4.0.0", "HEAD^")
    elif defect == "unmerged":
        _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD^")
    elif defect.endswith("entry"):
        section = (tmp_path / "CHANGELOG.md").read_text()
        section = {
            "missing-entry": "## [Unreleased]\n- Fixed launch.\n",
            "lookalike-entry": section.replace("[4.0.0]", "[4x0x0]"),
            "empty-entry": "## [4.0.0]\n### Fixed\n- \n",
            "duplicate-entry": section + section,
        }[defect]
        (tmp_path / "CHANGELOG.md").write_text(section)
    elif defect in {"wheel-version", "sdist-version"}:
        _artifacts(
            tmp_path,
            "4.0.1" if defect == "wheel-version" else "4.0.0",
            "4.0.1" if defect == "sdist-version" else "4.0.0",
        )
    elif defect == "extra-artifact":
        (tmp_path / "dist/notes.txt").write_text("Unexpected payload")
    elif defect == "notes-in-dist":
        args = ["--notes", "dist/release-notes.md"]
    result = _prepare(tmp_path, tag, *args)
    assert result.returncode != 0
    assert reason in result.stderr
    assert not (tmp_path / "release/release-notes.md").exists()
    assert not (tmp_path / "dist/release-notes.md").exists()
