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
    "Bent Over Row": "BENT_OVER_BARBELL_ROW",
    "Deadlift": "BARBELL_DEADLIFT",
    "Lat Pulldown": "LAT_PULLDOWN",
    "Overhead Press": "OVERHEAD_BARBELL_PRESS",
    "Squat": "BARBELL_BACK_SQUAT",
}
# Keep this catalog in sync with Strava's JSON strength upload docs:
# https://developers.strava.com/docs/uploads/#json-strength-training-limited
STRAVA_SUPPORTED_EXERCISE_TYPES_BY_CATEGORY = {
    "Bench Press": frozenset(
        {
            "BENCH_PRESS_GENERIC",
            "BARBELL_BENCH_PRESS",
            "DUMBBELL_BENCH_PRESS",
            "INCLINE_DUMBBELL_BENCH_PRESS",
            "INCLINE_BARBELL_BENCH_PRESS",
            "CLOSE_GRIP_BARBELL_BENCH_PRESS",
            "WIDE_GRIP_BARBELL_BENCH_PRESS",
        }
    ),
    "Deadlift": frozenset(
        {
            "DEADLIFT_GENERIC",
            "BARBELL_DEADLIFT",
            "DUMBBELL_DEADLIFT",
            "BARBELL_STRAIGHT_LEG_DEADLIFT",
            "SUMO_DEADLIFT",
            "RACK_PULL",
            "TRAP_BAR_DEADLIFT",
            "BARBELL_ROMANIAN_DEADLIFT",
        }
    ),
    "Pull Up": frozenset(
        {
            "PULL_UP_GENERIC",
            "LAT_PULLDOWN",
            "CLOSE_GRIP_CHIN_UP",
            "STRAIGHT_ARM_PULLDOWN",
            "ASSISTED_CHIN_UP",
            "WEIGHTED_CHIN_UP",
            "NEGATIVE_PULL_UP",
            "RING_PULL_UP",
        }
    ),
    "Row": frozenset(
        {
            "ROW_GENERIC",
            "SEATED_CABLE_ROW",
            "DUMBBELL_ROW",
            "FACE_PULL",
            "RENEGADE_ROW",
            "REVERSE_GRIP_BARBELL_ROW",
            "T_BAR_ROW",
            "KETTLEBELL_ROW",
            "BENT_OVER_ROW",
            "BENT_OVER_BARBELL_ROW",
            "BENT_OVER_DUMBBELL_ROW",
            "MACHINE_ISOLATERAL_HIGH_ROW",
            "LANDMINE_ROW",
            "SUSPENSION_LOW_ROW",
            "MACHINE_SEATED_ROW",
            "CHEST_SUPPORTED_ROW",
        }
    ),
    "Shoulder Press": frozenset(
        {
            "SHOULDER_PRESS_GENERIC",
            "OVERHEAD_BARBELL_PRESS",
            "BARBELL_PUSH_PRESS",
            "ARNOLD_PRESS",
            "OVERHEAD_DUMBBELL_PRESS",
            "STANDING_BARBELL_PRESS",
            "SEATED_BARBELL_PRESS",
        }
    ),
    "Squat": frozenset(
        {
            "SQUAT_GENERIC",
            "BARBELL_BACK_SQUAT",
            "GOBLET_SQUAT",
            "LEG_PRESS",
            "BARBELL_FRONT_SQUAT",
            "BARBELL_SQUAT_SNATCH",
            "BARBELL_STEP_UP",
            "OVERHEAD_SQUAT",
            "BARBELL_SQUAT",
        }
    ),
}
STRAVA_SUPPORTED_EXERCISE_TYPES = frozenset(
    exercise_type
    for exercise_types in STRAVA_SUPPORTED_EXERCISE_TYPES_BY_CATEGORY.values()
    for exercise_type in exercise_types
)


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
    metadata_update: dict[str, object] | None = None

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
    def update_metadata(
        cls,
        liftosaur_id: str,
        strava_id: str,
        metadata_update: dict[str, object],
        intervals_id: str | None = None,
        reason: str | None = None,
    ) -> "StravaSyncAction":
        return cls(
            "metadata",
            liftosaur_id,
            intervals_id=intervals_id,
            strava_id=strava_id,
            reason=reason,
            metadata_update=metadata_update,
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
    def is_metadata(self) -> bool:
        return self.kind == "metadata"

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
