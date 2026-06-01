from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from tabulate import tabulate


ELIGIBLE_INTERVALS_TYPES = {"WeightTraining", "Workout"}
LB_TO_KG = 0.45359237


@dataclass(frozen=True)
class LiftosaurWorkout:
    id: str
    start: datetime
    duration_seconds: int | None
    text: str
    summary: str
    kg_lifted: float | None
    program: str | None = None
    day_name: str | None = None


@dataclass(frozen=True)
class IntervalsActivity:
    id: str
    type: str | None
    start: datetime
    duration_seconds: int | None
    has_heartrate: bool | None
    description: str | None
    tags: list[str] | None
    name: str | None = None
    external_id: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class PlanningOptions:
    eligible_types: set[str] = field(default_factory=lambda: set(ELIGIBLE_INTERVALS_TYPES))
    overlap_seconds: int = 10 * 60
    start_tolerance_seconds: int = 30 * 60
    default_duration_seconds: int | None = None


@dataclass(frozen=True)
class SyncAction:
    kind: str
    liftosaur_id: str
    intervals_id: str | None = None
    reason: str | None = None
    match_kind: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncPlan:
    actions: list[SyncAction]
    warnings: list[str]


@dataclass(frozen=True)
class WriteOutcome:
    liftosaur_id: str
    status: str
    intervals_id: str | None = None
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class MatchCandidate:
    activity: IntervalsActivity
    overlap_seconds: int
    start_delta_seconds: float


def parse_liftosaur_workout(record_id: int | str, text: str) -> LiftosaurWorkout:
    metadata = text.split("/ exercises:", 1)[0].strip()
    timestamp_text = metadata.split(" / ", 1)[0].strip()
    start = parse_liftosaur_datetime(timestamp_text)
    duration = _int_metadata(metadata, "duration")
    program = _quoted_metadata(metadata, "program")
    day_name = _quoted_metadata(metadata, "dayName")
    summary, kg_lifted = _parse_exercises(text)
    return LiftosaurWorkout(
        id=str(record_id),
        start=start,
        duration_seconds=duration,
        text=text,
        summary=summary,
        kg_lifted=kg_lifted,
        program=program,
        day_name=day_name,
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
    warnings: list[str] = []
    actions_by_workout: dict[str, SyncAction] = {}

    overlapping_workouts = _overlapping_workout_ids(workouts)
    for workout_id in sorted(overlapping_workouts):
        warnings.append(f"Liftosaur Workout {workout_id} overlaps another Liftosaur Workout; skipping")
        actions_by_workout[workout_id] = SyncAction("skip", workout_id, reason="overlapping Liftosaur Workouts")

    candidates_by_workout: dict[str, list[MatchCandidate]] = {}
    matched_workouts_by_activity: dict[str, list[str]] = {}

    for workout in workouts:
        if workout.id in actions_by_workout:
            continue
        eligible: list[MatchCandidate] = []
        for activity in activities:
            candidate = _time_match_candidate(workout, activity, options)
            if candidate is None:
                continue
            if activity.type in options.eligible_types:
                eligible.append(candidate)
            else:
                warnings.append(
                    f"Liftosaur Workout {workout.id} has ineligible overlapping Intervals Activity "
                    f"{activity.id} of type {activity.type}; ignoring"
                )
        candidates_by_workout[workout.id] = eligible
        for candidate in eligible:
            matched_workouts_by_activity.setdefault(candidate.activity.id, []).append(workout.id)

    ambiguous_workout_ids: set[str] = set()
    for activity_id, workout_ids in matched_workouts_by_activity.items():
        if len(workout_ids) > 1:
            warnings.append(
                f"Intervals Activity {activity_id} matches multiple Liftosaur Workouts; skipping affected workouts"
            )
            ambiguous_workout_ids.update(workout_ids)

    for workout in workouts:
        if workout.id in actions_by_workout:
            continue
        if workout.id in ambiguous_workout_ids:
            actions_by_workout[workout.id] = SyncAction(
                "skip", workout.id, reason="one Intervals Activity matches multiple Liftosaur Workouts"
            )
            continue

        candidates = candidates_by_workout.get(workout.id, [])
        if candidates:
            if len(candidates) > 1:
                warnings.append(f"Liftosaur Workout {workout.id} has multiple eligible Time Matches; choosing best candidate")
            winner = _choose_candidate(candidates)
            if winner is None:
                warnings.append(f"Liftosaur Workout {workout.id} has tied Time Matches; skipping")
                actions_by_workout[workout.id] = SyncAction("skip", workout.id, reason="tied Time Matches")
                continue
            action_warnings: list[str] = []
            if not winner.activity.has_heartrate:
                warning = f"Matched Intervals Activity {winner.activity.id} has no heart-rate data"
                warnings.append(warning)
                action_warnings.append(warning)
            actions_by_workout[workout.id] = SyncAction(
                "enrich", workout.id, intervals_id=winner.activity.id, match_kind="time", warnings=tuple(action_warnings)
            )
            continue

        if workout.duration_seconds is None and options.default_duration_seconds is None:
            warnings.append(f"Liftosaur Workout {workout.id} has no duration; skipping Manual Fallback Activity")
            actions_by_workout[workout.id] = SyncAction("skip", workout.id, reason="missing duration")
        else:
            actions_by_workout[workout.id] = SyncAction("fallback", workout.id)

    return SyncPlan(actions=[actions_by_workout[workout.id] for workout in workouts], warnings=warnings)


def replace_managed_block(description: str | None, liftosaur_id: str, body: str) -> str:
    start_marker = f"<!-- liftosaur-sync:start id={liftosaur_id} -->"
    end_marker = f"<!-- liftosaur-sync:end id={liftosaur_id} -->"
    block = f"{start_marker}\n{body}\n{end_marker}"
    existing = description or ""
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        flags=re.DOTALL,
    )
    if pattern.search(existing):
        return pattern.sub(block, existing)
    if existing.strip():
        return existing.rstrip() + "\n\n" + block
    return block


def render_managed_block(workout: LiftosaurWorkout) -> str:
    title = workout.day_name or workout.program or "Strength Training"
    lines = [f"Liftosaur: {escape(title)}", f"Liftosaur history ID: {escape(workout.id)}"]
    if workout.duration_seconds is not None:
        lines.append(f"Duration: {workout.duration_seconds}s")
    if workout.kg_lifted is not None:
        lines.append(f"kg_lifted: {workout.kg_lifted:.3f}")
    lines.append("")
    lines.append(escape(workout.summary))
    lines.append("")
    lines.append("<details>")
    lines.append("<summary>Raw Liftosaur data</summary>")
    lines.append("")
    lines.append("```text")
    lines.append(escape(workout.text))
    lines.append("```")
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


def _quoted_metadata(metadata: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}:\s*\"([^\"]*)\"", metadata)
    return match.group(1) if match else None


def _int_metadata(metadata: str, key: str) -> int | None:
    match = re.search(rf"\b{re.escape(key)}:\s*(\d+)s?\b", metadata)
    return int(match.group(1)) if match else None


def _parse_exercises(text: str) -> tuple[str, float | None]:
    exercise_lines = _exercise_lines(text)
    summary_lines: list[str] = []
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
    return summary, kg_lifted


def _exercise_lines(text: str) -> list[str]:
    if "exercises:" not in text:
        return []
    after = text.split("exercises:", 1)[1]
    return [line.strip() for line in after.splitlines() if line.strip() and line.strip() not in {"{", "}"}]


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


def _time_match_candidate(
    workout: LiftosaurWorkout,
    activity: IntervalsActivity,
    options: PlanningOptions,
) -> MatchCandidate | None:
    start_delta = abs((workout.start - activity.start).total_seconds())
    overlap = _overlap_seconds(workout.start, workout.duration_seconds, activity.start, activity.duration_seconds)
    if overlap >= options.overlap_seconds or start_delta <= options.start_tolerance_seconds:
        return MatchCandidate(activity, overlap, start_delta)
    return None


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
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            0 if candidate.activity.has_heartrate else 1,
            -candidate.overlap_seconds,
            candidate.start_delta_seconds,
            candidate.activity.id,
        ),
    )
    if len(ranked) > 1:
        first_key = (
            0 if ranked[0].activity.has_heartrate else 1,
            -ranked[0].overlap_seconds,
            ranked[0].start_delta_seconds,
        )
        second_key = (
            0 if ranked[1].activity.has_heartrate else 1,
            -ranked[1].overlap_seconds,
            ranked[1].start_delta_seconds,
        )
        if first_key == second_key:
            return None
    return ranked[0]


def _overlapping_workout_ids(workouts: list[LiftosaurWorkout]) -> set[str]:
    overlapping: set[str] = set()
    for index, left in enumerate(workouts):
        for right in workouts[index + 1 :]:
            if _overlap_seconds(left.start, left.duration_seconds, right.start, right.duration_seconds) > 0:
                overlapping.add(left.id)
                overlapping.add(right.id)
    return overlapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan a Liftosaur to Intervals.icu sync")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Write planned enrichments and Manual Fallback Activities")
    args = parser.parse_args(argv)
    load_dotenv()
    timezone_name = require_env("SYNC_TIMEZONE")
    zone = ZoneInfo(timezone_name)
    since, until = _date_range(args, zone)
    liftosaur_records = fetch_liftosaur_history(since, until)
    workouts = [parse_liftosaur_workout(record["id"], record["text"]) for record in liftosaur_records]
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


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _date_range(args: argparse.Namespace, zone: ZoneInfo) -> tuple[date, date]:
    if args.since:
        since = date.fromisoformat(args.since)
    else:
        since = datetime.now(zone).date() - timedelta(days=args.days)
    until = date.fromisoformat(args.until) if args.until else datetime.now(zone).date() + timedelta(days=1)
    return since, until


def fetch_liftosaur_history(since: date, until: date) -> list[dict[str, object]]:
    api_key = require_env("LIFTOSAUR_API_KEY")
    records: list[dict[str, object]] = []
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
        data = payload.get("data", {})
        records.extend(data.get("records", []))
        if not data.get("hasMore"):
            break
        cursor = str(data.get("nextCursor"))
    return records


def apply_sync_plan(
    plan: SyncPlan,
    workouts: list[LiftosaurWorkout],
    intervals_adapter: "IntervalsAdapter",
) -> list[WriteOutcome]:
    workouts_by_id = {workout.id: workout for workout in workouts}
    activities_by_id = {activity.id: activity for activity in intervals_adapter.activities}
    outcomes: list[WriteOutcome] = []
    for action in plan.actions:
        try:
            if action.kind == "enrich":
                workout = workouts_by_id[action.liftosaur_id]
                activity = activities_by_id[action.intervals_id or ""]
                update = build_enrich_update(workout, activity)
                intervals_adapter.update_activity(activity.id, update)
                outcomes.append(WriteOutcome(workout.id, "enriched", intervals_id=activity.id))
            elif action.kind == "fallback":
                workout = workouts_by_id[action.liftosaur_id]
                upsert = build_fallback_upsert(workout, intervals_adapter.timezone_name)
                intervals_id = intervals_adapter.upsert_manual_activity(upsert)
                update = build_fallback_update(workout, SyncAction("fallback", workout.id, intervals_id=intervals_id))
                intervals_adapter.update_activity(intervals_id, update)
                outcomes.append(WriteOutcome(workout.id, "fallback_upserted", intervals_id=intervals_id))
            elif action.kind == "skip":
                outcomes.append(WriteOutcome(action.liftosaur_id, "skipped", reason=action.reason))
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


class IntervalsAdapter:
    timezone_name: str
    activities: list[IntervalsActivity]

    def list_activities(self, since: date, until: date) -> list[IntervalsActivity]:
        raise NotImplementedError

    def update_activity(self, activity_id: str, update: dict[str, object]) -> None:
        raise NotImplementedError

    def upsert_manual_activity(self, activity: dict[str, object]) -> str:
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
        self.activities = [self._activity_from_item(item) for item in payload if not item.get("strava_id")]
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
        if not isinstance(created, list) or not created or not isinstance(created[0], dict) or not created[0].get("id"):
            raise RuntimeError("Intervals manual fallback upsert returned no activity id")
        return str(created[0]["id"])

    def _activity_from_item(self, item: dict[str, object]) -> IntervalsActivity:
        start = _parse_intervals_start(item, self.zone)
        duration = item.get("elapsed_time") or item.get("moving_time")
        return IntervalsActivity(
            id=str(item["id"]),
            type=item.get("type"),
            start=start,
            duration_seconds=int(duration) if duration is not None else None,
            has_heartrate=item.get("has_heartrate"),
            description=item.get("description"),
            tags=item.get("tags"),
            name=item.get("name"),
            external_id=item.get("external_id"),
            source=item.get("source"),
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


def build_enrich_update(workout: LiftosaurWorkout, activity: IntervalsActivity) -> dict[str, object]:
    update: dict[str, object] = {
        "description": replace_managed_block(activity.description, workout.id, render_managed_block(workout)),
        "tags": _add_tags(activity.tags, ["liftosaur"]),
    }
    if workout.kg_lifted is not None:
        update["kg_lifted"] = workout.kg_lifted
    return update


def build_fallback_upsert(workout: LiftosaurWorkout, timezone_name: str) -> dict[str, object]:
    if workout.duration_seconds is None:
        raise ValueError(f"Liftosaur Workout {workout.id} has no duration")
    local_start = workout.start.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
    body: dict[str, object] = {
        "name": workout.day_name or workout.program or "Strength Training",
        "description": replace_managed_block("", workout.id, render_managed_block(workout)),
        "type": "WeightTraining",
        "start_date_local": local_start.isoformat(timespec="seconds"),
        "elapsed_time": workout.duration_seconds,
        "moving_time": workout.duration_seconds,
        "external_id": f"liftosaur:{workout.id}",
    }
    if workout.kg_lifted is not None:
        body["kg_lifted"] = workout.kg_lifted
    return body


def build_fallback_update(workout: LiftosaurWorkout, action: SyncAction) -> dict[str, object]:
    if workout.duration_seconds is None:
        raise ValueError(f"Liftosaur Workout {workout.id} has no duration")
    update: dict[str, object] = {
        "description": replace_managed_block("", workout.id, render_managed_block(workout)),
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


def _parse_intervals_start(item: dict[str, object], zone: ZoneInfo) -> datetime:
    local_value = item.get("start_date_local")
    if isinstance(local_value, str) and local_value:
        return datetime.fromisoformat(local_value).replace(tzinfo=zone).astimezone(timezone.utc)
    start_value = item.get("start_date")
    if isinstance(start_value, str) and start_value:
        return datetime.fromisoformat(start_value.replace("Z", "+00:00")).astimezone(timezone.utc)
    raise ValueError(f"Intervals Activity {item.get('id')} has no start time")


def _http_json(method: str, url: str, headers: dict[str, str], body: object | None = None) -> object:
    return _http_json_with_retry(method, url, headers, body)


def _http_json_with_retry(method: str, url: str, headers: dict[str, str], body: object | None) -> object:
    merged_headers = {"Accept": "application/json", "User-Agent": "liftosaur-intervals-sync/0.1", **headers}
    data = json.dumps(body).encode() if body is not None else None
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(4):
        request = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code not in retryable_statuses or attempt == 3:
                detail = error.read().decode(errors="replace")
                raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error
            retry_after = error.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            time.sleep(delay)
        except (TimeoutError, ConnectionError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"{method} {url} failed")


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
        outcome = outcomes_by_liftosaur_id.get(action.liftosaur_id)
        outcome_text = outcome.status if outcome else ""
        if action.kind == "enrich":
            activity = activities_by_id.get(action.intervals_id or "")
            hr = "hr=yes" if activity and activity.has_heartrate else "hr=no"
            activity_start = _format_display_time(activity.start) if activity else "unknown"
            rows.append(["enrich", action.liftosaur_id, workout_start, action.intervals_id or "", activity_start, action.match_kind or "", hr, outcome_text, _outcome_note(outcome)])
        elif action.kind == "fallback":
            intervals_id = outcome.intervals_id if outcome and outcome.intervals_id else ""
            rows.append(["fallback", action.liftosaur_id, workout_start, intervals_id, "", "", "", outcome_text, f"external_id=liftosaur:{action.liftosaur_id}"])
        else:
            rows.append(["skip", action.liftosaur_id, workout_start, "", "", "", "", outcome_text, action.reason or ""])
    print(f"Generated: {generated_at}")
    print(
        tabulate(
            rows,
            headers=["Action", "Liftosaur ID", "Liftosaur Start", "Intervals ID", "Intervals Start", "Match", "HR", "Outcome", "Note"],
            tablefmt="github",
        )
    )
    for warning in plan.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _format_display_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


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
