# Treat Intervals Activity Times As Local-Time Driven

Intervals Activity matching and Manual Fallback Activity creation use Intervals local wall time, not UTC request bounds, because the Intervals API lists activities by local `oldest` and `newest` values and requires `start_date_local` when creating manual activities. Liftosaur Workout timestamps are parsed as instants when they include `Z` or an offset, then converted through the configured sync timezone before querying or writing Intervals data.

## Consequences

- The sync requires a configured timezone that matches the Intervals athlete timezone.
- Intervals activity queries use local date-time bounds, padded around the requested sync window.
- Manual Fallback Activities are created with `start_date_local`, `elapsed_time`, and `moving_time`; `start_date` and `timezone` are not sent because Intervals recomputes or ignores them.
- Time Match comparisons can still use absolute instants internally after Intervals local times are interpreted in the configured timezone.

## Evidence

- A manual activity created with `start_date_local=2026-06-04T09:13:00` for an `America/Toronto` athlete returned `start_date=2026-06-04T13:13:00Z`.
- Creating a manual activity with only `start_date` returned `422 Missing start_date_local`.
- When `start_date` conflicted with `start_date_local`, Intervals used `start_date_local` and recomputed `start_date`.
- A narrow local-time activity list query found the created activity, while a narrow UTC-like query for the returned `start_date` did not.
