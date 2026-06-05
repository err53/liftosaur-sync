# Upload Structured Strava Activities From Intervals HR And Liftosaur Sets

Liftosaur Sync will create structured Strava Activities for strength training by combining Liftosaur Workout set details with heart-rate streams from a Time Matched Intervals Activity. HealthFit may sync Apple Watch strength workouts into Intervals, but should not also sync those same workouts directly to Strava because Strava does not add structured strength-training details to existing Strava Activities through the normal activity update endpoint. The Strava upload should use Strava's limited JSON strength-training format before considering FIT generation.

## Considered Options

- Enrich HealthFit-created Strava Activities by updating descriptions only.
- Upload complete structured Strava Activities directly from Liftosaur data and locally exported HealthFit files.
- Upload complete structured Strava Activities from Liftosaur set details and Intervals HR streams.

## Consequences

- Strava can receive individual sets, repetitions, and weights instead of only description text.
- Intervals remains part of the workflow as the low-effort source for HR-bearing activity data.
- HealthFit-to-Strava sync should be disabled for these strength workouts to avoid duplicate Strava Activities.
- If an existing Strava Activity Time Matches a Liftosaur Workout, Structured Strava Upload is skipped by default rather than creating a duplicate, but Strava title and description metadata may be updated.
- Existing Strava Activity checks use the same core Time Match rules as Intervals while filtering to plausible strength-related Strava sport types.
- The sync must verify that a Time Matched Intervals Activity exposes enough HR stream data before uploading a structured Strava Activity.
- If Intervals HR streams are unavailable for a matched activity, the sync should skip and warn by default rather than uploading a no-HR Strava Activity.
- Liftosaur exercise names must be mapped to Strava `exercise_type` identifiers through an explicit mapping table; a Structured Strava Upload is skipped unless every exercise with work sets has a mapping.
- Structured Strava Uploads are invoked through an explicit Strava CLI verb rather than a default target, reducing accidental uploads.
- The Strava CLI verb performs Structured Strava Uploads for unmatched Liftosaur Workouts and metadata enrichment for existing Strava Time Matches.
- `--apply` is sufficient authorization to upload eligible Structured Strava Uploads once the explicit Strava CLI verb is selected.
- An `all` CLI verb may run both Intervals sync and Strava structured upload planning/apply logic when their gates pass.
- The `all` verb's Strava planning uses only pre-existing Intervals Activities with actual HR stream/content availability; Manual Fallback Activities created during the same run and tag-based checks do not satisfy the HR source gate.
- Intervals `has_heartrate` may be used as a cheap pre-filter, but the authoritative HR gate is successfully fetching a non-empty HR stream aligned with a time stream from the Time Matched Intervals Activity.
- Initial Strava JSON uploads include HR streams only; optional active/moving streams are deferred until their semantics for strength training are clear.
- Strava set objects omit `start_time` unless Liftosaur exposes actual set timestamps; the sync does not invent set timing from exercise order or workout duration.
- Structured Strava Uploads include Liftosaur work sets only; warmup and missed sets are excluded initially.
- Structured Strava Upload names use `<program> - <day name>` when both are available, falling back to the available value or `Strength Training`.
- Structured Strava Upload descriptions are concise provenance summaries and do not include raw Liftosaur data by default.
- Structured Strava Uploads use the same stable Liftosaur-derived external ID stem as Intervals Manual Fallback Activities, `liftosaur:<liftosaur_history_id>`; Strava may return the uploaded file extension as part of its stored external ID.
- Strava idempotency treats `liftosaur:<liftosaur_history_id>` and `liftosaur:<liftosaur_history_id>.json` as the same Liftosaur-derived external ID.
- Strava external ID matches take precedence over Strava Time Matches when deciding whether a Liftosaur Workout was already uploaded.
- Structured Strava Upload activity start and duration come from the Liftosaur Workout; the Time Matched Intervals Activity supplies HR samples only.
- Intervals HR stream timestamps are treated as elapsed seconds from the start of the Time Matched Intervals Activity, then normalized onto the Structured Strava Upload timeline without applying wall-clock start offset. The stream is clipped to the Liftosaur Workout duration before upload.
- Structured Strava Uploads require at least one clipped HR sample; partial HR coverage is allowed, with a warning when coverage is less than 50% of the Liftosaur Workout duration.
- `all --apply` applies Intervals writes before Strava uploads, but Strava eligibility is planned from pre-apply fetched Intervals HR content and does not depend on Intervals write outcomes from the same run.
- The `strava` verb reads Intervals HR content but does not mutate Intervals; Intervals mutations remain under the `intervals` verb or the composed `all` verb.

## Evidence

- `PUT /activities/18749281250` with a `sets` payload was accepted but returned the unchanged normal activity representation with no set, rep, or weight fields.
- `POST /uploads` with Strava's structured JSON strength format for the same May 30, 2026 activity time created a new Strava Activity, `18760231398`, instead of merging into existing Strava Activity `18749281250` or rejecting as a duplicate.
- The uploaded spike activity had structured-upload metadata but no heart-rate data because no HR stream was included in the JSON payload.
