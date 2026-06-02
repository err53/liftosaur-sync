# Liftosaur Sync

Sync Liftosaur workout history to supported targets. The current target is Intervals.icu.

## Configuration

Create `.env` from `.env.example`:

```sh
LIFTOSAUR_API_KEY=
INTERVALS_API_KEY=
INTERVALS_ATHLETE_ID=i123456
SYNC_TIMEZONE=America/New_York
```

`INTERVALS_ATHLETE_ID` must be the real athlete ID, not `i`, when using API-key auth.

## Dry Run

```sh
uv run python main.py --days 14
uv run python main.py --days 14 --json
uv run liftosaur-sync --days 14
```

## Apply

```sh
uv run python main.py --days 14 --apply
uv run liftosaur-sync --days 14 --apply
```

`--apply` enriches Time Matched Intervals Activities with a plain-text managed Liftosaur description block, `kg_lifted` when computed, and the `liftosaur` tag. On later runs, the managed block's Liftosaur history ID is used before Time Match so the same Intervals Activity is updated again. It creates or updates Manual Fallback Activities only when no eligible Time Match exists.

## Tests

```sh
uv run python -m unittest discover -s tests
```
