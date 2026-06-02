from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tabulate import tabulate

from liftosaur_sync.api_models import (
    IntervalsActivityResponse,
    IntervalsManualActivityResponse,
    IntervalsStreamResponse,
    LiftosaurHistoryResponse,
    StravaActivityResponse,
    StravaUploadResponse,
    StravaUploadStatusResponse,
    parse_response,
)
from liftosaur_sync.auth import (
    authorize_strava_interactively,
    build_strava_authorize_url,
    extract_strava_code,
    get_strava_access_token,
    persist_env_value,
    require_env,
)
from liftosaur_sync.http import http_json as _http_json
from liftosaur_sync.http import http_multipart_json as _http_multipart_json
from liftosaur_sync.models import (
    ELIGIBLE_STRAVA_TYPES,
    LB_TO_KG,
    STRAVA_EXERCISE_TYPES,
    IntervalsActivity,
    IntervalsTimeMatchIndex,
    LiftosaurExercise,
    LiftosaurSet,
    LiftosaurWorkout,
    MatchCandidate,
    PlanningOptions,
    StravaActivity,
    StravaHRStream,
    StravaSyncAction,
    StravaSyncPlan,
    StravaWriteOutcome,
    StructuredStravaUpload,
    SyncAction,
    SyncPlan,
    WriteOutcome,
)
from liftosaur_sync.provenance import PROVENANCE


def parse_liftosaur_workout(record_id: int | str, text: str) -> LiftosaurWorkout:
    metadata = text.split("/ exercises:", 1)[0].strip()
    timestamp_text = metadata.split(" / ", 1)[0].strip()
    start = parse_liftosaur_datetime(timestamp_text)
    duration = _int_metadata(metadata, "duration")
    program = _quoted_metadata(metadata, "program")
    day_name = _quoted_metadata(metadata, "dayName")
    summary, kg_lifted, exercises = _parse_exercises(text)
    return LiftosaurWorkout(
        id=str(record_id),
        start=start,
        duration_seconds=duration,
        text=text,
        summary=summary,
        kg_lifted=kg_lifted,
        program=program,
        day_name=day_name,
        exercises=exercises,
    )


def parse_liftosaur_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"Liftosaur timestamp is missing timezone: {value}")
    return parsed.astimezone(timezone.utc)


def plan_sync(
    workouts: list[LiftosaurWorkout],
    activities: list[IntervalsActivity],
    options: PlanningOptions,
) -> SyncPlan:
    time_matches = TimeMatchPolicy(options)
    warnings: list[str] = []
    actions_by_workout: dict[str, SyncAction] = {}

    overlapping_workouts = time_matches.overlapping_workout_ids(workouts)
    for workout_id in sorted(overlapping_workouts):
        warnings.append(f"Liftosaur Workout {workout_id} overlaps another Liftosaur Workout; skipping")
        actions_by_workout[workout_id] = SyncAction.skip(workout_id, "overlapping Liftosaur Workouts")

    for workout in workouts:
        if workout.id in actions_by_workout:
            continue
        description_matches = [activity for activity in activities if PROVENANCE.has_intervals_managed_block(activity.description, workout)]
        if len(description_matches) == 1:
            activity = description_matches[0]
            action_warnings: list[str] = []
            if not activity.has_heartrate:
                warning = f"Matched Intervals Activity {activity.id} has no heart-rate data"
                warnings.append(warning)
                action_warnings.append(warning)
            actions_by_workout[workout.id] = SyncAction.enrich(workout.id, activity.id, "description", tuple(action_warnings))
        elif len(description_matches) > 1:
            warnings.append(f"Liftosaur Workout {workout.id} has managed blocks on multiple Intervals Activities; skipping")
            actions_by_workout[workout.id] = SyncAction.skip(workout.id, "multiple managed blocks")

    match_index = time_matches.index_intervals_activities(
        [workout for workout in workouts if workout.id not in actions_by_workout], activities
    )
    warnings.extend(match_index.warnings)

    for workout in workouts:
        if workout.id in actions_by_workout:
            continue
        if workout.id in match_index.ambiguous_workout_ids:
            actions_by_workout[workout.id] = SyncAction.skip(workout.id, "one Intervals Activity matches multiple Liftosaur Workouts")
            continue

        candidates = match_index.candidates_by_workout.get(workout.id, [])
        if candidates:
            if len(candidates) > 1:
                warnings.append(f"Liftosaur Workout {workout.id} has multiple eligible Time Matches; choosing best candidate")
            winner = time_matches.choose(candidates)
            if winner is None:
                warnings.append(f"Liftosaur Workout {workout.id} has tied Time Matches; skipping")
                actions_by_workout[workout.id] = SyncAction.skip(workout.id, "tied Time Matches")
                continue
            action_warnings: list[str] = []
            if not winner.activity.has_heartrate:
                warning = f"Matched Intervals Activity {winner.activity.id} has no heart-rate data"
                warnings.append(warning)
                action_warnings.append(warning)
            actions_by_workout[workout.id] = SyncAction.enrich(workout.id, winner.activity.id, "time", tuple(action_warnings))
            continue

        if workout.duration_seconds is None and options.default_duration_seconds is None:
            warnings.append(f"Liftosaur Workout {workout.id} has no duration; skipping Manual Fallback Activity")
            actions_by_workout[workout.id] = SyncAction.skip(workout.id, "missing duration")
        else:
            actions_by_workout[workout.id] = SyncAction.fallback(workout.id)

    return SyncPlan(actions=[actions_by_workout[workout.id] for workout in workouts], warnings=warnings)


def plan_strava_sync(
    workouts: list[LiftosaurWorkout],
    intervals_activities: list[IntervalsActivity],
    strava_activities: list[StravaActivity],
    hr_streams_by_intervals_id: dict[str, StravaHRStream],
    timezone_name: str,
    options: PlanningOptions | None = None,
) -> StravaSyncPlan:
    options = options or PlanningOptions()
    time_matches = TimeMatchPolicy(options)
    upload_policy = StructuredStravaUploadPolicy(timezone_name)
    warnings: list[str] = []
    actions: list[StravaSyncAction] = []
    for workout in workouts:
        external_matches = [activity for activity in strava_activities if PROVENANCE.matches_external_id(activity.external_id, workout)]
        if external_matches:
            if len(external_matches) > 1:
                warnings.append(f"Liftosaur Workout {workout.id} has multiple Strava external ID matches")
            actions.append(StravaSyncAction.skip(workout.id, "already uploaded", strava_id=external_matches[0].id))
            continue

        strava_time_matches = time_matches.strava_time_matches(workout, strava_activities)
        if strava_time_matches:
            if len(strava_time_matches) > 1:
                warnings.append(f"Liftosaur Workout {workout.id} has multiple existing Strava Time Matches")
            actions.append(StravaSyncAction.skip(workout.id, "existing Strava Time Match", strava_id=strava_time_matches[0].id))
            continue

        intervals_match = time_matches.choose_intervals_hr_source(workout, intervals_activities)
        if intervals_match is None:
            actions.append(StravaSyncAction.skip(workout.id, "missing Intervals Time Match"))
            continue
        hr_stream = hr_streams_by_intervals_id.get(intervals_match.activity.id)
        if hr_stream is None or not hr_stream.time or not hr_stream.heartrate:
            actions.append(StravaSyncAction.skip(workout.id, "missing HR stream", intervals_id=intervals_match.activity.id))
            continue

        unmapped = _unmapped_work_exercise_names(workout)
        if unmapped:
            warning = f"Liftosaur Workout {workout.id} has unmapped exercises: {', '.join(unmapped)}"
            warnings.append(warning)
            actions.append(
                StravaSyncAction.skip(
                    workout.id,
                    "unmapped exercises",
                    intervals_id=intervals_match.activity.id,
                    warnings=(warning,),
                )
            )
            continue

        try:
            upload = upload_policy.build(workout, intervals_match.activity, hr_stream)
        except ValueError as error:
            actions.append(StravaSyncAction.skip(workout.id, str(error), intervals_id=intervals_match.activity.id))
            continue
        action_warnings = list(upload.warnings)
        warnings.extend(action_warnings)
        actions.append(
            StravaSyncAction.upload_activity(upload)
        )
    return StravaSyncPlan(actions, warnings)


def build_strava_upload_payload(
    workout: LiftosaurWorkout,
    intervals_activity: IntervalsActivity,
    hr_stream: StravaHRStream,
    timezone_name: str,
) -> tuple[dict[str, object], list[str]]:
    upload = StructuredStravaUploadPolicy(timezone_name).build(workout, intervals_activity, hr_stream)
    return upload.payload, list(upload.warnings)


class StructuredStravaUploadPolicy:
    def __init__(self, timezone_name: str):
        self.timezone_name = timezone_name

    def build(
        self,
        workout: LiftosaurWorkout,
        intervals_activity: IntervalsActivity,
        hr_stream: StravaHRStream,
    ) -> StructuredStravaUpload:
        payload, warnings = self._payload(workout, intervals_activity, hr_stream)
        return StructuredStravaUpload(workout.id, intervals_activity.id, payload, tuple(warnings))

    def _payload(
        self,
        workout: LiftosaurWorkout,
        intervals_activity: IntervalsActivity,
        hr_stream: StravaHRStream,
    ) -> tuple[dict[str, object], list[str]]:
        if workout.duration_seconds is None:
            raise ValueError("missing duration")
        sets = _strava_sets_for_workout(workout)
        if not sets:
            raise ValueError("no mapped work sets")
        stream, warnings = _align_hr_stream(workout, intervals_activity, hr_stream)
        payload: dict[str, object] = {
            "version": "1.0",
            "start_time": _isoformat_z(workout.start),
            "utc_offset": _utc_offset_seconds(workout.start, self.timezone_name),
            "elapsed_time": workout.duration_seconds,
            "name": _strava_upload_name(workout),
            "description": PROVENANCE.render_strava_description(workout, intervals_activity),
            "external_id": PROVENANCE.external_id(workout),
            "sport_type": "WeightTraining",
            "streams": stream,
            "sets": sets,
        }
        return payload, warnings


def _unmapped_work_exercise_names(workout: LiftosaurWorkout) -> list[str]:
    return sorted(
        exercise.name
        for exercise in workout.exercises
        if exercise.work_sets and exercise.name not in STRAVA_EXERCISE_TYPES
    )


def _strava_sets_for_workout(workout: LiftosaurWorkout) -> list[dict[str, object]]:
    sets: list[dict[str, object]] = []
    for exercise in workout.exercises:
        exercise_type = STRAVA_EXERCISE_TYPES.get(exercise.name)
        if not exercise_type:
            continue
        for set_ in exercise.work_sets:
            if set_.repetitions <= 0:
                continue
            item: dict[str, object] = {"exercise_type": exercise_type, "repetitions": set_.repetitions}
            weight = _weight_kg(set_.weight, set_.unit)
            if weight is not None:
                item["weight"] = round(weight, 3)
            sets.append(item)
    return sets


def _weight_kg(weight: float | None, unit: str | None) -> float | None:
    if weight is None:
        return None
    if unit == "lb":
        return weight * LB_TO_KG
    if unit == "kg":
        return weight
    return None


def _align_hr_stream(
    workout: LiftosaurWorkout,
    intervals_activity: IntervalsActivity,
    hr_stream: StravaHRStream,
) -> tuple[dict[str, list[int]], list[str]]:
    if workout.duration_seconds is None:
        raise ValueError("missing duration")
    offset = int(round((intervals_activity.start - workout.start).total_seconds()))
    times: list[int] = []
    heartrates: list[int] = []
    for source_time, hr in zip(hr_stream.time, hr_stream.heartrate):
        if hr is None:
            continue
        aligned_time = offset + int(source_time)
        if 0 <= aligned_time <= workout.duration_seconds:
            times.append(aligned_time)
            heartrates.append(int(hr))
    if not times:
        raise ValueError("missing HR stream")
    warnings: list[str] = []
    if workout.duration_seconds > 0 and len(times) > 1:
        coverage = (times[-1] - times[0]) / workout.duration_seconds
        if coverage < 0.5:
            warnings.append(f"Liftosaur Workout {workout.id} has HR coverage below 50%")
    return {"time": times, "heartrate": heartrates}, warnings


def _strava_upload_name(workout: LiftosaurWorkout) -> str:
    if workout.program and workout.day_name:
        return f"{workout.program} - {workout.day_name}"
    return workout.program or workout.day_name or "Strength Training"


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_offset_seconds(value: datetime, timezone_name: str) -> int:
    offset = value.astimezone(ZoneInfo(timezone_name)).utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def replace_managed_block(description: str | None, liftosaur_id: str, body: str) -> str:
    return PROVENANCE.replace_intervals_managed_block(description, liftosaur_id, body)


def has_managed_block(description: str | None, liftosaur_id: str) -> bool:
    return PROVENANCE.has_intervals_managed_block(description, liftosaur_id)


def _managed_block_patterns(liftosaur_id: str) -> list[re.Pattern[str]]:
    return PROVENANCE._managed_block_patterns(liftosaur_id)


def render_managed_block(workout: LiftosaurWorkout) -> str:
    return PROVENANCE.render_intervals_managed_block(workout)


def _quoted_metadata(metadata: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}:\s*\"([^\"]*)\"", metadata)
    return match.group(1) if match else None


def _int_metadata(metadata: str, key: str) -> int | None:
    match = re.search(rf"\b{re.escape(key)}:\s*(\d+)s?\b", metadata)
    return int(match.group(1)) if match else None


def _parse_exercises(text: str) -> tuple[str, float | None, tuple[LiftosaurExercise, ...]]:
    exercise_lines = _exercise_lines(text)
    summary_lines: list[str] = []
    exercises: list[LiftosaurExercise] = []
    total_kg = 0.0
    saw_weighted_work = False
    tonnage_safe = True
    for line in exercise_lines:
        parts = [part.strip() for part in line.split(" / ")]
        if len(parts) < 2:
            summary_lines.append(line)
            tonnage_safe = False
            continue
        exercise_name = parts[0]
        work_text = parts[1]
        warmup_text = _section(parts, "warmup")
        work_sets = _parse_sets(work_text)
        warmup_sets = _parse_sets(warmup_text) if warmup_text else []
        summary_lines.append(exercise_name)
        if warmup_sets:
            summary_lines.append("Warmup: " + _format_sets(warmup_sets))
        work_sets_positive = [set_ for set_ in work_sets if set_[1] > 0]
        missed_sets = [set_ for set_ in work_sets if set_[1] == 0]
        if work_sets_positive:
            summary_lines.append("Work: " + _format_sets(work_sets_positive))
        if missed_sets:
            summary_lines.append("Missed: " + _format_sets(missed_sets))
        exercises.append(
            LiftosaurExercise(
                exercise_name,
                work_sets=tuple(_expand_set_groups(work_sets_positive)),
                warmup_sets=tuple(_expand_set_groups(warmup_sets)),
                missed_sets=tuple(_expand_set_groups(missed_sets)),
            )
        )
        if not work_sets and work_text:
            summary_lines.append("Work: " + work_text)
            tonnage_safe = False
        for count, reps, weight, unit in work_sets_positive:
            if weight is None:
                continue
            if unit == "lb":
                total_kg += count * reps * weight * LB_TO_KG
                saw_weighted_work = True
            elif unit == "kg":
                total_kg += count * reps * weight
                saw_weighted_work = True
            else:
                tonnage_safe = False
        summary_lines.append("")
    summary = "\n".join(summary_lines).strip()
    kg_lifted = total_kg if saw_weighted_work and tonnage_safe else None
    return summary, kg_lifted, tuple(exercises)


def _expand_set_groups(groups: list[tuple[int, int, float | None, str | None]]) -> list[LiftosaurSet]:
    sets: list[LiftosaurSet] = []
    for count, repetitions, weight, unit in groups:
        for _ in range(count):
            sets.append(LiftosaurSet(repetitions, weight, unit))
    return sets


def _exercise_lines(text: str) -> list[str]:
    if "exercises:" not in text:
        return []
    after = text.split("exercises:", 1)[1]
    return [
        line.strip()
        for line in after.splitlines()
        if line.strip() and line.strip() not in {"{", "}"} and not line.strip().startswith("//")
    ]


def _section(parts: list[str], name: str) -> str | None:
    prefix = f"{name}:"
    for part in parts[2:]:
        if part.startswith(prefix):
            return part[len(prefix) :].strip()
    return None


def _parse_sets(text: str | None) -> list[tuple[int, int, float | None, str | None]]:
    if not text:
        return []
    sets: list[tuple[int, int, float | None, str | None]] = []
    for chunk in text.split(","):
        cleaned = re.sub(r"\([^)]*\)", "", chunk).strip()
        match = re.match(r"(\d+)x(\d+)\+?\s*(?:(\d+(?:\.\d+)?)(lb|kg))?", cleaned)
        if not match:
            continue
        count = int(match.group(1))
        reps = int(match.group(2))
        weight = float(match.group(3)) if match.group(3) is not None else None
        unit = match.group(4)
        sets.append((count, reps, weight, unit))
    return sets


def _format_sets(sets: list[tuple[int, int, float | None, str | None]]) -> str:
    return ", ".join(_format_set(set_) for set_ in sets)


def _format_set(set_: tuple[int, int, float | None, str | None]) -> str:
    count, reps, weight, unit = set_
    if weight is None or unit is None:
        return f"{count}x{reps}"
    weight_text = str(int(weight)) if weight.is_integer() else str(weight)
    return f"{count}x{reps} @ {weight_text} {unit}"


class TimeMatchPolicy:
    def __init__(self, options: PlanningOptions):
        self.options = options

    def candidate(self, workout: LiftosaurWorkout, activity: object) -> MatchCandidate | None:
        activity_start = getattr(activity, "start")
        activity_duration = getattr(activity, "duration_seconds")
        start_delta = abs((workout.start - activity_start).total_seconds())
        overlap = _overlap_seconds(workout.start, workout.duration_seconds, activity_start, activity_duration)
        if overlap >= self.options.overlap_seconds or start_delta <= self.options.start_tolerance_seconds:
            return MatchCandidate(activity, overlap, start_delta)
        return None

    def choose(self, candidates: list[MatchCandidate]) -> MatchCandidate | None:
        ranked = sorted(candidates, key=self._rank_key)
        if len(ranked) > 1 and self._tie_key(ranked[0]) == self._tie_key(ranked[1]):
            return None
        return ranked[0]

    def overlapping_workout_ids(self, workouts: list[LiftosaurWorkout]) -> set[str]:
        overlapping: set[str] = set()
        for index, left in enumerate(workouts):
            for right in workouts[index + 1 :]:
                if _overlap_seconds(left.start, left.duration_seconds, right.start, right.duration_seconds) > 0:
                    overlapping.add(left.id)
                    overlapping.add(right.id)
        return overlapping

    def index_intervals_activities(
        self,
        workouts: list[LiftosaurWorkout],
        activities: list[IntervalsActivity],
    ) -> IntervalsTimeMatchIndex:
        warnings: list[str] = []
        candidates_by_workout: dict[str, list[MatchCandidate]] = {}
        matched_workouts_by_activity: dict[str, list[str]] = {}
        for workout in workouts:
            eligible: list[MatchCandidate] = []
            for activity in activities:
                candidate = self.candidate(workout, activity)
                if candidate is None:
                    continue
                if activity.type in self.options.eligible_types:
                    eligible.append(candidate)
                else:
                    warnings.append(
                        f"Liftosaur Workout {workout.id} has ineligible overlapping Intervals Activity "
                        f"{activity.id} of type {activity.type}; ignoring"
                    )
            candidates_by_workout[workout.id] = eligible
            for candidate in eligible:
                activity = candidate.activity
                matched_workouts_by_activity.setdefault(getattr(activity, "id"), []).append(workout.id)

        ambiguous_workout_ids: set[str] = set()
        for activity_id, workout_ids in matched_workouts_by_activity.items():
            if len(workout_ids) > 1:
                warnings.append(f"Intervals Activity {activity_id} matches multiple Liftosaur Workouts; skipping affected workouts")
                ambiguous_workout_ids.update(workout_ids)
        return IntervalsTimeMatchIndex(candidates_by_workout, ambiguous_workout_ids, warnings)

    def choose_intervals_hr_source(
        self,
        workout: LiftosaurWorkout,
        intervals_activities: list[IntervalsActivity],
    ) -> MatchCandidate | None:
        candidates = [
            candidate
            for activity in intervals_activities
            if activity.type in self.options.eligible_types and activity.has_heartrate
            for candidate in [self.candidate(workout, activity)]
            if candidate is not None
        ]
        return self.choose(candidates) if candidates else None

    def strava_time_matches(
        self,
        workout: LiftosaurWorkout,
        strava_activities: list[StravaActivity],
    ) -> list[StravaActivity]:
        return [
            activity
            for activity in strava_activities
            if activity.sport_type in ELIGIBLE_STRAVA_TYPES and self.candidate(workout, activity) is not None
        ]

    def intervals_hr_source_ids(
        self,
        workouts: list[LiftosaurWorkout],
        intervals_activities: list[IntervalsActivity],
    ) -> set[str]:
        return {
            candidate.activity.id
            for workout in workouts
            for activity in intervals_activities
            if activity.has_heartrate
            for candidate in [self.candidate(workout, activity)]
            if candidate is not None
        }

    def _rank_key(self, candidate: MatchCandidate) -> tuple[int, int, float, str]:
        activity = candidate.activity
        return (
            0 if getattr(activity, "has_heartrate") else 1,
            -candidate.overlap_seconds,
            candidate.start_delta_seconds,
            str(getattr(activity, "id")),
        )

    def _tie_key(self, candidate: MatchCandidate) -> tuple[int, int, float]:
        activity = candidate.activity
        return (
            0 if getattr(activity, "has_heartrate") else 1,
            -candidate.overlap_seconds,
            candidate.start_delta_seconds,
        )


def _time_match_candidate(
    workout: LiftosaurWorkout,
    activity: IntervalsActivity,
    options: PlanningOptions,
) -> MatchCandidate | None:
    return TimeMatchPolicy(options).candidate(workout, activity)


def _overlap_seconds(
    left_start: datetime,
    left_duration: int | None,
    right_start: datetime,
    right_duration: int | None,
) -> int:
    if left_duration is None or right_duration is None:
        return 0
    left_end = left_start + timedelta(seconds=left_duration)
    right_end = right_start + timedelta(seconds=right_duration)
    return max(0, int((min(left_end, right_end) - max(left_start, right_start)).total_seconds()))


def _choose_candidate(candidates: list[MatchCandidate]) -> MatchCandidate | None:
    return TimeMatchPolicy(PlanningOptions()).choose(candidates)


def _overlapping_workout_ids(workouts: list[LiftosaurWorkout]) -> set[str]:
    return TimeMatchPolicy(PlanningOptions()).overlapping_workout_ids(workouts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Liftosaur workout history to supported targets")
    subparsers = parser.add_subparsers(dest="command")
    intervals_parser = subparsers.add_parser("intervals", help="Sync Liftosaur Workouts to Intervals Activities")
    _add_sync_arguments(intervals_parser)
    strava_parser = subparsers.add_parser("strava", help="Upload structured Strava Activities")
    _add_sync_arguments(strava_parser)
    all_parser = subparsers.add_parser("all", help="Run Intervals sync and Strava structured uploads")
    _add_sync_arguments(all_parser)
    auth_parser = subparsers.add_parser("strava-auth", help="Authorize Strava and save STRAVA_REFRESH_TOKEN")
    auth_parser.add_argument("--strava-redirect-uri", help="OAuth redirect URI configured for the Strava app")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "strava-auth":
        authorize_strava_interactively(args.strava_redirect_uri)
        print("Saved STRAVA_REFRESH_TOKEN to .env")
        return 0
    if args.command == "intervals":
        return run_intervals_command(args)
    if args.command == "strava":
        return run_strava_command(args)
    if args.command == "all":
        return run_all_command(args)
    raise SystemExit(f"Unknown command: {args.command}")


def _add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Write planned changes")


def run_intervals_command(args: argparse.Namespace) -> int:
    timezone_name = require_env("SYNC_TIMEZONE")
    zone = ZoneInfo(timezone_name)
    since, until = _date_range(args, zone)
    liftosaur_records = fetch_liftosaur_history(since, until)
    workouts = [parse_liftosaur_workout(record_id, text) for record_id, text in liftosaur_records]
    intervals_adapter = HttpIntervalsAdapter(require_env("INTERVALS_API_KEY"), require_env("INTERVALS_ATHLETE_ID"), timezone_name)
    activities = intervals_adapter.list_activities(since, until)
    plan = plan_sync(workouts, activities, PlanningOptions())
    outcomes: list[WriteOutcome] = []
    if args.apply:
        outcomes = apply_sync_plan(plan, workouts, intervals_adapter)
    generated_at = datetime.now(timezone.utc).isoformat()
    if args.json:
        print(json.dumps(_plan_to_json(plan, generated_at, workouts, activities, outcomes), indent=2, sort_keys=True))
    else:
        print_plan(plan, workouts, activities, generated_at, outcomes)
    return 1 if any(outcome.status == "failed" for outcome in outcomes) else 0


def run_strava_command(args: argparse.Namespace) -> int:
    timezone_name = require_env("SYNC_TIMEZONE")
    zone = ZoneInfo(timezone_name)
    since, until = _date_range(args, zone)
    liftosaur_records = fetch_liftosaur_history(since, until)
    workouts = [parse_liftosaur_workout(record_id, text) for record_id, text in liftosaur_records]
    intervals_adapter = HttpIntervalsAdapter(require_env("INTERVALS_API_KEY"), require_env("INTERVALS_ATHLETE_ID"), timezone_name)
    intervals_activities = intervals_adapter.list_activities(since, until)
    strava_adapter = HttpStravaAdapter(get_strava_access_token(), timezone_name)
    strava_activities = strava_adapter.list_activities(since, until)
    hr_streams = fetch_hr_streams_for_strava(workouts, intervals_activities, intervals_adapter)
    plan = plan_strava_sync(workouts, intervals_activities, strava_activities, hr_streams, timezone_name)
    outcomes: list[StravaWriteOutcome] = []
    if args.apply:
        outcomes = apply_strava_sync_plan(plan, strava_adapter)
    generated_at = datetime.now(timezone.utc).isoformat()
    if args.json:
        print(json.dumps(_strava_plan_to_json(plan, generated_at, outcomes), indent=2, sort_keys=True))
    else:
        print_strava_plan(plan, generated_at, outcomes)
    return 1 if any(outcome.status == "failed" for outcome in outcomes) else 0


def run_all_command(args: argparse.Namespace) -> int:
    timezone_name = require_env("SYNC_TIMEZONE")
    zone = ZoneInfo(timezone_name)
    since, until = _date_range(args, zone)
    liftosaur_records = fetch_liftosaur_history(since, until)
    workouts = [parse_liftosaur_workout(record_id, text) for record_id, text in liftosaur_records]
    intervals_adapter = HttpIntervalsAdapter(require_env("INTERVALS_API_KEY"), require_env("INTERVALS_ATHLETE_ID"), timezone_name)
    intervals_activities = intervals_adapter.list_activities(since, until)
    strava_adapter = HttpStravaAdapter(get_strava_access_token(), timezone_name)
    strava_activities = strava_adapter.list_activities(since, until)
    hr_streams = fetch_hr_streams_for_strava(workouts, intervals_activities, intervals_adapter)
    intervals_plan = plan_sync(workouts, intervals_activities, PlanningOptions())
    strava_plan = plan_strava_sync(workouts, intervals_activities, strava_activities, hr_streams, timezone_name)
    intervals_outcomes: list[WriteOutcome] = []
    strava_outcomes: list[StravaWriteOutcome] = []
    if args.apply:
        intervals_outcomes = apply_sync_plan(intervals_plan, workouts, intervals_adapter)
        strava_outcomes = apply_strava_sync_plan(strava_plan, strava_adapter)
    generated_at = datetime.now(timezone.utc).isoformat()
    if args.json:
        print(
            json.dumps(
                {
                    "intervals": _plan_to_json(intervals_plan, generated_at, workouts, intervals_activities, intervals_outcomes),
                    "strava": _strava_plan_to_json(strava_plan, generated_at, strava_outcomes),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("Intervals")
        print_plan(intervals_plan, workouts, intervals_activities, generated_at, intervals_outcomes)
        print("\nStrava")
        print_strava_plan(strava_plan, generated_at, strava_outcomes)
    return 1 if any(outcome.status == "failed" for outcome in intervals_outcomes + strava_outcomes) else 0


def _date_range(args: argparse.Namespace, zone: ZoneInfo) -> tuple[date, date]:
    if args.since:
        since = date.fromisoformat(args.since)
    else:
        since = datetime.now(zone).date() - timedelta(days=args.days)
    until = date.fromisoformat(args.until) if args.until else datetime.now(zone).date() + timedelta(days=1)
    return since, until


def fetch_liftosaur_history(since: date, until: date) -> list[tuple[int | str, str]]:
    api_key = require_env("LIFTOSAUR_API_KEY")
    records: list[tuple[int | str, str]] = []
    cursor: str | None = None
    while True:
        params = {"limit": "200", "startDate": since.isoformat(), "endDate": until.isoformat()}
        if cursor:
            params["cursor"] = cursor
        payload = _http_json(
            "GET",
            "https://www.liftosaur.com/api/v1/history?" + urllib.parse.urlencode(params),
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response = parse_response(payload, LiftosaurHistoryResponse, "Liftosaur history")
        records.extend((record.id, record.text) for record in response.data.records)
        if not response.data.hasMore:
            break
        cursor = str(response.data.nextCursor)
    return records


def apply_sync_plan(
    plan: SyncPlan,
    workouts: list[LiftosaurWorkout],
    intervals_adapter: "IntervalsAdapter",
) -> list[WriteOutcome]:
    return IntervalsWriteWorkflow(intervals_adapter).apply(plan, workouts)


class IntervalsWriteWorkflow:
    def __init__(self, intervals_adapter: "IntervalsAdapter"):
        self.intervals_adapter = intervals_adapter

    def apply(self, plan: SyncPlan, workouts: list[LiftosaurWorkout]) -> list[WriteOutcome]:
        workouts_by_id = {workout.id: workout for workout in workouts}
        activities_by_id = {activity.id: activity for activity in self.intervals_adapter.activities}
        outcomes: list[WriteOutcome] = []
        for action in plan.actions:
            try:
                outcomes.append(self._apply_action(action, workouts_by_id, activities_by_id))
            except Exception as error:
                outcomes.append(
                    WriteOutcome(
                        action.liftosaur_id,
                        "failed",
                        intervals_id=action.intervals_id,
                        reason="activity_update_failed",
                        detail=str(error),
                    )
                )
        return outcomes

    def _apply_action(
        self,
        action: SyncAction,
        workouts_by_id: dict[str, LiftosaurWorkout],
        activities_by_id: dict[str, IntervalsActivity],
    ) -> WriteOutcome:
        if action.is_enrich:
            return self._enrich(action, workouts_by_id, activities_by_id)
        if action.is_fallback:
            return self._upsert_manual_fallback(action, workouts_by_id)
        if action.is_skip:
            return WriteOutcome(action.liftosaur_id, "skipped", reason=action.reason)
        raise ValueError(f"Unknown sync action kind: {action.kind}")

    def _enrich(
        self,
        action: SyncAction,
        workouts_by_id: dict[str, LiftosaurWorkout],
        activities_by_id: dict[str, IntervalsActivity],
    ) -> WriteOutcome:
        workout = workouts_by_id[action.liftosaur_id]
        activity = activities_by_id[action.intervals_id or ""]
        update = build_enrich_update(workout, activity)
        self.intervals_adapter.update_activity(activity.id, update)
        return WriteOutcome(workout.id, "enriched", intervals_id=activity.id)

    def _upsert_manual_fallback(
        self,
        action: SyncAction,
        workouts_by_id: dict[str, LiftosaurWorkout],
    ) -> WriteOutcome:
        workout = workouts_by_id[action.liftosaur_id]
        upsert = build_fallback_upsert(workout, self.intervals_adapter.timezone_name)
        intervals_id = self.intervals_adapter.upsert_manual_activity(upsert)
        update = build_fallback_update(workout, SyncAction.fallback(workout.id, intervals_id=intervals_id))
        self.intervals_adapter.update_activity(intervals_id, update)
        return WriteOutcome(workout.id, "fallback_upserted", intervals_id=intervals_id)


def fetch_hr_streams_for_strava(
    workouts: list[LiftosaurWorkout],
    intervals_activities: list[IntervalsActivity],
    intervals_adapter: IntervalsAdapter,
) -> dict[str, StravaHRStream]:
    streams: dict[str, StravaHRStream] = {}
    candidate_ids = TimeMatchPolicy(PlanningOptions()).intervals_hr_source_ids(workouts, intervals_activities)
    for activity_id in candidate_ids:
        stream = intervals_adapter.fetch_hr_stream(activity_id)
        if stream is not None and stream.time and stream.heartrate:
            streams[activity_id] = stream
    return streams


def apply_strava_sync_plan(
    plan: StravaSyncPlan,
    strava_adapter: HttpStravaAdapter,
) -> list[StravaWriteOutcome]:
    outcomes: list[StravaWriteOutcome] = []
    for action in plan.actions:
        try:
            if action.is_upload:
                if action.upload is None:
                    raise RuntimeError("missing Structured Strava Upload intent")
                strava_id = strava_adapter.upload_structured_activity(action.upload.payload)
                outcomes.append(StravaWriteOutcome(action.liftosaur_id, "uploaded", strava_id=strava_id))
            elif action.is_skip:
                outcomes.append(StravaWriteOutcome(action.liftosaur_id, "skipped", strava_id=action.strava_id, reason=action.reason))
        except Exception as error:
            outcomes.append(
                StravaWriteOutcome(
                    action.liftosaur_id,
                    "failed",
                    reason="strava_upload_failed",
                    detail=str(error),
                )
            )
    return outcomes


class IntervalsAdapter:
    timezone_name: str
    activities: list[IntervalsActivity]

    def list_activities(self, since: date, until: date) -> list[IntervalsActivity]:
        raise NotImplementedError

    def update_activity(self, activity_id: str, update: dict[str, object]) -> None:
        raise NotImplementedError

    def upsert_manual_activity(self, activity: dict[str, object]) -> str:
        raise NotImplementedError

    def fetch_hr_stream(self, activity_id: str) -> StravaHRStream | None:
        raise NotImplementedError


class HttpIntervalsAdapter(IntervalsAdapter):
    def __init__(self, api_key: str, athlete_id: str, timezone_name: str):
        self.api_key = api_key
        self.athlete_id = athlete_id
        self.timezone_name = timezone_name
        self.zone = ZoneInfo(timezone_name)
        self.activities: list[IntervalsActivity] = []
        self.auth = _intervals_auth_header(api_key)

    def list_activities(self, since: date, until: date) -> list[IntervalsActivity]:
        padded_since = since - timedelta(days=1)
        padded_until = until + timedelta(days=1)
        fields = ",".join(
            [
                "id",
                "name",
                "type",
                "start_date",
                "start_date_local",
                "elapsed_time",
                "moving_time",
                "external_id",
                "tags",
                "source",
                "strava_id",
                "has_heartrate",
                "description",
            ]
        )
        params = urllib.parse.urlencode(
            {"oldest": padded_since.isoformat(), "newest": padded_until.isoformat(), "limit": "200", "fields": fields}
        )
        payload = _http_json(
            "GET",
            f"https://intervals.icu/api/v1/athlete/{urllib.parse.quote(self.athlete_id)}/activities?{params}",
            headers={"Authorization": self.auth},
        )
        items = parse_response(payload, list[IntervalsActivityResponse], "Intervals activities")
        self.activities = [self._activity_from_item(item) for item in items if item.strava_id is None]
        return self.activities

    def update_activity(self, activity_id: str, update: dict[str, object]) -> None:
        _http_json(
            "PUT",
            f"https://intervals.icu/api/v1/activity/{urllib.parse.quote(activity_id)}",
            headers={"Authorization": self.auth, "Content-Type": "application/json"},
            body=update,
        )

    def upsert_manual_activity(self, activity: dict[str, object]) -> str:
        created = _http_json(
            "POST",
            f"https://intervals.icu/api/v1/athlete/{urllib.parse.quote(self.athlete_id)}/activities/manual/bulk",
            headers={"Authorization": self.auth, "Content-Type": "application/json"},
            body=[activity],
        )
        created_activities = parse_response(created, list[IntervalsManualActivityResponse], "Intervals manual fallback upsert")
        if not created_activities:
            raise RuntimeError("Intervals manual fallback upsert returned no activity id")
        return str(created_activities[0].id)

    def fetch_hr_stream(self, activity_id: str) -> StravaHRStream | None:
        query = urllib.parse.urlencode([("types", "time"), ("types", "heartrate")])
        payload = _http_json(
            "GET",
            f"https://intervals.icu/api/v1/activity/{urllib.parse.quote(activity_id)}/streams.json?{query}",
            headers={"Authorization": self.auth},
        )
        return _parse_intervals_hr_stream(payload)

    def _activity_from_item(self, item: IntervalsActivityResponse) -> IntervalsActivity:
        start = _parse_intervals_start(item, self.zone)
        duration = item.elapsed_time or item.moving_time
        return IntervalsActivity(
            id=str(item.id),
            type=item.type,
            start=start,
            duration_seconds=int(duration) if duration is not None else None,
            has_heartrate=item.has_heartrate,
            description=item.description,
            tags=item.tags,
            name=item.name,
            external_id=item.external_id,
            source=item.source,
        )


class InMemoryIntervalsAdapter(IntervalsAdapter):
    def __init__(
        self,
        activities: list[IntervalsActivity] | None = None,
        timezone_name: str = "UTC",
        fail_updates: set[str] | None = None,
    ):
        self.activities = list(activities or [])
        self.timezone_name = timezone_name
        self.fail_updates = set(fail_updates or set())
        self.updated_activity_ids: list[str] = []

    def list_activities(self, since: date, until: date) -> list[IntervalsActivity]:
        return self.activities

    def update_activity(self, activity_id: str, update: dict[str, object]) -> None:
        if activity_id in self.fail_updates:
            raise RuntimeError(f"configured failure for {activity_id}")
        self.updated_activity_ids.append(activity_id)
        for index, activity in enumerate(self.activities):
            if activity.id == activity_id:
                self.activities[index] = IntervalsActivity(
                    id=activity.id,
                    type=activity.type,
                    start=activity.start,
                    duration_seconds=int(update.get("elapsed_time", activity.duration_seconds or 0))
                    if ("elapsed_time" in update or activity.duration_seconds is not None)
                    else None,
                    has_heartrate=activity.has_heartrate,
                    description=str(update.get("description", activity.description)) if update.get("description", activity.description) is not None else None,
                    tags=list(update.get("tags", activity.tags) or []),
                    name=activity.name,
                    external_id=activity.external_id,
                    source=activity.source,
                )
                return

    def upsert_manual_activity(self, activity: dict[str, object]) -> str:
        external_id = str(activity["external_id"])
        for existing in self.activities:
            if existing.external_id == external_id:
                return existing.id
        activity_id = f"manual-{external_id}"
        local_start = datetime.fromisoformat(str(activity["start_date_local"])).replace(tzinfo=ZoneInfo(self.timezone_name))
        self.activities.append(
            IntervalsActivity(
                id=activity_id,
                type=str(activity.get("type")),
                start=local_start.astimezone(timezone.utc),
                duration_seconds=int(activity["elapsed_time"]),
                has_heartrate=False,
                description=str(activity.get("description", "")),
                tags=None,
                name=str(activity.get("name", "")),
                external_id=external_id,
                source="MANUAL",
            )
        )
        return activity_id

    def fetch_hr_stream(self, activity_id: str) -> StravaHRStream | None:
        return None


class HttpStravaAdapter:
    def __init__(self, access_token: str, timezone_name: str):
        self.access_token = access_token
        self.timezone_name = timezone_name
        self.zone = ZoneInfo(timezone_name)
        self.activities: list[StravaActivity] = []

    def list_activities(self, since: date, until: date) -> list[StravaActivity]:
        after = int(datetime.combine(since - timedelta(days=1), datetime.min.time(), self.zone).timestamp())
        before = int(datetime.combine(until + timedelta(days=1), datetime.min.time(), self.zone).timestamp())
        activities: list[StravaActivity] = []
        page = 1
        while True:
            params = urllib.parse.urlencode({"after": after, "before": before, "page": page, "per_page": 200})
            payload = _http_json(
                "GET",
                f"https://www.strava.com/api/v3/athlete/activities?{params}",
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            items = parse_response(payload, list[StravaActivityResponse], "Strava activities")
            if not items:
                break
            activities.extend(_strava_activity_from_item(item) for item in items)
            if len(items) < 200:
                break
            page += 1
        self.activities = activities
        return activities

    def upload_structured_activity(self, payload: dict[str, object]) -> str:
        file_payload = {
            key: payload[key]
            for key in ["version", "start_time", "utc_offset", "elapsed_time", "streams", "sets"]
            if key in payload
        }
        fields = {
            "sport_type": str(payload.get("sport_type", "WeightTraining")),
            "name": str(payload["name"]),
            "description": str(payload["description"]),
            "data_type": "json",
            "external_id": str(payload["external_id"]),
        }
        upload = _http_multipart_json(
            "POST",
            "https://www.strava.com/api/v3/uploads",
            headers={"Authorization": f"Bearer {self.access_token}"},
            fields=fields,
            file_field="file",
            filename=str(payload["external_id"]) + ".json",
            file_content=json.dumps(file_payload).encode(),
            file_content_type="application/json",
        )
        upload_response = parse_response(upload, StravaUploadResponse, "Strava upload")
        return self._poll_upload(str(upload_response.id))

    def _poll_upload(self, upload_id: str) -> str:
        for _ in range(30):
            time.sleep(1)
            payload = _http_json(
                "GET",
                f"https://www.strava.com/api/v3/uploads/{urllib.parse.quote(upload_id)}",
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            upload = parse_response(payload, StravaUploadStatusResponse, "Strava upload status")
            if upload.error:
                raise RuntimeError(upload.error)
            if upload.activity_id:
                return str(upload.activity_id)
        raise RuntimeError(f"Strava upload {upload_id} did not finish processing")


def build_enrich_update(workout: LiftosaurWorkout, activity: IntervalsActivity) -> dict[str, object]:
    update: dict[str, object] = {
        "description": PROVENANCE.replace_intervals_managed_block(
            activity.description,
            workout,
            PROVENANCE.render_intervals_managed_block(workout),
        ),
        "tags": _add_tags(activity.tags, ["liftosaur"]),
    }
    if workout.kg_lifted is not None:
        update["kg_lifted"] = workout.kg_lifted
    return update


def build_fallback_upsert(workout: LiftosaurWorkout, timezone_name: str) -> dict[str, object]:
    if workout.duration_seconds is None:
        raise ValueError(f"Liftosaur Workout {workout.id} has no duration")
    local_start = workout.start.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
    description = PROVENANCE.replace_intervals_managed_block(
        "",
        workout,
        PROVENANCE.render_intervals_managed_block(workout),
    )
    body: dict[str, object] = {
        "name": workout.day_name or workout.program or "Strength Training",
        "description": description,
        "type": "WeightTraining",
        "start_date_local": local_start.isoformat(timespec="seconds"),
        "elapsed_time": workout.duration_seconds,
        "moving_time": workout.duration_seconds,
        "external_id": PROVENANCE.external_id(workout),
    }
    if workout.kg_lifted is not None:
        body["kg_lifted"] = workout.kg_lifted
    return body


def build_fallback_update(workout: LiftosaurWorkout, action: SyncAction) -> dict[str, object]:
    if workout.duration_seconds is None:
        raise ValueError(f"Liftosaur Workout {workout.id} has no duration")
    description = PROVENANCE.replace_intervals_managed_block(
        "",
        workout,
        PROVENANCE.render_intervals_managed_block(workout),
    )
    update: dict[str, object] = {
        "description": description,
        "tags": ["liftosaur", "liftosaur-fallback"],
        "elapsed_time": workout.duration_seconds,
        "moving_time": workout.duration_seconds,
    }
    if workout.kg_lifted is not None:
        update["kg_lifted"] = workout.kg_lifted
    return update


def _add_tags(existing: list[str] | None, tags: list[str]) -> list[str]:
    result = list(existing or [])
    for tag in tags:
        if tag not in result:
            result.append(tag)
    return result


def _parse_intervals_start(item: IntervalsActivityResponse, zone: ZoneInfo) -> datetime:
    if item.start_date_local:
        local_value = item.start_date_local
        return datetime.fromisoformat(local_value).replace(tzinfo=zone).astimezone(timezone.utc)
    if item.start_date:
        start_value = item.start_date
        return datetime.fromisoformat(start_value.replace("Z", "+00:00")).astimezone(timezone.utc)
    raise ValueError(f"Intervals Activity {item.id} has no start time")


def _parse_intervals_hr_stream(payload: object) -> StravaHRStream | None:
    streams = parse_response(payload, list[IntervalsStreamResponse], "Intervals activity streams")
    time_values: list[int] | None = None
    hr_values: list[int | None] | None = None
    for item in streams:
        stream_type = str(item.type or item.name or "").lower()
        values = _stream_data_values(item.data)
        if not values:
            continue
        if stream_type in {"time", "timer_time", "secs", "seconds"}:
            time_values = [int(value) for value in values if value is not None]
        elif stream_type in {"heartrate", "heart_rate", "hr"}:
            hr_values = [int(value) if value is not None else None for value in values]
    if time_values is None or hr_values is None:
        return None
    count = min(len(time_values), len(hr_values))
    return StravaHRStream(time_values[:count], hr_values[:count]) if count else None


def _stream_data_values(data: object) -> list[object]:
    if isinstance(data, list):
        return list(data)
    if isinstance(data, dict):
        keys = list(data.keys())
        if all(str(key).lstrip("-").isdigit() for key in keys):
            return [data[key] for key in sorted(keys, key=lambda key: int(str(key)))]
        return list(data.values())
    return []


def _strava_activity_from_item(item: StravaActivityResponse) -> StravaActivity:
    start_value = item.start_date
    duration = item.elapsed_time or item.moving_time
    return StravaActivity(
        id=str(item.id),
        name=item.name,
        sport_type=item.sport_type or item.type,
        start=datetime.fromisoformat(start_value.replace("Z", "+00:00")).astimezone(timezone.utc),
        duration_seconds=int(duration) if duration is not None else None,
        has_heartrate=item.has_heartrate,
        external_id=item.external_id,
    )


def _intervals_auth_header(api_key: str) -> str:
    token = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
    return f"Basic {token}"


def print_plan(
    plan: SyncPlan,
    workouts: list[LiftosaurWorkout],
    activities: list[IntervalsActivity],
    generated_at: str,
    outcomes: list[WriteOutcome] | None = None,
) -> None:
    workouts_by_id = {workout.id: workout for workout in workouts}
    activities_by_id = {activity.id: activity for activity in activities}
    outcomes_by_liftosaur_id = {outcome.liftosaur_id: outcome for outcome in outcomes or []}
    rows: list[list[str]] = []
    for action in plan.actions:
        workout = workouts_by_id.get(action.liftosaur_id)
        workout_start = _format_display_time(workout.start) if workout else "unknown"
        kg_lifted = _format_kg_lifted(workout.kg_lifted) if workout else ""
        outcome = outcomes_by_liftosaur_id.get(action.liftosaur_id)
        outcome_text = outcome.status if outcome else ""
        if action.is_enrich:
            activity = activities_by_id.get(action.intervals_id or "")
            hr = "hr=yes" if activity and activity.has_heartrate else "hr=no"
            activity_start = _format_display_time(activity.start) if activity else "unknown"
            rows.append(["enrich", action.liftosaur_id, workout_start, kg_lifted, action.intervals_id or "", activity_start, action.match_kind or "", hr, outcome_text, _outcome_note(outcome)])
        elif action.is_fallback:
            intervals_id = outcome.intervals_id if outcome and outcome.intervals_id else ""
            rows.append(["fallback", action.liftosaur_id, workout_start, kg_lifted, intervals_id, "", "", "", outcome_text, f"external_id={PROVENANCE.external_id(action.liftosaur_id)}"])
        else:
            rows.append(["skip", action.liftosaur_id, workout_start, kg_lifted, "", "", "", "", outcome_text, action.reason or ""])
    print(f"Generated: {generated_at}")
    print(
        tabulate(
            rows,
            headers=["Action", "Liftosaur ID", "Liftosaur Start", "kg_lifted", "Intervals ID", "Intervals Start", "Match", "HR", "Outcome", "Note"],
            tablefmt="github",
        )
    )
    for warning in plan.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def print_strava_plan(
    plan: StravaSyncPlan,
    generated_at: str,
    outcomes: list[StravaWriteOutcome] | None = None,
) -> None:
    outcomes_by_liftosaur_id = {outcome.liftosaur_id: outcome for outcome in outcomes or []}
    rows: list[list[str]] = []
    for action in plan.actions:
        outcome = outcomes_by_liftosaur_id.get(action.liftosaur_id)
        if action.is_upload:
            rows.append(
                [
                    "upload",
                    action.liftosaur_id,
                    action.intervals_id or "",
                    "",
                    str(action.mapped_set_count),
                    outcome.status if outcome else "",
                    outcome.strava_id if outcome and outcome.strava_id else "",
                    _strava_outcome_note(outcome),
                ]
            )
        else:
            rows.append(
                [
                    "skip",
                    action.liftosaur_id,
                    action.intervals_id or "",
                    action.strava_id or "",
                    str(action.mapped_set_count) if action.mapped_set_count else "",
                    outcome.status if outcome else "",
                    outcome.strava_id if outcome and outcome.strava_id else "",
                    action.reason or _strava_outcome_note(outcome),
                ]
            )
    print(f"Generated: {generated_at}")
    print(
        tabulate(
            rows,
            headers=["Action", "Liftosaur ID", "Intervals HR ID", "Existing Strava ID", "Sets", "Outcome", "Uploaded Strava ID", "Note"],
            tablefmt="github",
        )
    )
    for warning in plan.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _strava_outcome_note(outcome: StravaWriteOutcome | None) -> str:
    if outcome is None:
        return ""
    if outcome.status == "failed":
        return f"{outcome.reason}: {outcome.detail}"
    return outcome.reason or ""


def _format_display_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_kg_lifted(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def _outcome_note(outcome: WriteOutcome | None) -> str:
    if outcome is None:
        return ""
    if outcome.status == "failed":
        return f"{outcome.reason}: {outcome.detail}"
    return outcome.reason or ""


def _plan_to_json(
    plan: SyncPlan,
    generated_at: str,
    workouts: list[LiftosaurWorkout] | None = None,
    activities: list[IntervalsActivity] | None = None,
    outcomes: list[WriteOutcome] | None = None,
) -> dict[str, object]:
    workouts_by_id = {workout.id: workout for workout in workouts or []}
    activities_by_id = {activity.id: activity for activity in activities or []}
    outcomes_by_liftosaur_id = {outcome.liftosaur_id: outcome for outcome in outcomes or []}
    return {
        "generated_at": generated_at,
        "actions": [
            {
                "kind": action.kind,
                "liftosaur_id": action.liftosaur_id,
                "liftosaur_start": workouts_by_id[action.liftosaur_id].start.isoformat()
                if action.liftosaur_id in workouts_by_id
                else None,
                "kg_lifted": workouts_by_id[action.liftosaur_id].kg_lifted
                if action.liftosaur_id in workouts_by_id
                else None,
                "intervals_id": action.intervals_id,
                "intervals_start": activities_by_id[action.intervals_id].start.isoformat()
                if action.intervals_id in activities_by_id
                else None,
                "match_kind": action.match_kind,
                "outcome": _outcome_to_json(outcomes_by_liftosaur_id.get(action.liftosaur_id)),
                "reason": action.reason,
                "warnings": list(action.warnings),
            }
            for action in plan.actions
        ],
        "warnings": plan.warnings,
    }


def _strava_plan_to_json(
    plan: StravaSyncPlan,
    generated_at: str,
    outcomes: list[StravaWriteOutcome] | None = None,
) -> dict[str, object]:
    outcomes_by_liftosaur_id = {outcome.liftosaur_id: outcome for outcome in outcomes or []}
    return {
        "generated_at": generated_at,
        "actions": [
            {
                "kind": action.kind,
                "liftosaur_id": action.liftosaur_id,
                "intervals_id": action.intervals_id,
                "strava_id": action.strava_id,
                "mapped_set_count": action.mapped_set_count,
                "reason": action.reason,
                "warnings": list(action.warnings),
                "outcome": _strava_outcome_to_json(outcomes_by_liftosaur_id.get(action.liftosaur_id)),
            }
            for action in plan.actions
        ],
        "warnings": plan.warnings,
    }


def _strava_outcome_to_json(outcome: StravaWriteOutcome | None) -> dict[str, object] | None:
    if outcome is None:
        return None
    return {
        "status": outcome.status,
        "strava_id": outcome.strava_id,
        "reason": outcome.reason,
        "detail": outcome.detail,
    }


def _outcome_to_json(outcome: WriteOutcome | None) -> dict[str, object] | None:
    if outcome is None:
        return None
    return {
        "status": outcome.status,
        "intervals_id": outcome.intervals_id,
        "reason": outcome.reason,
        "detail": outcome.detail,
    }


if __name__ == "__main__":
    raise SystemExit(main())
