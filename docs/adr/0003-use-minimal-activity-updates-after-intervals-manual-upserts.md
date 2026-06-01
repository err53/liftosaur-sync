# Use Minimal Activity Updates After Intervals Manual Upserts

Manual Fallback Activities are created or found with Intervals' manual bulk upsert endpoint using a Liftosaur-derived `external_id`, then updated with minimal `PUT /activity/{id}` requests for fields that must be guaranteed. Matched Intervals Activities are enriched only through minimal `PUT` requests so existing source-owned activity data is not rewritten.

## Consequences

- Manual Fallback Activity writes may use two API calls: bulk upsert for idempotency, then partial update for guaranteed enrichment fields.
- Description, tags, `kg_lifted`, `elapsed_time`, and `moving_time` are written through minimal updates when they need to be guaranteed.
- The sync does not send full returned Activity objects back to Intervals.
- Empty descriptions and tag lists are written as `""` and `[]`; `null` is not used to clear those fields.

## Evidence

- `PUT /activity/{id}` accepted partial updates for `description`, `kg_lifted`, and `tags` while preserving omitted activity fields.
- Sending a full returned Activity object back to `PUT /activity/{id}` failed with `422`.
- Tags sent on single manual create were still absent after delayed reads.
- Manual bulk upsert by `external_id` returned the same activity ID on repeated calls and updated name, description, `kg_lifted`, and tags.
- Manual bulk upsert did not update `elapsed_time` or `moving_time` on an existing activity after delayed reads.
- A real non-manual `OAUTH_CLIENT` activity created by Intervals Companion accepted partial updates to description and tags without changing HR data, timing, name, source, or external ID.
- Sending `null` for description and tags did not clear a real activity; sending `""` and `[]` did.
