import unittest
from datetime import datetime

from liftosaur_sync import (
    IntervalsActivity,
    StravaActivity,
    StravaHRStream,
    build_strava_upload_payload,
    parse_liftosaur_workout,
    plan_strava_sync,
)


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class StravaStructuredUploadTests(unittest.TestCase):
    def test_builds_structured_upload_payload_from_work_sets_and_hr(self):
        workout = parse_liftosaur_workout(
            1780180669253,
            """2026-05-30 22:37:49 +00:00 / program: "GZCLP" / dayName: "Day 2" / week: 1 / dayInWeek: 2 / duration: 3170s / exercises: {
  Overhead Press / 4x3 70lb, 1x6 70lb / warmup: 1x5 55lb / target: 4x3 70lb, 1x3+ 70lb
  Deadlift / 8x1 220lb, 2x0 220lb / warmup: 1x5 65lb / target: 9x1 220lb, 1x1+ 220lb
  Bent Over Row / 2x15 55lb, 1x16 55lb / target: 2x15 55lb, 1x15+ 55lb
}""",
        )
        intervals = IntervalsActivity("i-hr", "WeightTraining", instant("2026-05-30T22:47:49Z"), 1800, True, None, None)
        hr = StravaHRStream([0, 60], [90, 100])

        payload, warnings = build_strava_upload_payload(workout, intervals, hr, "America/Toronto")

        self.assertEqual(payload["start_time"], "2026-05-30T22:37:49Z")
        self.assertEqual(payload["utc_offset"], -14400)
        self.assertEqual(payload["elapsed_time"], 3170)
        self.assertEqual(payload["name"], "GZCLP - Day 2")
        self.assertEqual(payload["external_id"], "liftosaur:1780180669253")
        self.assertEqual(payload["streams"], {"time": [600, 660], "heartrate": [90, 100]})
        self.assertEqual(len(payload["sets"]), 16)
        self.assertEqual(payload["sets"][0], {"exercise_type": "OVERHEAD_BARBELL_PRESS", "repetitions": 3, "weight": 31.751})
        self.assertEqual(payload["sets"][-1], {"exercise_type": "BARBELL_BENT_OVER_ROW", "repetitions": 16, "weight": 24.948})
        self.assertFalse(any("unmapped" in warning for warning in warnings))

    def test_strava_plan_skips_existing_external_id_before_time_match(self):
        workout = parse_liftosaur_workout(
            123,
            """2026-05-30 22:37:49 +00:00 / program: "GZCLP" / dayName: "Day 2" / duration: 3170s / exercises: {
  Deadlift / 1x1 220lb
}""",
        )
        intervals = IntervalsActivity("i-hr", "WeightTraining", instant("2026-05-30T22:37:49Z"), 3170, True, None, None)
        existing = StravaActivity("s1", "Uploaded", "WeightTraining", instant("2026-05-29T22:37:49Z"), 3170, False, "liftosaur:123.json")

        plan = plan_strava_sync([workout], [intervals], [existing], {"i-hr": StravaHRStream([0], [90])}, "America/Toronto")

        self.assertEqual(plan.actions[0].kind, "skip")
        self.assertEqual(plan.actions[0].reason, "already uploaded")
        self.assertEqual(plan.actions[0].strava_id, "s1")

    def test_strava_plan_skips_existing_time_match(self):
        workout = parse_liftosaur_workout(
            123,
            """2026-05-30 22:37:49 +00:00 / program: "GZCLP" / dayName: "Day 2" / duration: 3170s / exercises: {
  Deadlift / 1x1 220lb
}""",
        )
        intervals = IntervalsActivity("i-hr", "WeightTraining", instant("2026-05-30T22:37:49Z"), 3170, True, None, None)
        existing = StravaActivity("s1", "Strength", "WeightTraining", instant("2026-05-30T22:40:00Z"), 3000, True, "healthfit.fit")

        plan = plan_strava_sync([workout], [intervals], [existing], {"i-hr": StravaHRStream([0], [90])}, "America/Toronto")

        self.assertEqual(plan.actions[0].kind, "skip")
        self.assertEqual(plan.actions[0].reason, "existing Strava Time Match")
        self.assertEqual(plan.actions[0].strava_id, "s1")

    def test_strava_plan_requires_full_mapping_and_hr_stream(self):
        unmapped = parse_liftosaur_workout(
            123,
            """2026-05-30 22:37:49 +00:00 / duration: 3170s / exercises: {
  Mystery Lift / 1x1 220lb
}""",
        )
        no_hr = parse_liftosaur_workout(
            456,
            """2026-05-31 22:37:49 +00:00 / duration: 3170s / exercises: {
  Deadlift / 1x1 220lb
}""",
        )
        intervals = [
            IntervalsActivity("i-unmapped", "WeightTraining", unmapped.start, 3170, True, None, None),
            IntervalsActivity("i-no-hr", "WeightTraining", no_hr.start, 3170, True, None, None),
        ]

        plan = plan_strava_sync([unmapped, no_hr], intervals, [], {"i-unmapped": StravaHRStream([0], [90])}, "America/Toronto")

        self.assertEqual([action.kind for action in plan.actions], ["skip", "skip"])
        self.assertEqual(plan.actions[0].reason, "unmapped exercises")
        self.assertEqual(plan.actions[1].reason, "missing HR stream")


if __name__ == "__main__":
    unittest.main()
