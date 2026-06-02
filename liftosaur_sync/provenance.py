from __future__ import annotations

import re

from liftosaur_sync.models import LiftosaurWorkout, MANAGED_END_TEMPLATE, MANAGED_START_TEMPLATE


class LiftosaurProvenance:
    def external_id(self, workout_or_id: LiftosaurWorkout | str) -> str:
        liftosaur_id = self._id(workout_or_id)
        return f"liftosaur:{liftosaur_id}"

    def matches_external_id(self, value: str | None, workout_or_id: LiftosaurWorkout | str) -> bool:
        if not value:
            return False
        return value.removesuffix(".json") == self.external_id(workout_or_id)

    def replace_intervals_managed_block(self, description: str | None, workout_or_id: LiftosaurWorkout | str, body: str) -> str:
        liftosaur_id = self._id(workout_or_id)
        start_marker = MANAGED_START_TEMPLATE.format(id=liftosaur_id)
        end_marker = MANAGED_END_TEMPLATE.format(id=liftosaur_id)
        block = f"{start_marker}\n{body}\n{end_marker}"
        existing = description or ""
        for pattern in self._managed_block_patterns(liftosaur_id):
            if pattern.search(existing):
                return pattern.sub(block, existing)
        if existing.strip():
            return existing.rstrip() + "\n\n" + block
        return block

    def has_intervals_managed_block(self, description: str | None, workout_or_id: LiftosaurWorkout | str) -> bool:
        liftosaur_id = self._id(workout_or_id)
        existing = description or ""
        return any(pattern.search(existing) for pattern in self._managed_block_patterns(liftosaur_id))

    def render_intervals_managed_block(self, workout: LiftosaurWorkout) -> str:
        title = workout.day_name or workout.program or "Strength Training"
        lines = [f"Liftosaur: {title}", f"Liftosaur history ID: {workout.id}"]
        if workout.duration_seconds is not None:
            lines.append(f"Duration: {workout.duration_seconds}s")
        if workout.kg_lifted is not None:
            lines.append(f"kg_lifted: {workout.kg_lifted:.3f}")
        lines.append("")
        lines.append(workout.summary)
        lines.append("")
        lines.append("Raw Liftosaur data:")
        lines.append(workout.text)
        return "\n".join(lines)

    def render_strava_description(self, workout: LiftosaurWorkout, intervals_activity: object) -> str:
        lines = ["Synced from Liftosaur."]
        if workout.program:
            lines.append(f"Program: {workout.program}")
        if workout.day_name:
            lines.append(f"Day: {workout.day_name}")
        lines.append(f"Liftosaur history ID: {workout.id}")
        if workout.kg_lifted is not None:
            lines.append(f"kg_lifted: {workout.kg_lifted:.3f}")
        lines.append(f"HR source: Intervals Activity {getattr(intervals_activity, 'id')}")
        return "\n".join(lines)

    def managed_block_patterns(self, liftosaur_id: str) -> list[re.Pattern[str]]:
        return self._managed_block_patterns(liftosaur_id)

    def _managed_block_patterns(self, liftosaur_id: str) -> list[re.Pattern[str]]:
        plain_start = MANAGED_START_TEMPLATE.format(id=liftosaur_id)
        plain_end = MANAGED_END_TEMPLATE.format(id=liftosaur_id)
        html_start = f"<!-- liftosaur-sync:start id={liftosaur_id} -->"
        html_end = f"<!-- liftosaur-sync:end id={liftosaur_id} -->"
        return [
            re.compile(re.escape(plain_start) + r".*?" + re.escape(plain_end), flags=re.DOTALL),
            re.compile(re.escape(html_start) + r".*?" + re.escape(html_end), flags=re.DOTALL),
        ]

    def _id(self, workout_or_id: LiftosaurWorkout | str) -> str:
        if isinstance(workout_or_id, LiftosaurWorkout):
            return workout_or_id.id
        return str(workout_or_id)


PROVENANCE = LiftosaurProvenance()
