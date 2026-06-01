# Liftosaur Intervals Sync

This context describes the language for synchronizing strength training records from Liftosaur into Intervals.icu.

## Language

**Liftosaur Workout**:
A completed strength training session recorded in Liftosaur history. A Liftosaur Workout may correspond to one Intervals Activity when their time ranges describe the same training session.
_Avoid_: Liftosaur activity, Liftosaur session

**Intervals Activity**:
A workout-like record in Intervals.icu, including activities imported from devices or health platforms and activities created manually. An Intervals Activity may receive strength training details from a matching Liftosaur Workout.
_Avoid_: Intervals workout

**Time Match**:
The relationship between a Liftosaur Workout and an Intervals Activity when their recorded times indicate they represent the same training session.
_Avoid_: Source match, name match

**Manual Fallback Activity**:
An Intervals Activity created from a Liftosaur Workout only when no Time Match exists. A Manual Fallback Activity represents the strength session without device-recorded heart-rate data.
_Avoid_: Duplicate workout, backup activity

## Example Dialogue

Dev: Should we create a new Intervals Activity for every Liftosaur Workout?

Domain expert: Not if a Time Match already exists. Add the Liftosaur strength details to the matching Intervals Activity first, then create a manual activity only when no Time Match is found.
