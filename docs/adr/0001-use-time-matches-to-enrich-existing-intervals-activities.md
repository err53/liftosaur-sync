# Use Time Matches To Enrich Existing Intervals Activities

Liftosaur Workouts should enrich existing Intervals Activities when their times indicate the same training session, because device- or health-platform-created Intervals Activities may already contain the heart-rate data Liftosaur does not provide. Manual Fallback Activities are created only when no Time Match exists, avoiding duplicate activities and double-counted training records. Enrichment is idempotent through managed description blocks on matched activities, while Manual Fallback Activities are idempotent through Liftosaur-derived external IDs.

On reruns, an existing managed description block for a Liftosaur Workout ID takes precedence over Time Match. This keeps previously enriched Intervals Activities stable even if later imports create new activities with similar times.

## Considered Options

- Always create a new manual Intervals Activity for each Liftosaur Workout.
- Match by source or activity name.
- Match by time and enrich the existing Intervals Activity when possible.

## Consequences

- Existing HR-bearing Intervals Activities remain the preferred record for a training session.
- The sync must handle ambiguous Time Matches conservatively and warn instead of guessing.
- Manual Fallback Activities are explicitly secondary and may later be superseded by a Time Matched activity.
- Managed description blocks are plain text because Intervals does not render HTML in activity descriptions.
