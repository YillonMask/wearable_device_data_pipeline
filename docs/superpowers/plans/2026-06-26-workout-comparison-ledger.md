# Workout Comparison — Progress Ledger

Plan: `docs/superpowers/plans/2026-06-26-workout-comparison.md`
Repo is NOT git — "commit" steps are full-suite checkpoints (`uv run pytest`).

- [x] Task 1: workouts table migration — complete, review clean (72 passed)
- [x] Task 2: Workout model + WorkoutFetchResult + upsert_workout — complete, review clean (73 passed; test uses tuple(row) for sqlite3.Row)
- [x] Task 3: WhoopClient.fetch_workouts — complete, review clean (74 passed)
- [x] Task 4: match_workouts pure function — complete, review clean (78 passed, 4/4 new)
- [x] Task 5: GoogleHealthClient.fetch_workouts — complete, review clean (80 passed). Probe resolved: data type = `exercise`; max_hr not exposed (None).
- [x] Task 6: orchestrator pull_workouts + CLI --workouts — complete, review clean (79 passed, 17/17 orchestrator)
- [x] Task 7: viz workout comparison section — complete, review clean (89 passed w/ analysis+viz extras)

All 7 tasks complete. Full suite: 89 passed (with --extra analysis --extra viz). The 9 "failures" under the bare dev env are purely pandas-not-installed, pre-existing.

Final review: APPROVED. Finding #1 (Whoop nextToken/next_token asymmetry) verified live as a FALSE POSITIVE — code is correct (request param `nextToken`, response key `next_token`). Fixed Minor #4 (viz start shown UTC → now local).

LIVE END-TO-END SMOKE caught a real bug not visible in unit tests: the CLI rebuilt clients from stale `settings` for the workouts pass, double-consuming Whoop's rotating refresh token → 400 invalid_grant. Fixed in cli.py:pull by building `enabled_clients` once and sharing `clients=entries` across pull_one_day + pull_workouts. Documented in CLAUDE.md. Re-ran live: whoop 10 workouts, google_health 5 workouts, 15 total. Matching produces 5 cross-device pairs.
