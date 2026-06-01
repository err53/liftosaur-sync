import unittest
from datetime import datetime, timezone

from liftosaur_intervals_sync import (
    IntervalsActivity,
    LiftosaurWorkout,
    PlanningOptions,
    parse_liftosaur_workout,
    plan_sync,
    replace_managed_block,
    render_managed_block,
)


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PlanningTests(unittest.TestCase):
    def test_time_match_enriches_existing_activity(self):
        workout = LiftosaurWorkout(
            id="123",
            start=instant("2026-05-26T21:07:57Z"),
            duration_seconds=3894,
            text="raw",
            summary="summary",
            kg_lifted=100.0,
        )
        activity = IntervalsActivity(
            id="i1",
            type="WeightTraining",
            start=instant("2026-05-26T21:10:45Z"),
            duration_seconds=3884,
            has_heartrate=True,
            description=None,
            tags=None,
        )

        plan = plan_sync([workout], [activity], PlanningOptions())

        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].kind, "enrich")
        self.assertEqual(plan.actions[0].liftosaur_id, "123")
        self.assertEqual(plan.actions[0].intervals_id, "i1")
        self.assertEqual(plan.actions[0].match_kind, "time")
        self.assertFalse(any(action.kind == "fallback" for action in plan.actions))

    def test_existing_managed_block_matches_by_liftosaur_id_before_time(self):
        workout = LiftosaurWorkout(
            id="123",
            start=instant("2026-05-26T21:07:57Z"),
            duration_seconds=3894,
            text="raw",
            summary="summary",
            kg_lifted=100.0,
        )
        activity_with_comment = IntervalsActivity(
            id="previous",
            type="WeightTraining",
            start=instant("2026-05-20T21:10:45Z"),
            duration_seconds=3884,
            has_heartrate=True,
            description="LIFTOSAUR-SYNC-START id=123\nold\nLIFTOSAUR-SYNC-END id=123",
            tags=None,
        )
        time_match = IntervalsActivity(
            id="time",
            type="WeightTraining",
            start=instant("2026-05-26T21:10:45Z"),
            duration_seconds=3884,
            has_heartrate=True,
            description=None,
            tags=None,
        )

        plan = plan_sync([workout], [activity_with_comment, time_match], PlanningOptions())

        self.assertEqual(plan.actions[0].kind, "enrich")
        self.assertEqual(plan.actions[0].intervals_id, "previous")
        self.assertEqual(plan.actions[0].match_kind, "description")

    def test_ineligible_overlap_warns_and_falls_back(self):
        workout = LiftosaurWorkout(
            id="123",
            start=instant("2026-05-26T21:00:00Z"),
            duration_seconds=3600,
            text="raw",
            summary="summary",
            kg_lifted=None,
        )
        activity = IntervalsActivity(
            id="row",
            type="VirtualRow",
            start=instant("2026-05-26T21:10:00Z"),
            duration_seconds=1200,
            has_heartrate=True,
            description=None,
            tags=None,
        )

        plan = plan_sync([workout], [activity], PlanningOptions())

        self.assertEqual(plan.actions[0].kind, "fallback")
        self.assertIn("ineligible overlapping Intervals Activity", plan.warnings[0])

    def test_crossfit_is_ineligible_for_v1(self):
        workout = LiftosaurWorkout("123", instant("2026-05-26T21:00:00Z"), 3600, "raw", "summary", None)
        activity = IntervalsActivity("cf", "Crossfit", instant("2026-05-26T21:00:00Z"), 3600, True, None, None)

        plan = plan_sync([workout], [activity], PlanningOptions())

        self.assertEqual(plan.actions[0].kind, "fallback")
        self.assertTrue(any("type Crossfit" in warning for warning in plan.warnings))

    def test_missing_duration_without_time_match_skips_fallback(self):
        workout = LiftosaurWorkout("123", instant("2026-05-26T21:00:00Z"), None, "raw", "summary", None)

        plan = plan_sync([workout], [], PlanningOptions())

        self.assertEqual(plan.actions[0].kind, "skip")
        self.assertEqual(plan.actions[0].reason, "missing duration")

    def test_multiple_eligible_matches_choose_hr_activity(self):
        workout = LiftosaurWorkout(
            id="123",
            start=instant("2026-05-26T21:00:00Z"),
            duration_seconds=3600,
            text="raw",
            summary="summary",
            kg_lifted=None,
        )
        no_hr = IntervalsActivity("nohr", "WeightTraining", instant("2026-05-26T21:00:00Z"), 3600, False, None, None)
        with_hr = IntervalsActivity("hr", "WeightTraining", instant("2026-05-26T21:05:00Z"), 3600, True, None, None)

        plan = plan_sync([workout], [no_hr, with_hr], PlanningOptions())

        self.assertEqual(plan.actions[0].kind, "enrich")
        self.assertEqual(plan.actions[0].intervals_id, "hr")
        self.assertTrue(any("multiple eligible" in warning for warning in plan.warnings))

    def test_one_activity_matching_multiple_workouts_skips_both(self):
        activity = IntervalsActivity("i1", "WeightTraining", instant("2026-05-26T21:00:00Z"), 7200, True, None, None)
        workouts = [
            LiftosaurWorkout("1", instant("2026-05-26T21:00:00Z"), 3600, "raw", "summary", None),
            LiftosaurWorkout("2", instant("2026-05-26T22:00:00Z"), 3600, "raw", "summary", None),
        ]

        plan = plan_sync(workouts, [activity], PlanningOptions())

        self.assertEqual([action.kind for action in plan.actions], ["skip", "skip"])
        self.assertTrue(any("matches multiple Liftosaur Workouts" in warning for warning in plan.warnings))


class ParserAndRenderingTests(unittest.TestCase):
    def test_parses_actual_liftosaur_shape_and_conservative_tonnage(self):
        text = """2026-05-30 22:37:49 +00:00 / program: "GZCLP" / dayName: "Day 2" / week: 1 / dayInWeek: 2 / duration: 3170s / exercises: {
  Overhead Press / 4x3 70lb, 1x6 70lb / warmup: 1x5 55lb / target: 4x3 70lb, 1x3+ 70lb
  Deadlift / 8x1 220lb, 2x0 220lb / warmup: 1x5 65lb / target: 9x1 220lb, 1x1+ 220lb
}"""

        workout = parse_liftosaur_workout(1780180669253, text)

        self.assertEqual(workout.id, "1780180669253")
        self.assertEqual(workout.start, datetime(2026, 5, 30, 22, 37, 49, tzinfo=timezone.utc))
        self.assertEqual(workout.duration_seconds, 3170)
        self.assertIn("Overhead Press", workout.summary)
        self.assertIn("Warmup: 1x5 @ 55 lb", workout.summary)
        self.assertIn("Work: 4x3 @ 70 lb, 1x6 @ 70 lb", workout.summary)
        self.assertIn("Missed: 2x0 @ 220 lb", workout.summary)
        expected_kg = ((4 * 3 * 70) + (1 * 6 * 70) + (8 * 1 * 220)) * 0.45359237
        self.assertAlmostEqual(workout.kg_lifted or 0, expected_kg, places=3)

    def test_replaces_only_managed_block(self):
        original = "User text\nLIFTOSAUR-SYNC-START id=123\nOld\nLIFTOSAUR-SYNC-END id=123\nFooter"

        updated = replace_managed_block(original, "123", "New")

        self.assertEqual(updated, "User text\nLIFTOSAUR-SYNC-START id=123\nNew\nLIFTOSAUR-SYNC-END id=123\nFooter")

    def test_rendered_managed_block_is_plain_text(self):
        workout = LiftosaurWorkout("123", instant("2026-05-26T21:07:57Z"), 3894, "raw text", "Squat\nWork: 3x5 @ 100 lb", 100.0)

        block = render_managed_block(workout)

        self.assertIn("Raw Liftosaur data:", block)
        self.assertNotIn("<details>", block)
        self.assertNotIn("```", block)


if __name__ == "__main__":
    unittest.main()
