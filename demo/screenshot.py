#!/usr/bin/env python3
"""Export the actual dashboard as SVG using only synthetic scenario data."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from render import main as render_scenario  # noqa: E402

from uxon import __version__  # noqa: E402
from uxon.domain.agents import default_agent_catalog  # noqa: E402
from uxon.domain.session import to_tui_session  # noqa: E402
from uxon.domain.status import ServerStatus  # noqa: E402
from uxon.domain.sudo import SudoCapability  # noqa: E402
from uxon.infra.agents import AgentAvailability  # noqa: E402
from uxon.infra.demo import (  # noqa: E402
    load_demo_local_sessions,
    load_demo_snapshot,
    synthesize_remote_hosts,
)
from uxon.tui.app import UxonApp  # noqa: E402
from uxon.tui.context import LaunchProfileOption, TuiContext  # noqa: E402
from uxon.tui.messages import _RefreshSourceLanded  # noqa: E402
from uxon.tui.widgets.session_list_view import SessionListView  # noqa: E402


async def export(scenario: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="uxon-screenshot-") as directory:
        data = Path(directory)
        render_scenario([str(scenario), str(data)])
        hosts = synthesize_remote_hosts(data)
        snapshots = {host.name: load_demo_snapshot(host.name, data, time.time()) for host in hosts}
        for snapshot in snapshots.values():
            if snapshot.error:
                raise ValueError(snapshot.error)
        # Explicit fictional identities keep host account/config data out of the image.
        local = load_demo_local_sessions(data, "alice-agent")
        catalog = default_agent_catalog()
        context = TuiContext(
            sessions=[to_tui_session(session, "uxon-") for session in local],
            total_cpu="28.4",
            total_ram="880 MiB",
            version=__version__,
            cwd="/srv/projects/alice/billing-api",
            cwd_short="billing-api",
            new_project_root="/srv/projects/shared",
            existing_projects=[],
            server_status=ServerStatus(cpu="28%", ram="8 / 32 GiB", uptime="2d"),
            current_user="alice",
            launch_user="alice-agent",
            cwd_writable=True,
            sudo_caps=SudoCapability(frozenset({"alice-agent", "bob-agent"})),
            remote_hosts=hosts,
            remote_snapshots=snapshots,
            tui_table_columns=("host", "user", "name", "agent", "cpu", "ram", "last"),
            agents=catalog,
            enabled_profiles=tuple(catalog),
            default_profile="claude",
            launch_profiles={
                name: LaunchProfileOption(name, name, name, "alice-agent") for name in catalog
            },
            agent_availability={name: AgentAvailability("ok") for name in catalog},
            refresh_sources=[],
            tui_render_debounce_ms=0,
        )
        app = UxonApp(context, probe_agents=False)
        async with app.run_test(size=(120, 26)) as pilot:
            app.post_message(_RefreshSourceLanded("main_ctx_rebuild", context))
            for name, snapshot in snapshots.items():
                app.post_message(_RefreshSourceLanded(f"remote:{name}", snapshot))
            expected_rows = len(local) + sum(
                len(snapshot.sessions) for snapshot in snapshots.values()
            )
            async with asyncio.timeout(5):
                while app.screen.query_one(SessionListView).row_count != expected_rows:
                    await pilot.pause(0.02)
            await pilot.pause()
            output.parent.mkdir(parents=True, exist_ok=True)
            app.save_screenshot(filename=output.name, path=str(output.parent))
            output.write_text(
                "\n".join(line.rstrip() for line in output.read_text().splitlines()) + "\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    asyncio.run(export(args.scenario, args.output))


if __name__ == "__main__":
    main()
