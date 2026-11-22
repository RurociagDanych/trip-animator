# Cinematic Photos

> Purpose: Define the trip photo quota and the cinematic photo playback experience.
> Audience: contributors, reviewers.
> Last reviewed: 2026-11-22.

- Confirmed requirements
- Delivery sequence
- Open decisions
- Existing-system observations
- Glossary

Status: Implemented for cinematic_stop playback (editor, shared view, render worker). Quota step is on main; grouped photos use the defaults in Open decisions. Verified with unit, worker and PostgreSQL tests and a rendered one-photo MP4. Editor browser check is open.

## Confirmed requirements

- A trip can contain up to 100 photos in total.
- Add a photo display option without replacing existing options. When the route marker reaches a photo, the map zooms in and transitions to the photo filling the playback frame. The photo stays visible for a configurable number of seconds, then the marker resumes the journey.
- The effect applies to the editor preview, exported MP4, and shared web playback wherever the viewer already has access to the photos. Photo access rules do not change.
- Route progress freezes from the start of the zoom-in until the return to the map completes. The configured seconds apply only while the photo is fully visible. The transitions and photo hold extend the runtime rather than shortening travel or other holds.
- Show every enabled photo attached to a route position. Photos at the same point or sufficiently close in marker travel time share one zoom-in and one return to the map. Each photo gets its own full hold, in route order with a stable tie-breaker. Disabled or unmatched photos count toward the quota but do not trigger stops.
- The user can set the grouping threshold in seconds of marker travel, excluding holds and transitions. Compare each photo to the preceding photo in route order, not to the first photo in the group. For example, with a one-second threshold, photos at 0.0, 0.8, and 1.6 seconds of travel form one group.
- During the full-frame display, hide the map overlays and telemetry, preserve the whole photo without cropping, and fill unused frame space with a blurred background.
- In this mode, the existing duration setting sets the base journey duration, excluding the additional time for photo holds and their transitions. Show the calculated total film duration near the top of the editor. The 180-second limit on the base duration does not itself cap the resulting film; do not silently truncate photos to meet a verified export constraint. Explain any such constraint before export.

## Delivery sequence

1. Raise the photo quota to 100 per trip. (Done: photos.py MAX_PHOTOS_PER_PROJECT = 100.)
2. First working slice: verify the entire one-photo sequence in the editor and an actual exported MP4. (Done for the MP4: one photo at progress 0.5 rendered to 8 s, 240 frames, photo visible about frames 90-145.)
3. Complete the release with grouped photos and shared web playback. (Implemented in the same change; the shared view uses the same schedule. Editor UI check in a browser is still open.)

## Open decisions

- Q9 — Grouping control defaults (applied: default 1 second, range 0-180). Recommendation: default to 1 second; 0 seconds groups only photos at the same route position. Include adjacent photos whose travel-time gap is exactly the configured threshold.
- Q10 — Return to the route (applied). Recommendation: start the stop when the marker reaches the first photo in the group, then return to that exact point and show the remaining route without jumps or repeated photo stops for photos the group already displayed.

## Existing-system observations

- The [photo upload endpoint](../../apps/api/src/trip_api/routers/photos.py) enforces a 100-photo project quota and rechecks it under a project lock before insertion.
- The [shared playback scheduler](../../apps/web/src/lib/geo/photos.ts) uses matched route progress to trigger displayed photos. Its existing pause rule can shorten holds to fit within 65% of active playback; short previews also scale hold time.
- The editor already offers a Cinematic Ken Burns photo card, not a map zoom or a full-frame display. The [render manifest](../../apps/worker/src/trip_worker/manifest.py) excludes that card style from repeated-frame pause windows. The new mode must not be confused with or replace that style.
- The [render spec](../../packages/telemetry/src/trip_telemetry/schemas.py) currently caps the configured duration at 180 seconds. At four seconds each, 100 fully visible photos take 400 seconds before travel or transitions. The safe effective runtime and corresponding worker limits are not yet verified.

## Glossary

- [Cinematic Photo Stop](../../GLOSSARY.md#trip-playback)
- [Photo Group](../../GLOSSARY.md#trip-playback)
