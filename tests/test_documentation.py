"""Executable public documentation contracts, without network access."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from uxon.domain.runtime import render_profile_template
from uxon.infra.config_loader import parse_config

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_GUIDE = ROOT / "docs/guides/customise/run-agents-in-a-container.md"
HARDEN_GUIDE = ROOT / "docs/guides/harden/harden-a-container.md"


def _blocks(path: Path, language: str) -> list[str]:
    return re.findall(rf"^```{language}\s*\n(.*?)^```\s*$", path.read_text(), re.M | re.S)


@pytest.mark.parametrize(
    "relative",
    [
        "config/config.example.toml",
        "config/config.example.toml#command-runtime",
        "docs/guides/customise/run-agents-in-a-container.md",
        "docs/guides/customise/configure-github-on-new-project.md",
        "docs/migrations.md",
    ],
)
def test_complete_documented_configurations_use_the_actual_parser(relative: str) -> None:
    filename, _, variant = relative.partition("#")
    path = ROOT / filename
    if variant == "command-runtime":
        text = path.read_text()
        section = text[text.index("# [launch.profiles.claude_container]") :]
        # Preserve narrative comments; remove one prefix from TOML declarations
        # and already nested comments in the maintained operator example.
        uncommented = "\n".join(
            line.removeprefix("# ") if re.match(r'# (?:\[|#|\w+\s*=|"[^"]+"\s*=)', line) else line
            for line in section.splitlines()
        )
        examples = [
            '[launch]\nenabled_profiles = ["claude_container"]\n'
            'default_profile = "claude_container"\n' + uncommented
        ]
    else:
        examples = [path.read_text()] if path.suffix == ".toml" else _blocks(path, "toml")
    assert examples, f"no maintained configuration examples in {relative}"
    for example in examples:
        cfg = parse_config(tomllib.loads(example))
        for runtime in cfg.runtimes.values():
            if runtime.identity_command:
                rendered = render_profile_template(
                    runtime.identity_command,
                    profile=runtime,
                    what="identity_command",
                    resource="uxon-example",
                    runtime_dir="/work/example",
                    user="alice",
                    launch_profile="claude_workbox",
                    agent="claude",
                    project_slug="example",
                )
                assert (
                    '{"id":"{{.Id}}","host_pid":{{.State.Pid}},"epoch":"{{.State.StartedAt}}"}'
                    in rendered
                )
            if runtime.stop_command:
                pidfile = "/tmp/uxon-" + "a" * 32 + ".pid"
                stop = render_profile_template(
                    runtime.stop_command,
                    profile=runtime,
                    what="stop_command",
                    resource="uxon-example",
                    runtime_dir="/work/example",
                    user="alice",
                    launch_profile="claude_workbox",
                    agent="claude",
                    project_slug="example",
                    pidfile=pidfile,
                )
                assert stop[3:6] == ["python3", "/usr/local/libexec/uxon-runtime-stop.py", pidfile]
                assert float(stop[-1]) < runtime.stop_timeout_seconds


def _anchors(content: str) -> set[str]:
    content = re.sub(r"^```.*?^```\s*$", "", content, flags=re.M | re.S)
    anchors, counts = set(), {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", content, re.M):
        heading = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", heading)
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(slug + (f"-{count}" if count else ""))
    anchors.update(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)', content))
    return anchors


def test_public_relative_markdown_links_and_anchors_exist() -> None:
    pages = [
        ROOT / name for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md")
    ]
    pages += [
        p
        for p in (ROOT / "docs").rglob("*.md")
        if "agents" not in p.relative_to(ROOT / "docs").parts
    ]
    failures = []
    for page in pages:
        content = re.sub(r"^```.*?^```\s*$", "", page.read_text(), flags=re.M | re.S)
        for match in re.finditer(r"\]\(([^)]+)\)", content):
            href = match.group(1).split(' "', 1)[0].strip("<>")
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or href.startswith("/"):
                continue
            destination = (page.parent / unquote(parsed.path)).resolve() if parsed.path else page
            location = (
                f"{page.relative_to(ROOT)}:{content[: match.start()].count(chr(10)) + 1} → {href}"
            )
            if not destination.exists():
                failures.append(location)
            elif (
                parsed.fragment
                and destination.suffix == ".md"
                and unquote(parsed.fragment) not in _anchors(destination.read_text())
            ):
                failures.append(location)
    assert not failures, "broken relative links/anchors:\n" + "\n".join(failures)


@pytest.mark.slow
@pytest.mark.parametrize("guide", [CONTAINER_GUIDE, HARDEN_GUIDE], ids=["container", "harden"])
def test_documented_compose_renders_runtime_home_and_consistent_mounts(guide: Path) -> None:
    # Offline config parsing uses the client directly, not a daemon-access shim.
    docker = shutil.which("docker", path=os.defpath)
    if docker is None:
        pytest.skip("Docker Compose client unavailable")
    yaml = _blocks(guide, "yaml")
    assert len(yaml) == 1
    env = dict(
        os.environ,
        UXON_RESOURCE="uxon-example",
        UXON_PROJECT="/tmp/uxon-example",
        UXON_RUNTIME_DIR="/work/example",
        HOME="/controller-home-not-runtime",
    )
    result = subprocess.run(
        [docker, "compose", "-f", "-", "config", "--format", "json"],
        input=yaml[0],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    service = json.loads(result.stdout)["services"]["agent"]
    assert service["container_name"] == "uxon-example"
    assert service["working_dir"] == "/work/example"
    assert service["environment"]["HOME"] == "/tmp/uxon-home"
    assert "$HOME" in service["command"][-1]
    assert env["HOME"] not in service["command"][-1]
    assert any(
        v["source"] == env["UXON_PROJECT"] and v["target"] == env["UXON_RUNTIME_DIR"]
        for v in service["volumes"]
    )
