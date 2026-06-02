# PTY integration test flakes under load: left-click on second action row

where: `tests/test_tui_integration.py::PtyTuiIntegrationTests::test_left_click_on_second_action_row_selects_it`

why: The test forks a PTY child, feeds an SGR-1006 mouse press+release
(`\x1b[<0;5;4M` / `\x1b[<0;5;4m`) then `q`, and asserts the last frame still
shows `"Create new project"`. Under CPU contention (`-n auto`, or a serial run
while other heavy work competes for the host) the captured trace sometimes
contains only the echoed input bytes (`^[[<0;5;4M^[[<0;5;4mq`) with no rendered
TUI frame — the click-driven render misses its drain window — and the assertion
fails. **In isolation it passes**: the single test and the whole
`PtyTuiIntegrationTests` class both pass serially (`-p no:xdist`) on a quiet
host, on current HEAD and on the clean baseline `91e3aa0`. So this is a
load/timing flake, NOT a deterministic failure and NOT a product bug.

pre-existing: present on `91e3aa0` (Part 1 tip, before any Part 2 work). NOT
caused by Phase P1 (which only moved `host_breaker` + `git_profiles` into
`domain/` and repointed imports — no TUI render-path change). Same *category* as
the two flakes in `2026-06-01-session-choice-pilot-flake-under-xdist.md` (slow
message pump / render under parallel contention), just on the mouse-click path
rather than the keyboard path.

done when: folded into the Phase P9 flaky-fix work (§J of
`docs/agents/superpowers/plans/2026-06-02-src-layering-part2.md`) — fix at the
source (reduce on-mount/message-pump work or widen the click-path drain window
so it stays under threshold under load), then verify 3× consecutive full
`-n auto` runs green and remove this file. No `xfail`/`skip`/`@flaky`
(criterion #4/#11).

Non-corrupting: test-harness/render-timing issue in a PTY integration test, no
product impact. Surfaced during Phase P1; deferred to P9 (TUI flaky-fix phase).
