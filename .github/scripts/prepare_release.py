# SPDX-License-Identifier: MIT
"""Validate release inputs and write the notes before any package is published."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urljoin


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def changelog_entry(text: str, version: str) -> str:
    """Select one literal version heading, never a version-shaped regex."""
    entries: list[list[str]] = []
    selected: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## ["):
            selected = None
            heading, bracket, _ = line.partition("]")
            if bracket and heading == f"## [{version}":
                selected = []
                entries.append(selected)
        elif selected is not None:
            selected.append(line)
    if len(entries) != 1 or not any(
        line.startswith("- ") and line[2:].strip() for line in entries[0]
    ):
        raise ValueError(f"CHANGELOG.md must contain one nonempty '## [{version}]' section")
    return "\n".join(entries[0]).strip()


def verify_metadata(data: bytes, version: str, artifact: Path) -> None:
    metadata = BytesParser().parsebytes(data)
    if metadata.get_all("Name") != ["uxon"] or metadata.get_all("Version") != [version]:
        raise ValueError(
            f"{artifact}: expected Name: uxon and Version: {version}; "
            f"got Name: {metadata.get_all('Name')!r}, Version: {metadata.get_all('Version')!r}"
        )


def prepare(tag: str, repository: str, dist: Path, notes: Path) -> None:
    source = ast.parse(Path("src/uxon/__init__.py").read_text())
    versions = [
        ast.literal_eval(node.value)
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    if len(versions) != 1 or not isinstance(versions[0], str) or tag != f"v{versions[0]}":
        raise ValueError(f"tag {tag!r} must exactly match the source version prefixed with 'v'")
    version = versions[0]
    if git("rev-parse", f"refs/tags/{tag}^{{commit}}") != git("rev-parse", "HEAD"):
        raise ValueError(f"tag {tag!r} does not point to the checked-out commit")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main"]
    ).returncode:
        raise ValueError("the tagged commit must be an ancestor of origin/main")
    entry = changelog_entry(Path("CHANGELOG.md").read_text(), version)
    # Relative links work in CHANGELOG.md but need a source location in a release body.
    changelog_url = f"https://github.com/{repository}/blob/{tag}/CHANGELOG.md"
    entry = re.sub(
        r"(?<=\]\()[^)\s]+(?=\))",
        lambda match: urljoin(changelog_url, match[0]),
        entry,
    )

    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1 or set(dist.iterdir()) != {*wheels, *sdists}:
        raise ValueError(f"{dist}: expected exactly one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"{wheels[0]}: expected one package metadata file")
        verify_metadata(wheel.read(metadata_names[0]), version, wheels[0])
    with tarfile.open(sdists[0], "r:gz") as sdist:
        metadata_members = [
            member
            for member in sdist.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO") and member.isfile()
        ]
        if len(metadata_members) != 1:
            raise ValueError(f"{sdists[0]}: expected one top-level package metadata file")
        stream = sdist.extractfile(metadata_members[0])
        assert stream is not None
        verify_metadata(stream.read(), version, sdists[0])

    previous = subprocess.run(
        ["git", "describe", "--tags", "--match", "v*", "--abbrev=0", "HEAD^"],
        capture_output=True,
        text=True,
    )
    body = (
        f"## What changed\n\n{entry}\n\n"
        "## Install / upgrade\n\n```bash\n"
        f"uv tool install --upgrade 'uxon=={version}'\n"
        f"pipx install --force 'uxon=={version}'\n```\n\n"
        f"**Full changelog:** [CHANGELOG.md](https://github.com/{repository}/blob/{tag}/CHANGELOG.md)\n"
    )
    if previous.returncode == 0:
        prior_tag = previous.stdout.strip()
        body += f"\n**Compare:** [{prior_tag}...{tag}](https://github.com/{repository}/compare/{prior_tag}...{tag})\n"
    if notes.resolve().is_relative_to(dist.resolve()):
        raise ValueError("release notes must be stored outside the distribution directory")
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("repository")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--notes", type=Path, default=Path("release/release-notes.md"))
    args = parser.parse_args()
    try:
        prepare(args.tag, args.repository, args.dist, args.notes)
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as error:
        parser.exit(1, f"Release validation failed: {error}\n")


if __name__ == "__main__":
    main()
