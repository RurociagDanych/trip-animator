# Activity Library — Product and Implementation Specification

> Purpose: Specify the Activity Library product scope, contracts and acceptance criteria.
> Audience: contributors, reviewers
> Last reviewed: 2026-11-20

Sections:

- Provider-policy risk — deferred by the product owner
- Confirmed decisions
- Delivery contract
- Domain and persistence contracts
- Query and statistics contracts
- Synchronization and lifecycle contracts
- Acceptance and verification
- Existing-system observations
- Deferred details and explicit release gates
- Evidence and constraints to resolve
- Glossary

Status: first-release scope approved for incremental implementation; implementation and verification in progress.
Product name: provisional.

## Provider-policy risk — deferred by the product owner

The official Strava API Policy, effective June 1, 2026, was checked directly in the browser during discovery. The agreed persistent Strava-backed library is a product goal, not a validated permitted integration design.

- Section 5.5 expressly prohibits storing Strava Data or derived data in a Persistent Index, including search indexes and archives configured for subsequent retrieval or query.
- Sections 5.7 and 6.2 restrict storage of geographic/user information to the permitted transient cache, with a maximum seven-day retention period.
- Section 5.4 broadly restricts analytics and derived outputs; applicability to the proposed personal statistics requires clarification rather than an assumption of permission.
- Section 2.3 limits disclosure to the contributing authenticated user. Future social functionality cannot assume permission to share API-derived data.
- Section 6.3 requires reflecting activity deletions expeditiously and within 48 hours. Section 7.4 requires prompt deletion after revocation or applicable deletion requests, with a maximum 30-day deadline.
- Section 6.6 preserves a user's right to obtain their own bulk export. This alone does not establish permission for this portal to ingest, retain, analyze, or share that export; applicable terms require separate review.

Source: https://www.strava.com/legal/api_policy . These are engineering constraints identified from the published text, not a legal opinion.

The product owner explicitly chose to continue planning the persistent Strava API-backed library for primarily personal use and to defer resolving this provider-policy risk until a broader release/application-capacity review. This is an accepted product risk, not evidence of permission: personal use and a future athlete-capacity increase do not themselves establish an exception to the published restrictions. Do not block the current discovery on obtaining Strava approval. Before broader release, verify permission for the concrete storage, search, analytics, and sharing design separately from athlete-capacity approval. No scraping, quota circumvention, or retention workaround is part of the agreed scope. User-provided file ingestion remains a separately assessable extension, not a replacement first-release requirement.

## Confirmed decisions

- Move the bulk Strava import entry point out of Atlas without removing import capability.
- Introduce a dedicated activity-history area, not merely an import page.
- Include import/synchronization status, search, map exploration, and statistics in this area.
- Preserve Atlas as the view of curated trips and journeys prepared in the portal, separate from sporting history.
- Prioritize finding activities and navigating between a common filtered list, map, and statistics, with access to existing portal capabilities.
- Support multiple portal users with private per-user libraries from the outset.
- Exclude social features from the first release, while preserving a future extension path. This does not authorize public visibility or sharing now.
- Specify the complete feature but deliver incrementally. The first useful release includes historical import, reliable progress/recovery, search, and a jointly filtered activity list and map.
- Plan advanced analytics and exploration mechanics in the same specification without requiring every such feature for the first useful release.
- Library activities exist independently of trips and editor projects. Historical import must not automatically create trips or attach activities to projects. An activity may be used in multiple projects without duplicating its library identity.
- Include activities without GPS in the library and applicable statistics; omit them from geographic rendering. Include private Strava activities only when the user grants the required access.
- Define completeness against available permissions and provider data, and expose missing or unavailable data rather than claiming unconditional completeness.
- Import in two stages: make activity metadata searchable first, then enrich tracks and detailed analyses in the background. Distinguish catalog availability from track/analysis readiness.
- Atlas represents curated trips and journeys prepared in the portal. The Activity Library represents the entire sporting history, including routine workouts. Bulk historical-import controls belong only in the library area; navigation from Atlas to the library is allowed.
- The map roadmap includes activity tracks, exploration tiles, and an intensity heatmap. Tracks and list interaction ship first; exploration tiles precede the heatmap. The heatmap has the lowest priority among these map capabilities. Exploration challenges such as a maximum filled square and connected tile areas belong to a later stage.
- Initial search combines name, date range, sport, distance, elevation gain, duration, and GPS availability, with sorting. Search covers the entire imported catalog rather than the current page. List, map, and summary share the same activity selection; activities without GPS remain eligible for applicable summaries.
- The sport dropdown offers distinct activity types in the signed-in owner's catalog, independent of the current search and page. Options respect the hidden-activity toggle and exclude disconnected activities. Matching any selected sport is sufficient; clearing the selection includes all sports.
- Panning and zooming do not implicitly filter results. An explicit geographic action selects activities whose tracks intersect the selected area, rather than only activities starting there. Activities with pending tracks must be disclosed as not yet evaluable for geographic filtering.
- Strava is the initial ingestion source for the new library. The domain model remains provider-independent. Additional GPX/FIT ingestion, other providers, migration of existing trip uploads, and cross-provider duplicate detection are not implicitly included in the first release.
- Initial statistics cover activity count, distance, elevation gain, moving time, and elapsed time for the selected activities, with sport breakdowns. Missing measurements are not zero; incompatible sports must not be collapsed into misleading averages.
- The next analytics stage includes weekly/monthly/yearly trends, period comparisons, personal records, and consistency. Later specialist metrics may include Eddington number, equipment, heart rate, and power where data permits. Analytical charts should lead back to their contributing activities.
- Private named saved views are included in the first release. They save filter rules and sorting, not a frozen result list. Relative date ranges remain relative; explicit dates remain fixed.
- Calendar grouping uses the activity's local start date/time. An activity spanning midnight belongs to its start day without splitting basic summaries. Missing source timezone information requires a documented fallback.
- Rollout includes existing imported Strava activities without duplicating their library identities or changing existing trips, stage edits, or animations. Multiple project usages remain intact. Automatic matching between manual GPX uploads and Strava activities is excluded initially.
- After historical import, synchronize new and changed activities automatically using provider events plus periodic reconciliation, with a manual sync action. Jobs survive browser closure; retries are idempotent; live updates must not be starved by historical backfill. Show last successful synchronization, progress, failures, and rate-limit waits.
- Provider changes update the library, not existing project edits automatically. Used stages may expose an explicit source-update action. Splitting an activity while preparing an animation is strictly project-local: it creates or changes project segments, not additional library activities or provider activities. The original library activity remains intact; other projects using it are unaffected. Any source-update operation must account for project-local splits and must not silently replace or discard them.
- Distinguish reversible hiding from confirmed data deletion. Hidden activities are excluded from default results, maps, and statistics. Deletion must disclose project usages and prevent automatic resurrection during sync. Neither operation deletes the provider's activity. Source deletion and account disconnection need separate lifecycle rules.
- Design and validate against a reference history of 20,000 activities per user, without making this an account cap. Support desktop and mobile with persistent common filters across views; avoid loading the whole catalog or all detailed track points into the browser.
- For a project-local split, retain the project's current source material when the library source changes. The initial release does not automatically remap cuts or merge source changes into the edit. A new source version may be added alongside existing material; replacement requires an explicit action and impact warning.
- From the library, select one or multiple activities to create a trip or add them to an existing trip. Reuse ready local source data rather than downloading again. Pending track data exposes a waiting state; no-GPS activities are not presented as geographic animation material. Each project owns its edits while retaining source provenance.
- Opening an activity first shows library details rather than creating/opening an editor project: available track and metrics, ingestion state, provider link, and project usages. Project navigation and creation are explicit actions.
- Exploration coverage derives from the actual tracks of the selected library activities, never their project usages. Reusing or splitting an activity in projects cannot multiply coverage. Filters affect coverage; no-GPS activities contribute no coverage, and incomplete track coverage is disclosed. Distinct visited tiles and repeat visits are separate concepts.
- Disconnecting Strava stops synchronization but does not automatically delete projects. Permanent data deletion is a separate confirmed operation that identifies affected project usages. Provider-originated deletion and access revocation remain distinct lifecycle events, not equivalent to reversible hiding.

## Delivery contract

Approval covers incremental implementation of the first release, not a claim that all roadmap features already work. Preserve the current branch and existing project data. The product owner authorizes using and rebuilding the existing local Docker stack for integration and browser verification; preserve data volumes and avoid destructive resets. Coordinate shared-service rebuilds through the parent agent. Do not call live providers, publish, push, or deploy to the VPS as part of local implementation and verification. VPS deployment testing remains with the product owner through CI/CD. Changes are locally committed only after verification and independent review.

### First release

1. Separate canonical activity identity from project segments and source refresh. Protect existing splits before exposing the catalog.
2. Provide a per-user server-side catalog with bounded pagination, whole-selection search/filtering, deterministic ordering, details, and summaries calculated from the same selection.
3. Add a dedicated library dashboard view and move historical-import controls out of Atlas. Use persistent backend jobs and honest progress states; do not substitute the project-producing legacy import for independent history ingestion.
4. Connect the shared filtered selection to list, map tracks, basic statistics, and private saved views. Opening details does not create a project. Geographic filtering is explicit and based on track intersection.
5. Support explicit use of ready activity material in a new or existing project without mutating canonical history or other projects.
6. Reconcile existing Strava imports without deleting project material, duplicating source identities, or inventing unavailable original tracks. Expose incomplete legacy material as incomplete.

This sequence contains usable vertical slices. A passed slice is not acceptance of the entire first release.

### Subsequent releases

- Exploration tiles and later tile challenges, with a separately specified grid and visit semantics.
- Time trends, comparisons, records, consistency, and specialist analytics where source data supports them.
- Heatmap last. Social features, additional providers, cross-provider matching, and automatic remapping of project edits remain outside the first release.

## Domain and persistence contracts

- The canonical source identity is owned by a user and qualified by provider. Provider activity identifiers are never globally unique across users by assumption.
- A project stage is an editable usage of source material, not the source activity itself. Several stages, including split segments and stages in other projects, may reference one canonical activity.
- Project telemetry and edit boundaries belong to the project. Canonical telemetry, metadata, and exploration coverage cannot be replaced with project-edited geometry.
- Source refresh updates canonical data; automatic provider ingestion must not select and rewrite an arbitrary existing project stage merely because it shares a provider identifier.
- Reuse existing Vault models where their actual contracts support these rules. Do not create a parallel catalog solely for naming. Add a schema migration only where required to preserve identity, provenance, durable job state, or saved views.
- Migration must be repeatable and conservative. Legacy rows created from splits are not automatically independent sporting sessions. Ambiguous provenance must be disclosed rather than resolved by guessing or destructive merging.

## Query and statistics contracts

- Authentication and ownership apply to catalog, detail, geometry, summaries, saved views, and project-usage lookup. Another user's identifier must not reveal private activity data.
- Apply all active filters before pagination and aggregate computation. Include a stable identity tie-breaker in each ordering. Bound page sizes and validate ranges.
- Date filters and calendar buckets use activity-local start dates when available. If timezone/local date is unavailable, use UTC with an explicit fallback indication; never infer timezone from the viewer's browser.
- Name search is case-insensitive substring search. Initial duration filtering uses moving time, explicitly labelled; elapsed time remains a separate displayed metric.
- Preserve unknown measurements as unknown in activity details. Summary sums include only known values, with coverage counts where needed; a zero-valued measurement is distinct from missing data.
- No-GPS activities remain in applicable list and summary results unless explicitly filtered out. Geographic selections exclude non-evaluable tracks and disclose pending/incomplete coverage.
- Repeated project usage has no effect on catalog counts, distance totals, or canonical geographic coverage.
- Saved views persist versioned filter rules and sorting per user, not query results. Relative date presets resolve when opened. A missing activity cannot break a saved view.

## Synchronization and lifecycle contracts

- Metadata ingestion and track enrichment expose distinct readiness states. Metadata-first results are useful before all tracks finish.
- Durable server-side state records history progress, cursors, pending enrichment, retry/rate-limit timing, and recoverable failures. Browser polling observes work rather than owning its lifetime.
- Idempotent retries, historical scans, live updates, and reconciliation use the same canonical source identity. Provider rate limits are respected; a bounded page or temporary rate limit is not reported as complete history.
- Disconnect stops future provider work; existing project records are not automatically destroyed. An explicit permanent-delete flow must preview project impact and prevent silent reimport.
- Provider deletion/revocation processing must be distinguished from manual disconnect. Broader-release handling and permission remain subject to the recorded provider-policy risk; this specification does not declare preservation of API-derived copies to be authorized.
- Public sharing of library data is disabled. Existing trip-sharing behavior must not expose private library endpoints, source credentials, or unrelated activities.

## Acceptance and verification

Approved test boundaries are authenticated HTTP catalog/sync/project-stage endpoints, the actual local persistence/object-store pipeline, and user-visible browser interactions. Tests exercise observable behavior and failures rather than documentation wording.

- Splitting a source-backed stage yields independently editable project segments while canonical activity identity, count, geometry, metrics, and tile coverage remain unchanged. Another project remains unchanged.
- Refreshing a canonical source cannot silently overwrite existing project-local cuts or edits.
- Repeating imports does not create duplicate canonical identities or automatic projects. Closing the browser and resuming work does not lose the server-side job.
- A matching activity outside the first page is found by server search. Filtered list totals and basic summary values agree, including missing-measurement and no-GPS cases.
- A different user's list, details, geometry, and source usages remain inaccessible.
- Import entry points are absent from Atlas and available in the library. Failures, rate-limit waits, incomplete history, and pending tracks have truthful UI states.
- Desktop and mobile library navigation preserve the selection between list, map, statistics, and details. Verify actual browser interactions and screenshots, not only component mocks.
- Benchmark the reference history of 20,000 activities before claiming scale acceptance. Check bounded API results and bounded geometry rendering; record measured timings rather than promising unmeasured targets.
- Run focused regression checks while implementing, independent review for persistence/identity changes, and an integrated final candidate check. Existing concurrency, ownership, and project-edit protections remain covered. Any unavailable gate is reported as blocked, not passed.

## Existing-system observations

- Atlas currently lives inside the dashboard and exposes both a Strava picker and bulk import.
- Existing bulk import creates ingestion jobs associated with a project; ingestion creates stages and mirrors activity information into Vault.
- The current picker searches already-loaded data rather than an entire server-side activity index.
- Atlas H3 heatmap coverage differs from the existing Vault exploration-tile model.
- Historical import must not imply completion merely because one bounded batch finished.

### Confirmed split/library coupling to replace

The existing split endpoint mirrors both resulting project stages into Vault (`apps/api/src/trip_api/routers/stages.py:594-642`). Part two clears provider activity identifiers. The current integration test explicitly expects different Vault activities for the two resulting stages (`apps/api/tests/test_vault_sync.py:775-794`). This is incompatible with the agreed project-local split contract; existing Vault behavior cannot be reused unchanged as the canonical library model.

Implementation must first add regression coverage proving that a split leaves canonical activity count, original geometry, totals, and exploration coverage unchanged, while preserving independent project segments and provenance. Existing tests protecting concurrent-edit detection, user isolation, and idempotent ingestion must remain meaningful. Migration must not assume every existing Vault row represents an independent source activity; historical split-derived rows require evidence-based reconciliation without deleting project material or inventing lost original geometry.

Implementation references: `apps/web/src/components/dashboard/AtlasView.tsx`, `apps/web/src/components/editor/StravaPickerModal.tsx`, `apps/api/src/trip_api/routers/strava.py`, `apps/api/src/trip_api/ingest.py`, `apps/api/src/trip_api/db.py`.

## Deferred details and explicit release gates

1. Exploration-grid specification, repeat-visit counting, and challenge algorithms belong to the exploration release, not the initial catalog implementation.
2. Precise purge mechanics across shared source artifacts and project copies require an impact preview and verified artifact ownership before the permanent-delete action ships. Do not ship an unsafe placeholder deletion flow.
3. Actual provider application capacity, granted OAuth scopes, history completeness, and production performance require explicit verification before making corresponding release claims.
4. Provider-policy resolution and future social permissions remain preconditions for broader release, deferred by the product owner rather than silently considered satisfied.

## Evidence and constraints to resolve

- StatsHunters already supports shared filtering across activity views; this is baseline functionality rather than a unique differentiator.
- Review the current Strava API agreement and data-use/deletion rules before finalizing storage and synchronization architecture.
- Validate actual application limits and permissions rather than assuming a whole account can be imported immediately.

Public references: https://www.statshunters.com/faq ; https://developers.strava.com/docs/rate-limits/ ; https://developers.strava.com/docs/reference/ ; https://developers.strava.com/ .

## Glossary

The Activity Library domain language (Activity Library, Historical Import, Library Activity, Atlas, Project-local Split) is defined in [GLOSSARY.md](../../GLOSSARY.md).
