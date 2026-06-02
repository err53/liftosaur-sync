# Liftosaur Sync

Sync Liftosaur workout history to supported targets. Supported targets include Intervals.icu and Strava.

## Configuration

Create `.env` from `.env.example`:

```sh
LIFTOSAUR_API_KEY=
INTERVALS_API_KEY=
INTERVALS_ATHLETE_ID=i123456
SYNC_TIMEZONE=America/New_York
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
STRAVA_REFRESH_TOKEN=
```

`INTERVALS_ATHLETE_ID` must be the real athlete ID, not `i`, when using API-key auth.

For first-run Strava authorization, set `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`, then run:

```sh
uv run liftosaur-sync strava-auth
```

Authorize the app with `activity:read_all` and `activity:write`, then paste the redirected URL or `code` value when prompted. The command saves `STRAVA_REFRESH_TOKEN` to `.env`.

## Dry Run

```sh
uv run liftosaur-sync intervals --days 14
uv run liftosaur-sync intervals --days 14 --json
uv run liftosaur-sync strava --days 14
uv run liftosaur-sync all --days 14
```

## Apply

```sh
uv run liftosaur-sync intervals --days 14 --apply
uv run liftosaur-sync strava --days 14 --apply
uv run liftosaur-sync all --days 14 --apply
```

`intervals --apply` enriches Time Matched Intervals Activities with a plain-text managed Liftosaur description block, `kg_lifted` when computed, and the `liftosaur` tag. On later runs, the managed block's Liftosaur history ID is used before Time Match so the same Intervals Activity is updated again. It creates or updates Manual Fallback Activities only when no eligible Time Match exists.

`strava --apply` uploads structured Strava Activities using Liftosaur work sets and HR streams from pre-existing Time Matched Intervals Activities. Existing Strava Time Matches block upload to avoid duplicates. Disable HealthFit-to-Strava strength sync before using this mode.

`all --apply` runs Intervals writes first, then Strava uploads using HR content fetched before Intervals writes.

## Tests

```sh
uv run python -m unittest discover -s tests
```
