# PTY integration test fails deterministically: left-click on second action row

where: `tests/test_tui_integration.py::PtyTuiIntegrationTests::test_left_click_on_second_action_row_selects_it`

why: The test forks a PTY child, feeds an SGR-1006 mouse press+release
(`\x1b[<0;5;4M` / `\x1b[<0;5;4m`) then `q`, and asserts the last frame still
shows `"Create new project"`. In this environment the captured trace contains
only the echoed input bytes (`^[[<0;5;4M^[[<0;5;4mq`) with no rendered TUI frame
at all — so the assertion fails. The 7 sibling tests in the same
`PtyTuiIntegrationTests` class render and pass (`1 failed, 7 passed`), so it is
not a global PTY/TTY harness outage — it is specific to this mouse-click
scenario (likely the click sequence drives an early exit / blank frame, or the
drain windows are too short for the click-driven render under load).

pre-existing: Reproduced on the clean baseline commit `91e3aa0` (the Part 1 tip,
before any Part 2 work) — `1 failed, 7 passed` identically, both serially
(`-p no:xdist`) and under `-n auto`. NOT caused by the Phase P1 domain
consolidation (which only moved `host_breaker` + `git_profiles` into `domain/`
and repointed imports — no TUI render-path change). NOT one of the two sanctioned
flakes tracked in `2026-06-01-session-choice-pilot-flake-under-xdist.md` (those
flake under contention; this one fails deterministically in isolation).

done when: the test renders a real frame in this environment (or the PTY drain
windows / click handling are fixed so the SGR click path renders) and the assert
passes deterministically. Candidate investigation: compare the click path vs the
keyboard sibling tests' drain timing; check whether the click triggers an early
`q`-less exit before first paint.

Non-corrupting: test-harness/render-timing issue in a PTY integration test, no
product impact (the keyboard-driven and other mouse tests in the class pass).
Surfaced during Phase P1; deferred because it is outside P1's scope (TUI render
surface is owned by P5/P6/P7) and predates the entire Part 2 refactor.
