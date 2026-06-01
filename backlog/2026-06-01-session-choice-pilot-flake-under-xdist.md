# Flaky Pilot test under `pytest -n auto`: SessionChoiceScreen dismiss-callback keyboard test

where: `tests/test_session_choice.py::SessionChoiceScreenTests::test_keyboard_works_when_pushed_from_dismiss_callback`

why: Under `pytest tests/ -n auto` (xdist, parallel CPU contention) the Textual message pump for `SessionChoiceScreen` occasionally takes >0.1s (observed 0.11s–0.50s), emitting an asyncio `WARNING Executing <Task ...> took N seconds`. The canonical local check treats warnings in the run as failures (see `docs/agents/code-map.md` § TUI test runtime policy), so the test reports FAILED. It passes deterministically in isolation and serially (`-p no:xdist` → 10/10). Observed on the clean baseline (pre-refactor) and on every parallel run since, so it is pre-existing and not caused by the src-layering refactor.

done when: a full `pytest tests/ -n auto` run is reliably green without needing a serial re-confirm of this nodeid. Candidate fixes: (a) raise/relax the slow-callback warning threshold for this Pilot scenario, (b) mark the test to tolerate the message-pump-timing warning under load, or (c) reduce its message-pump work so it stays under the threshold even under xdist contention.

Non-corrupting: pure test-harness timing flake, no product impact. Current mitigation during the layering refactor: each phase's green gate re-runs any failing Pilot nodeid serially to distinguish this flake from a real regression.
