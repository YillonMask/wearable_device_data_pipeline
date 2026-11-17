#!/bin/sh
# Daily morning pull, run by launchd (see CLAUDE.md "Daily automation").
#   --date today      last night's sleep (devices label a sleep by its wake-up date)
#   --date yesterday  yesterday's finalized activity (steps, calories)
# Each pull also syncs Morning Log phone scores into self_report.
# Whoop only: the personal-recovery experiment (started 2026-11-08) uses Whoop
# and the owner's feelings, not Fitbit or Oura. Drop the --device flags to pull
# every configured device again.
# Runs from the repo root so .env and data/wearable.db resolve.
cd "$(dirname "$0")/.." || exit 1
UV="${UV:-$HOME/.local/bin/uv}"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') morning pull"
"$UV" run wearable pull --date today --device whoop
"$UV" run wearable pull --date yesterday --device whoop
