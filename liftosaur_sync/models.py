from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


ELIGIBLE_INTERVALS_TYPES = {"WeightTraining", "Workout"}
ELIGIBLE_STRAVA_TYPES = {"WeightTraining", "Workout", "Crossfit"}
LB_TO_KG = 0.45359237
MANAGED_START_TEMPLATE = "LIFTOSAUR-SYNC-START id={id}"
MANAGED_END_TEMPLATE = "LIFTOSAUR-SYNC-END id={id}"
STRAVA_EXERCISE_TYPES = {
    "Bench Press": "BARBELL_BENCH_PRESS",
    "Deadlift": "BARBELL_DEADLIFT",
    "Lat Pulldown": "LAT_PULLDOWN",
    "Overhead Press": "OVERHEAD_BARBELL_PRESS",
    "Squat": "BARBELL_BACK_SQUAT",
}


@dataclass(frozen=True)
class LiftosaurSet:
    repetitions: int
    weight: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class LiftosaurExercise:
    name: str
    work_sets: tuple[LiftosaurSet, ...]
    warmup_sets: tuple[LiftosaurSet, ...] = ()
    missed_sets: tuple[LiftosaurSet, ...] = ()


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
    exercises: tuple[LiftosaurExercise, ...] = ()


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
class StravaActivity:
    id: str
    name: str | None
    sport_type: str | None
    start: datetime
    duration_seconds: int | None
    has_heartrate: bool | None
    external_id: str | None = None


@dataclass(frozen=True)
class StravaHRStream:
    time: list[int]
    heartrate: list[int | None]


@dataclass(frozen=True)
class StructuredStravaUpload:
    liftosaur_id: str
    intervals_id: str
    payload: dict[str, object]
    warnings: tuple[str, ...] = ()

    @property
    def mapped_set_count(self) -> int:
        sets = self.payload.get("sets", [])
        return len(sets) if isinstance(sets, list) else 0


@dataclass(frozen=True)
class StravaSyncAction:
    kind: str
    liftosaur_id: str
    intervals_id: str | None = None
    strava_id: str | None = None
    reason: str | None = None
    mapped_set_count: int = 0
    warnings: tuple[str, ...] = ()
    upload: StructuredStravaUpload | None = None

    @classmethod
    def upload_activity(cls, upload: StructuredStravaUpload) -> "StravaSyncAction":
        return cls(
            "upload",
            upload.liftosaur_id,
            intervals_id=upload.intervals_id,
            mapped_set_count=upload.mapped_set_count,
            warnings=upload.warnings,
            upload=upload,
        )

    @classmethod
    def skip(
        cls,
        liftosaur_id: str,
        reason: str,
        intervals_id: str | None = None,
        strava_id: str | None = None,
        warnings: tuple[str, ...] = (),
    ) -> "StravaSyncAction":
        return cls("skip", liftosaur_id, intervals_id=intervals_id, strava_id=strava_id, reason=reason, warnings=warnings)

    @property
    def is_upload(self) -> bool:
        return self.kind == "upload"

    @property
    def is_skip(self) -> bool:
        return self.kind == "skip"


@dataclass(frozen=True)
class StravaSyncPlan:
    actions: list[StravaSyncAction]
    warnings: list[str]


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

    @classmethod
    def enrich(
        cls,
        liftosaur_id: str,
        intervals_id: str,
        match_kind: str,
        warnings: tuple[str, ...] = (),
    ) -> "SyncAction":
        return cls("enrich", liftosaur_id, intervals_id=intervals_id, match_kind=match_kind, warnings=warnings)

    @classmethod
    def fallback(cls, liftosaur_id: str, intervals_id: str | None = None) -> "SyncAction":
        return cls("fallback", liftosaur_id, intervals_id=intervals_id)

    @classmethod
    def skip(cls, liftosaur_id: str, reason: str) -> "SyncAction":
        return cls("skip", liftosaur_id, reason=reason)

    @property
    def is_enrich(self) -> bool:
        return self.kind == "enrich"

    @property
    def is_fallback(self) -> bool:
        return self.kind == "fallback"

    @property
    def is_skip(self) -> bool:
        return self.kind == "skip"


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
class StravaWriteOutcome:
    liftosaur_id: str
    status: str
    strava_id: str | None = None
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class MatchCandidate:
    activity: object
    overlap_seconds: int
    start_delta_seconds: float


@dataclass(frozen=True)
class IntervalsTimeMatchIndex:
    candidates_by_workout: dict[str, list[MatchCandidate]]
    ambiguous_workout_ids: set[str]
    warnings: list[str]


@dataclass(frozen=True)
class StravaTokens:
    access_token: str
    refresh_token: str
    expires_at: int | None = None
