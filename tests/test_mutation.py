import unittest
from datetime import datetime

from liftosaur_sync import (
    InMemoryIntervalsAdapter,
    IntervalsActivity,
    LiftosaurWorkout,
    SyncAction,
    SyncPlan,
    apply_sync_plan,
    build_enrich_update,
    build_fallback_upsert,
    build_fallback_update,
)


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MutationPayloadTests(unittest.TestCase):
    def test_builds_minimal_enrich_update(self):
        workout = LiftosaurWorkout(
            id="123",
            start=instant("2026-05-26T21:07:57Z"),
            duration_seconds=3894,
            text="raw liftosaur text",
            summary="Squat\nWork: 3x5 @ 100 lb",
            kg_lifted=680.388555,
            program="GZCLP",
            day_name="Day 1",
        )
        activity = IntervalsActivity(
            id="i1",
            type="WeightTraining",
            start=instant("2026-05-26T21:10:45Z"),
            duration_seconds=3884,
            has_heartrate=True,
            description="Existing notes",
            tags=["keep"],
        )

        update = build_enrich_update(workout, activity)

        self.assertEqual(set(update), {"description", "kg_lifted", "tags"})
        self.assertIn("Existing notes", update["description"])
        self.assertIn("LIFTOSAUR-SYNC-START id=123", update["description"])
        self.assertIn("Squat", update["description"])
        self.assertEqual(update["kg_lifted"], 680.388555)
        self.assertEqual(update["tags"], ["keep", "liftosaur"])

    def test_apply_sync_plan_returns_outcomes_and_uses_adapter_seam(self):
        workouts = [
            LiftosaurWorkout("enrich", instant("2026-05-26T21:07:57Z"), 3894, "raw", "Summary", 10.0),
            LiftosaurWorkout("fallback", instant("2026-05-27T21:07:57Z"), 1200, "raw", "Summary", 20.0),
        ]
        activities = [
            IntervalsActivity("i-enrich", "WeightTraining", instant("2026-05-26T21:10:45Z"), 3884, True, None, None)
        ]
        plan = SyncPlan(
            actions=[
                SyncAction("enrich", "enrich", intervals_id="i-enrich"),
                SyncAction("fallback", "fallback"),
            ],
            warnings=[],
        )
        adapter = InMemoryIntervalsAdapter(activities, timezone_name="America/Toronto")

        outcomes = apply_sync_plan(plan, workouts, adapter)

        self.assertEqual([outcome.status for outcome in outcomes], ["enriched", "fallback_upserted"])
        self.assertEqual(outcomes[0].intervals_id, "i-enrich")
        self.assertEqual(outcomes[1].intervals_id, "manual-liftosaur:fallback")
        self.assertEqual(adapter.updated_activity_ids, ["i-enrich", "manual-liftosaur:fallback"])

    def test_apply_sync_plan_continues_after_adapter_failure(self):
        workouts = [
            LiftosaurWorkout("fail", instant("2026-05-26T21:07:57Z"), 3894, "raw", "Summary", 10.0),
            LiftosaurWorkout("ok", instant("2026-05-27T21:07:57Z"), 1200, "raw", "Summary", 20.0),
        ]
        activities = [
            IntervalsActivity("i-fail", "WeightTraining", instant("2026-05-26T21:10:45Z"), 3884, True, None, None),
            IntervalsActivity("i-ok", "WeightTraining", instant("2026-05-27T21:10:45Z"), 1200, True, None, None),
        ]
        plan = SyncPlan(
            actions=[
                SyncAction("enrich", "fail", intervals_id="i-fail"),
                SyncAction("enrich", "ok", intervals_id="i-ok"),
            ],
            warnings=[],
        )
        adapter = InMemoryIntervalsAdapter(activities, timezone_name="America/Toronto", fail_updates={"i-fail"})

        outcomes = apply_sync_plan(plan, workouts, adapter)

        self.assertEqual([outcome.status for outcome in outcomes], ["failed", "enriched"])
        self.assertEqual(outcomes[0].reason, "activity_update_failed")
        self.assertEqual(adapter.updated_activity_ids, ["i-ok"])

    def test_replaces_existing_managed_block_in_enrich_update(self):
        workout = LiftosaurWorkout("123", instant("2026-05-26T21:07:57Z"), 3894, "new raw", "New summary", None)
        activity = IntervalsActivity(
            id="i1",
            type="WeightTraining",
            start=instant("2026-05-26T21:10:45Z"),
            duration_seconds=3884,
            has_heartrate=True,
            description="Before\nLIFTOSAUR-SYNC-START id=123\nOld\nLIFTOSAUR-SYNC-END id=123\nAfter",
            tags=None,
        )

        update = build_enrich_update(workout, activity)

        self.assertIn("Before", update["description"])
        self.assertIn("New summary", update["description"])
        self.assertNotIn("Old", update["description"])
        self.assertIn("After", update["description"])

    def test_builds_fallback_upsert_and_update_payloads(self):
        workout = LiftosaurWorkout(
            id="123",
            start=instant("2026-05-26T21:07:57Z"),
            duration_seconds=3894,
            text="raw",
            summary="Bench\nWork: 3x5 @ 100 lb",
            kg_lifted=680.388555,
            program="GZCLP",
            day_name="Day 1",
        )
        action = SyncAction("fallback", "123", intervals_id="i-created")

        upsert = build_fallback_upsert(workout, "America/Toronto")
        update = build_fallback_update(workout, action)

        self.assertEqual(upsert["external_id"], "liftosaur:123")
        self.assertEqual(upsert["name"], "Day 1")
        self.assertEqual(upsert["type"], "WeightTraining")
        self.assertEqual(upsert["start_date_local"], "2026-05-26T17:07:57")
        self.assertEqual(upsert["elapsed_time"], 3894)
        self.assertEqual(upsert["moving_time"], 3894)
        self.assertEqual(update["tags"], ["liftosaur", "liftosaur-fallback"])
        self.assertEqual(update["elapsed_time"], 3894)
        self.assertEqual(update["moving_time"], 3894)
        self.assertEqual(update["kg_lifted"], 680.388555)
        self.assertIn("LIFTOSAUR-SYNC-START id=123", update["description"])


if __name__ == "__main__":
    unittest.main()
