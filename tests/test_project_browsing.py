"""Project browsing stays inside the selected execution filesystem."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from helpers import make_config

from uxon.app.project_browsing import browse_project_directories
from uxon.domain.authz import project_directory
from uxon.infra.identity import process_user


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../demo",
        "demo/../other",
        "demo/./other",
        "/demo",
        "demo//other",
        "demo/",
        "demo\x00",
    ],
)
def test_project_path_refuses_traversal_and_absolute_names(path: str) -> None:
    with pytest.raises(ValueError, match="invalid project path"):
        project_directory("/srv/projects", path)


def test_nested_listing_and_symlink_boundary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "projects"
        nested = root / "group" / "project space"
        nested.mkdir(parents=True)
        (nested / "zebra").mkdir()
        (nested / "[alpha]").mkdir()
        (nested / ".hidden").mkdir()
        (nested / "file.txt").write_text("text")
        (nested / "link").symlink_to(nested / "zebra", target_is_directory=True)
        cfg = make_config(new_project_root=str(root), allowed_roots=[str(root)])
        rows = browse_project_directories(cfg, process_user(), "group/project space")
        assert [name for name, _ in rows] == ["[alpha]", "zebra"]
        assert all(mtime for _, mtime in rows)
        assert browse_project_directories(cfg, process_user(), "group/project space/zebra") == []

        outside = Path(tmp) / "outside"
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(SystemExit, match="2") as caught:
            browse_project_directories(cfg, process_user(), "escape")
        assert caught.value.uxon_msg == "project directory is outside new_project_root"
        with pytest.raises(SystemExit):
            browse_project_directories(cfg, process_user(), "group/missing")
