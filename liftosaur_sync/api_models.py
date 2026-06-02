from __future__ import annotations

from typing import Any, TypeVar

import msgspec


T = TypeVar("T")


class LiftosaurHistoryRecord(msgspec.Struct):
    id: int | str
    text: str


class LiftosaurHistoryData(msgspec.Struct):
    records: list[LiftosaurHistoryRecord] = msgspec.field(default_factory=list)
    hasMore: bool = False
    nextCursor: int | str | None = None


class LiftosaurHistoryResponse(msgspec.Struct):
    data: LiftosaurHistoryData = msgspec.field(default_factory=LiftosaurHistoryData)


class IntervalsActivityResponse(msgspec.Struct):
    id: int | str
    start_date: str | None = None
    start_date_local: str | None = None
    type: str | None = None
    name: str | None = None
    elapsed_time: int | float | str | None = None
    moving_time: int | float | str | None = None
    external_id: str | None = None
    tags: list[str] | None = None
    source: str | None = None
    strava_id: int | str | None = None
    has_heartrate: bool | None = None
    description: str | None = None


class IntervalsManualActivityResponse(msgspec.Struct):
    id: int | str


class IntervalsStreamResponse(msgspec.Struct):
    data: Any = None
    type: str | None = None
    name: str | None = None


class StravaActivityResponse(msgspec.Struct):
    id: int | str
    start_date: str
    name: str | None = None
    sport_type: str | None = None
    type: str | None = None
    elapsed_time: int | float | str | None = None
    moving_time: int | float | str | None = None
    has_heartrate: bool | None = None
    external_id: str | None = None


class StravaUploadResponse(msgspec.Struct):
    id: int | str


class StravaUploadStatusResponse(msgspec.Struct):
    activity_id: int | str | None = None
    error: str | None = None


class StravaTokenResponse(msgspec.Struct):
    access_token: str
    refresh_token: str
    expires_at: int | float | str | None = None


def parse_response(payload: object, type_: type[T], source: str) -> T:
    try:
        return msgspec.convert(payload, type=type_)
    except msgspec.ValidationError as error:
        raise RuntimeError(f"{source} response did not match expected shape: {error}") from error
