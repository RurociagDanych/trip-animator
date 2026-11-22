# Cinematic Photos

> Purpose: Define the trip photo quota and the cinematic photo playback experience.
> Audience: contributors, reviewers.
> Last reviewed: 2026-11-22.

- Confirmed requirements
- Delivery sequence
- Open decisions
- Existing-system observations
- Glossary

Status: In review. cinematic_stop playback is implemented in the editor, shared view and render worker. The quota step is on main and grouped photos use the defaults in Open decisions. Verified with unit, worker and PostgreSQL tests and rendered single-photo and grouped-photo MP4s. Open: the manual editor check in a browser has not been done.

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

- Q9: Grouping control defaults (applied: default 1 second, range 0-180). Recommendation: default to 1 second; 0 seconds groups only photos at the same route position. Include adjacent photos whose travel-time gap is exactly the configured threshold.
- Q10: Return to the route (applied). Recommendation: start the stop when the marker reaches the first photo in the group, then return to that exact point and show the remaining route without jumps or repeated photo stops for photos the group already displayed.
- Q11: Long films and chunking (applied: no cap). Chunking stays enabled for cinematic_stop. Only the shared web schedule knows the film length, so the worker loads the render page once before splitting, reads the frame count it reports and uses that total for the chunk ranges, the stored manifest and the ffprobe duration check. Jobs without photo stops skip this step. The API stream deadline uses an upper bound of the base duration plus one stop per enabled photo.
- Q12: Preview rule (applied, owner to confirm). A preview shortens only the journey to 5 seconds. Photo holds and transitions keep their export length, and groups are decided from export travel seconds, so preview and export group the same photos. A preview with photos therefore runs longer than 5 seconds.
- Q13: Full-frame image (applied). cinematic_stop uses the full photo variant, served to the render through the virtual asset URL and to the editor and shared view through the existing image endpoints.
- Q14: Map zoom (accepted limitation). The zoom into the marker is a CSS scale of the map layer, so tiles are not re-rendered at a higher zoom and look softer at the peak of the zoom.

## Existing-system observations

- The [photo upload endpoint](../../apps/api/src/trip_api/routers/photos.py) enforces a 100-photo project quota and rechecks it under a project lock before insertion.
- The [shared playback scheduler](../../apps/web/src/lib/geo/photos.ts) uses matched route progress to trigger displayed photos. Its existing pause rule can shorten holds to fit within 65% of active playback; short previews also scale hold time.
- The editor already offers a Cinematic Ken Burns photo card, not a map zoom or a full-frame display. The [render manifest](../../apps/worker/src/trip_worker/manifest.py) excludes that card style from repeated-frame pause windows. The new mode must not be confused with or replace that style.
- The [render spec](../../packages/telemetry/src/trip_telemetry/schemas.py) currently caps the configured duration at 180 seconds. At four seconds each, 100 fully visible photos take 400 seconds before travel or transitions. The film length is therefore not capped by this limit; see Q11.

## Glossary

- [Cinematic Photo Stop](../../GLOSSARY.md#trip-playback)
- [Photo Group](../../GLOSSARY.md#trip-playback)
