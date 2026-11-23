import type { ActivityType } from "../../components/icons";

export interface VaultActivityReadinessOut { metadata_available: boolean; has_gps: boolean; track_ready: boolean; tile_precision: "none" | "summary_polyline" | "full_stream" }
export type ProjectReuseEligibility = "ready" | "waiting" | "no_gps" | "unavailable";
export interface VaultActivityListItemOut {
  id: string; name: string; activity_type: string; raw_sport_type: string; source_provider: string;
  started_at: string; local_date: string; timezone_name: string | null; date_is_utc_fallback: boolean;
  distance_m: number | null; elevation_gain_m: number | null; moving_seconds: number | null; elapsed_seconds: number | null;
  has_gps: boolean; readiness: VaultActivityReadinessOut; project_reuse_eligibility?: ProjectReuseEligibility; hidden?: boolean;
}
export interface VaultActivitySportSummaryOut {
  activity_type: string; activity_count: number; distance_m: number | null; distance_known_count: number;
  elevation_gain_m: number | null; elevation_gain_known_count: number; moving_seconds: number | null;
  moving_seconds_known_count: number; elapsed_seconds: number | null; elapsed_seconds_known_count: number;
  gps_activity_count: number; no_gps_activity_count: number;
}
export interface VaultActivitySummaryOut {
  activity_count: number; distance_m: number | null; distance_known_count: number;
  elevation_gain_m: number | null; elevation_gain_known_count: number; moving_seconds: number | null;
  moving_seconds_known_count: number; elapsed_seconds: number | null; elapsed_seconds_known_count: number;
  gps_activity_count: number; no_gps_activity_count: number;
}
export interface VaultActivityListOut { items: VaultActivityListItemOut[]; total: number; offset: number; limit: number; summary: VaultActivitySummaryOut; by_sport: VaultActivitySportSummaryOut[]; geographic_excluded_count?: number }
export interface LibraryDisconnectPreviewOut {
  operation: "disconnect"; confirmation: string;
  project_usages: { project_id: string; title: string; stage_id: string }[];
  preserves_project_material: true; permanent_purge_available: false; retention_notice: string;
}
export interface VaultActivityMapFeature { type: "Feature"; geometry: { type: "LineString"; coordinates: number[][] }; properties: { id: string; name: string; precision: "summary_polyline" } }
export interface VaultActivityMapOut { type: "FeatureCollection"; features: VaultActivityMapFeature[]; total: number; drawable_count: number; no_gps_count: number; unavailable_count: number; returned_count: number; truncated: boolean; geographic_excluded_count: number; unevaluated_count: number; next_offset?: number | null; unavailable_in_page_count?: number }
export interface VaultActivityDetailOut extends VaultActivityListItemOut {
  strava_activity_id: number | null; started_at_local: string | null; utc_offset_s: number;
  elevation_loss_m: number | null; elev_high_m: number | null; elev_low_m: number | null;
  avg_speed_mps: number | null; max_speed_mps: number | null; avg_hr_bpm: number | null; max_hr_bpm: number | null;
  avg_watts: number | null; weighted_watts: number | null; kilojoules: number | null; avg_cadence: number | null;
  suffer_score: number | null; gear_id: string | null; gear_name: string | null; is_commute: boolean;
  is_indoor_virtual: boolean; is_private: boolean; summary_polyline: string | null; tile_count_z14: number;
  new_tiles_z14_count: number; primary_country: string | null; source_url: string | null;
  project_usages: { project_id: string; project_title: string; stage_id: string; stage_name: string }[];
}
export interface VaultSyncStatusOut {
  connected: boolean; status: string; phase: string; phase_label: string; mode: string; progress_pct: number | null;
  processed_activities: number; estimated_total_activities: number | null; imported_count: number; vault_version: number;
  last_synced_at: string | null; last_error: string | null;
}

export type ResolutionPreset =
  | "1280x720"
  | "1920x1080"
  | "1080x1080"
  | "1080x1350"
  | "1080x1920"
  | "720x1280"
  | "720x900"
  | "720x720";

export type MapStylePreset =
  | "liberty"
  | "positron"
  | "bright"
  | "dark"
  | "topo-relief"
  | "satellite-hybrid"
  | "editorial-mono";
export type TimelineMode = "distance" | "recorded-time";
export type CameraMode = "fit-track" | "follow-marker";
export type CameraPreset = "flat" | "dynamic-3d" | "drone-orbit";
export type FollowZoomMode = "adaptive" | "fixed";
export type VisibilityMode = "full" | "traversed" | "trail";
export type UnitsMode = "metric" | "imperial";
export type TransitionIcon = "none" | "bed" | "meal" | "coffee" | "tent" | "transport";
export type LodLevel = "full" | "medium" | "low";
export type PanelPosition = "top" | "bottom" | "left" | "right";
export type RenderJobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type PhotoSourceType = "upload" | "google_photos";
export type PhotoMatchMethod = "timestamp" | "gps" | "manual" | "unmatched";
export type StageFormat = "gpx" | "fit" | "tcx" | "strava";
export type ColorByMode = "stage" | "grade" | "speed" | "hr" | "watts";
export type DirectorPreset = "none" | "expedition-doc" | "climb-spotlight" | "social-reel";
export type PhotoCardStyle = "corner" | "polaroid" | "cinematic" | "cinematic_stop";
export type PalettePreset = "alpine-dawn" | "topo-emerald" | "stage-by-stage";
export type ClimbCategory = "HC" | "Cat 1" | "Cat 2" | "Cat 3" | "Cat 4";
export type StageFocusMode = "auto" | "close" | "street" | "custom";

export interface StageCameraOverride {
  focus_mode: StageFocusMode;
  target_zoom: number | null;
  speed_multiplier: number;
  transition_margin: number;
}

export type StatisticMetric =
  | "date"
  | "time"
  | "trip-day"
  | "elapsed-time"
  | "speed"
  | "elevation"
  | "distance-travelled"
  | "distance-remaining"
  | "grade"
  | "hr"
  | "power"
  | "vam";

export const ALL_STATISTIC_METRICS: readonly StatisticMetric[] = [
  "date",
  "time",
  "trip-day",
  "elapsed-time",
  "speed",
  "elevation",
  "distance-travelled",
  "distance-remaining",
  "grade",
  "hr",
  "power",
  "vam",
];

export interface HealthResponse {
  status: string;
  service?: string;
  env?: string;
}

export interface PublicConfigOut {
  dev_auth_enabled: boolean;
  registration_enabled?: boolean;
  strava_configured: boolean;
  google_photos_configured?: boolean;
}

export interface RegisterRequest {
  email: string;
  display_name: string;
  password: string;
  invite_code: string;
}

export interface RegisterResponse {
  status: "verification_required";
  email: string;
  message: string;
}

export interface ResendVerificationResponse {
  status: "accepted";
  message: string;
}

export interface ForgotPasswordResponse {
  status: "accepted";
  message: string;
}

export interface ResetPasswordRequest {
  token: string;
  new_password: string;
}

export interface ResetPasswordResponse {
  ok: boolean;
  message: string;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface ChangePasswordResponse {
  ok: boolean;
  revoked_sessions_count: number;
}

export interface AuthSessionOut {
  id: string;
  created_at: string;
  expires_at: string;
  is_current: boolean;
}

export interface LogoutAllResponse {
  ok: boolean;
  revoked_sessions_count: number;
}

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  strava_connected: boolean;
}

export interface SessionInfoOut {
  user: UserOut;
  csrf_token: string;
  dev_auth_enabled: boolean;
}

export interface StatisticsPanelConfig {
  enabled: boolean;
  position: PanelPosition;
  time_zone: string;
  text_size: number;
  scale: number;
  text_color: string;
  background_color: string;
  background_opacity: number;
  metrics: StatisticMetric[];
  collision_avoidance: boolean;
}

export interface CameraKeyframe {
  progress: number;
  camera_mode: CameraMode;
  camera_preset: CameraPreset;
  pitch_deg: number;
  bearing_offset_deg: number;
  zoom_offset: number;
  speed_multiplier: number;
  label: string;
}

export type RenderFps = 15 | 30 | 60;

export interface RenderSpecV2 {
  version: 2;
  resolution: ResolutionPreset;
  fps: RenderFps;
  map_style: MapStylePreset;
  duration_seconds: number;
  timeline_mode: TimelineMode;
  camera_mode: CameraMode;
  camera_preset: CameraPreset;
  camera_bearing_deg: number;
  terrain_3d: boolean;
  camera_pitch_deg: number;
  follow_zoom_mode: FollowZoomMode;
  follow_zoom_offset: number;
  target_screen_px_per_sec: number;
  visibility_mode: VisibilityMode;
  trail_duration_seconds: number;
  track_color: string;
  marker_color: string;
  track_width_px: number;
  units: UnitsMode;
  intro_hold_seconds: number;
  outro_hold_seconds: number;
  show_overnight_stops: boolean;
  elevation_chart_overlay: boolean;
  show_photos: boolean;
  photo_display_seconds: number;
  photo_group_gap_seconds: number;
  pause_on_photo: boolean;
  stage_colors: Record<string, string>;
  stage_transitions: Record<string, TransitionIcon>;
  stage_camera_overrides: Record<string, StageCameraOverride>;
  statistics_panel: StatisticsPanelConfig;
  is_preview: boolean;
  color_by: ColorByMode;
  terrain_exaggeration: number;
  elevation_curtain: boolean;
  camera_keyframes: CameraKeyframe[];
  director_preset: DirectorPreset;
  show_chapter_cards: boolean;
  show_transfer_arcs: boolean;
  show_finale_poster: boolean;
  show_climb_callouts: boolean;
  climb_callout_duration_s: number;
  photo_card_style: PhotoCardStyle;
  telemetry_smoothing: number;
}

export interface IngestWarning {
  code: string;
  message: string;
  count?: number;
}

export interface StageOut {
  id: string;
  project_id: string;
  source_type: "gpx" | "strava";
  source_format?: StageFormat;
  etl_version?: number;
  activity_type?: ActivityType;
  external_source_id: string | null;
  strava_activity_id: number | null;
  name: string;
  stage_order: number;
  day_index: number;
  trim_start_s: number | null;
  trim_end_s: number | null;
  started_at: string | null;
  ended_at?: string | null;
  point_count: number;
  distance_m: number;
  elevation_gain_m: number;
  elevation_loss_m: number;
  moving_seconds: number;
  elapsed_seconds: number;
  has_timestamps: boolean;
  min_lat: number | null;
  max_lat: number | null;
  min_lon: number | null;
  max_lon: number | null;
  warnings: IngestWarning[];
  created_at: string;
}

export interface ProjectOut {
  id: string;
  title: string;
  revision: number;
  stage_count: number;
  total_distance_m: number;
  settings: RenderSpecV2;
  share_enabled?: boolean;
  share_slug?: string | null;
  share_created_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetailOut extends ProjectOut {
  stages: StageOut[];
}

export interface ProjectShareOut {
  share_enabled: boolean;
  share_slug: string | null;
  share_created_at: string | null;
}

export interface PublicStageOut {
  id: string;
  project_id: string;
  source_type: "gpx" | "strava";
  source_format?: StageFormat;
  activity_type?: ActivityType;
  name: string;
  stage_order: number;
  day_index: number;
  trim_start_s: number | null;
  trim_end_s: number | null;
  started_at: string | null;
  ended_at?: string | null;
  point_count: number;
  distance_m: number;
  elevation_gain_m: number;
  elevation_loss_m: number;
  moving_seconds: number;
  elapsed_seconds: number;
  has_timestamps: boolean;
  min_lat: number | null;
  max_lat: number | null;
  min_lon: number | null;
  max_lon: number | null;
  created_at: string;
}

export interface ProjectPhotoOut {
  id: string;
  project_id: string;
  stage_id: string | null;
  source_type: PhotoSourceType;
  width: number;
  height: number;
  byte_size: number;
  content_hash?: string | null;
  caption: string | null;
  taken_at_utc: string | null;
  lat: number | null;
  lon: number | null;
  matched_lat: number | null;
  matched_lon: number | null;
  matched_progress: number | null;
  match_method: PhotoMatchMethod;
  camera_make_model?: string | null;
  exif_offset_s?: number;
  enabled?: boolean;
  created_at: string;
}

export interface PublicPhotoOut {
  id: string;
  width: number;
  height: number;
  caption: string | null;
  taken_at_utc: string | null;
  lat: number | null;
  lon: number | null;
  matched_lat: number | null;
  matched_lon: number | null;
  matched_progress: number | null;
  match_method: PhotoMatchMethod;
  enabled?: boolean;
}

export interface ProjectPhotoUpdateRequest {
  caption?: string | null;
  taken_at_utc?: string | null;
  lat?: number | null;
  lon?: number | null;
  enabled?: boolean;
}

export interface PublicProjectOut {
  id: string;
  title: string;
  stage_count: number;
  total_distance_m: number;
  settings: RenderSpecV2;
  stages: PublicStageOut[];
  photos?: PublicPhotoOut[];
  has_rendered_video: boolean;
  latest_render_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditEventOut {
  id: number;
  project_id: string | null;
  event_type: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ProjectCreateRequest {
  title: string;
  settings?: RenderSpecV2;
}

export interface ProjectUpdateRequest {
  revision: number;
  title?: string;
  settings?: RenderSpecV2;
}

export interface StageUpdateRequest {
  name?: string;
  day_index?: number;
  trim_start_s?: number | null;
  trim_end_s?: number | null;
  activity_type?: ActivityType;
}

export interface StageReorderRequest {
  stage_ids: string[];
  day_indices?: Record<string, number>;
}

export interface StageSplitRequest {
  split_distance_m?: number | null;
  split_elapsed_s?: number | null;
  part1_name?: string | null;
  part2_name?: string | null;
}

export interface StageSplitOut {
  part1: StageOut;
  part2: StageOut;
  stages: StageOut[];
  revision: number;
}

export interface TelemetryChartPoint {
  cum_dist_m: number;
  elev_m: number | null;
  speed_mps: number;
  grade_pct: number | null;
  hr_bpm: number | null;
  watts: number | null;
}

export interface TelemetryChartOut {
  project_id: string;
  total_distance_m: number;
  points: TelemetryChartPoint[];
}

export interface TelemetryGeometryProperties {
  lod: LodLevel;
  point_count: number;
  original_point_count: number;
  segment_count: number;
  bbox: [number, number, number, number];
  crosses_antimeridian: boolean;
  stage_id: string;
  name: string;
  day_index: number;
  stage_order: number;
  source_type: "gpx" | "strava";
  activity_type?: ActivityType;
  distance_m: number;
  elevation_gain_m: number;
}

export interface TelemetryGeometryFeature {
  type: "Feature";
  id: string;
  geometry: {
    type: "MultiLineString";
    coordinates: [number, number][][];
  };
  properties: TelemetryGeometryProperties;
}

export interface TelemetryGeometryOut {
  type: "FeatureCollection";
  project_id: string;
  features: TelemetryGeometryFeature[];
}

export interface TelemetryTimelinePoint {
  cum_dist_m: number;
  /** UTC epoch milliseconds recorded for this point. */
  ts_ms: number;
}

export interface TelemetryTimelineStage {
  stage_id: string;
  stage_order: number;
  points: TelemetryTimelinePoint[];
}

export interface TelemetryTimelineOut {
  project_id: string;
  stages: TelemetryTimelineStage[];
}

export interface StageKpiOut {
  stage_id: string;
  name: string;
  day_index: number;
  point_count: number;
  distance_m: number;
  total_distance_m: number;
  ascent_m: number;
  elevation_gain_m: number;
  descent_m: number;
  elevation_loss_m: number;
  moving_time_s: number;
  elapsed_time_s: number;
  avg_speed_mps: number;
  avg_moving_speed_mps: number;
  max_speed_mps: number;
  vam: number;
  min_elev_m: number | null;
  max_elev_m: number | null;
  max_grade_pct: number | null;
  avg_hr_bpm: number | null;
  avg_watts: number | null;
}

export interface ProjectKpisOut {
  project_id: string;
  stage_count: number;
  total_distance_m: number;
  total_elevation_gain_m: number;
  total_moving_seconds: number;
  stages: StageKpiOut[];
}

export interface StravaConnectOut {
  authorize_url: string;
  state: string;
}

export interface StravaCallbackOut {
  connected: boolean;
  athlete_id: string;
  scope: string;
}

export interface StravaDisconnectOut {
  connected: boolean;
}

export interface StageAutoOrganizeRequest {
  overnight_gap_hours?: number;
  palette_preset?: PalettePreset;
  assign_overnight_icons?: boolean;
  timezone?: string;
}

export type StageAutoOrganizeOut = ProjectDetailOut;

export interface PhotoShiftClockRequest {
  offset_seconds?: number | null;
  photo_ids?: string[] | null;
  dry_run?: boolean;
  camera_make_model?: string | null;
}

export interface PhotoShiftClockOut {
  applied_offset_seconds: number;
  dry_run: boolean;
  matched_before: number;
  matched_after: number;
  newly_matched_count: number;
  photos: ProjectPhotoOut[];
}

export interface PhotoPinRequest {
  lat?: number | null;
  lon?: number | null;
  progress?: number | null;
}

export interface ClimbHighlightOut {
  id: string;
  stage_id: string;
  name: string;
  category: ClimbCategory;
  start_dist_m: number;
  end_dist_m: number;
  start_progress: number;
  end_progress: number;
  summit_progress: number;
  summit_lat: number;
  summit_lon: number;
  summit_elev_m: number;
  distance_m: number;
  elevation_gain_m: number;
  avg_grade_pct: number;
  max_grade_pct: number;
  fiets_score: number;
}

export interface MetricSplitOut {
  stage_id: string;
  split_index: number;
  split_km: number;
  distance_m: number;
  elapsed_seconds: number;
  moving_seconds: number;
  elevation_gain_m: number;
  elevation_loss_m: number;
  avg_speed_mps: number;
  avg_grade_pct: number | null;
  avg_hr_bpm: number | null;
  avg_watts: number | null;
}

export interface DwellStopOut {
  id: string;
  stage_id: string;
  lat: number;
  lon: number;
  cum_dist_m: number;
  progress: number;
  duration_seconds: number;
  started_at: string | null;
  ended_at: string | null;
  inferred_kind: string;
}

export interface PeakEffortOut {
  stage_id: string;
  metric: string;
  window_label: string;
  window_seconds: number;
  value: number;
  start_dist_m: number;
  end_dist_m: number;
}

export interface StageGeoContextOut {
  stage_id: string;
  countries: string[];
  regions: string[];
  passes: string[];
  primary_country: string | null;
}

export interface ProjectHighlightsOut {
  project_id: string;
  climbs: ClimbHighlightOut[];
  splits: MetricSplitOut[];
  dwell_stops: DwellStopOut[];
  peak_efforts: PeakEffortOut[];
  geo_context: StageGeoContextOut[];
}

export interface CareerBucketOut {
  year: number;
  project_count: number;
  stage_count: number;
  distance_m: number;
  elevation_gain_m: number;
  moving_seconds: number;
}

export interface ActivityBreakdownOut {
  activity_type: string;
  stage_count: number;
  distance_m: number;
  elevation_gain_m: number;
  moving_seconds: number;
}

export interface CareerAnalyticsOut {
  total_projects: number;
  total_stages: number;
  total_distance_m: number;
  total_elevation_gain_m: number;
  total_moving_seconds: number;
  countries_visited: string[];
  by_year: CareerBucketOut[];
  by_activity: ActivityBreakdownOut[];
}

export interface AtlasTripOut {
  project_id: string;
  title: string;
  year: number | null;
  primary_activity: string;
  distance_m: number;
  elevation_gain_m: number;
  stage_count: number;
  bbox: [number, number, number, number] | null;
  polylines: [number, number][][];
}

export interface HeatmapCellOut {
  h3_index: string;
  lat: number;
  lon: number;
  point_count: number;
  stage_count: number;
}

export interface AnalyticsHeatmapOut {
  trips: AtlasTripOut[];
  hex_cells: HeatmapCellOut[];
}

export interface StravaActivityOut {
  id: number;
  name: string;
  sport_type: string;
  start_date: string;
  distance_m: number;
  elevation_gain_m: number;
  moving_time_s: number;
  has_gps: boolean;
  summary_polyline?: string | null;
}

export interface StravaImportRequest {
  activity_id: number;
  day_index: number;
  name?: string;
  activity_type?: ActivityType;
}

export interface StravaBatchImportItem {
  activity_id: number;
  day_index?: number;
  name?: string | null;
  custom_name?: string | null;
  activity_type?: ActivityType | null;
}

export interface StravaBatchImportRequest {
  items: StravaBatchImportItem[];
}

export interface StravaBatchImportFailure {
  activity_id: number;
  detail: string;
}

export interface StravaBatchImportOut {
  imported: StageOut[];
  failed: StravaBatchImportFailure[];
}

export interface StravaImportAllRequest {
  project_id?: string | null;
  max_activities?: number;
  after?: string | null;
  before?: string | null;
  gps_only?: boolean;
}

export interface StravaImportAllOut {
  queued_count: number;
  skipped_existing_count: number;
  skipped_no_gps_count: number;
  total_scanned: number;
  project_id: string | null;
  project_title: string | null;
  queued_activity_ids: number[];
}

export interface StravaImportStatusOut {
  connected: boolean;
  athlete_id: string | null;
  scope: string | null;
  pending_jobs: number;
  queued_jobs: number;
  running_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  ignored_jobs: number;
  imported_stages_count: number;
  last_error: string | null;
}

export interface RenderJobOut {
  id: string;
  project_id: string;
  status: RenderJobStatus;
  progress_pct: number;
  attempt_count: number;
  spec: RenderSpecV2;
  idempotency_key: string;
  output_object_key: string | null;
  thumbnail_key: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface RenderManifestPoint {
  lat: number;
  lon: number;
  lon_unwrapped?: number;
  elev_m: number | null;
  cum_dist_m: number;
  speed_mps: number;
  grade_pct?: number | null;
  hr_bpm?: number | null;
  cadence?: number | null;
  watts?: number | null;
  elapsed_s: number | null;
  ts_utc: string | null;
  segment_idx: number;
}

export interface ManifestColorSegment {
  start_index: number;
  end_index: number;
  color_rgb: [number, number, number];
  bucket_label?: string;
}

export interface RenderManifestStage {
  id: string;
  name: string;
  day_index: number;
  stage_order: number;
  activity_type?: ActivityType;
  color: string;
  transition: TransitionIcon;
  distance_m: number;
  elevation_gain_m: number;
  moving_seconds: number;
  elapsed_seconds: number;
  has_timestamps: boolean;
  points: RenderManifestPoint[];
  color_segments?: ManifestColorSegment[];
}

export interface ManifestClimb {
  id: string;
  stage_id: string;
  name: string;
  category: ClimbCategory;
  start_dist_m: number;
  end_dist_m: number;
  start_progress: number;
  end_progress: number;
  summit_progress: number;
  summit_lat: number;
  summit_lon: number;
  summit_elev_m: number;
  distance_m: number;
  elevation_gain_m: number;
  avg_grade_pct: number;
  max_grade_pct: number;
  fiets_score: number;
}

export interface ManifestPhoto {
  id: string;
  caption: string | null;
  width: number;
  height: number;
  taken_at_utc: string | null;
  lat: number | null;
  lon: number | null;
  matched_lat?: number | null;
  matched_lon?: number | null;
  matched_progress?: number | null;
  match_method?: PhotoMatchMethod;
  data_uri: string;
  asset_url?: string | null;
}

export interface RenderManifest {
  job_id: string;
  project_id: string;
  project_title: string;
  spec: RenderSpecV2;
  width: number;
  height: number;
  fps: number;
  duration_s: number;
  total_frames: number;
  is_preview: boolean;
  stages: RenderManifestStage[];
  photos?: ManifestPhoto[];
  climbs?: ManifestClimb[];
}

export interface TripRendererApi {
  ready: Promise<void>;
  getFrameCount: () => number;
  setFrame: (frameIndex: number) => Promise<void>;
}

declare global {
  interface Window {
    __TRIP_RENDER_MANIFEST__?: RenderManifest;
    __TRIP_RENDERER__?: TripRendererApi;
  }
}

export function createDefaultStatisticsPanelConfig(): StatisticsPanelConfig {
  return {
    enabled: true,
    position: "top",
    time_zone: "UTC",
    text_size: 24,
    scale: 1.0,
    text_color: "#FFFFFF",
    background_color: "#0F172A",
    background_opacity: 0.75,
    metrics: ["trip-day", "distance-travelled", "speed", "elevation", "elapsed-time"],
    collision_avoidance: true,
  };
}

export function createDefaultRenderSpecV2(): RenderSpecV2 {
  return {
    version: 2,
    resolution: "1920x1080",
    fps: 30,
    map_style: "liberty",
    duration_seconds: 20.0,
    timeline_mode: "distance",
    camera_mode: "follow-marker",
    camera_preset: "flat",
    camera_bearing_deg: 0.0,
    terrain_3d: false,
    camera_pitch_deg: 0.0,
    follow_zoom_mode: "adaptive",
    follow_zoom_offset: 0.0,
    target_screen_px_per_sec: 180.0,
    visibility_mode: "traversed",
    trail_duration_seconds: 4.0,
    track_color: "#F97316",
    marker_color: "#FFFFFF",
    track_width_px: 5.0,
    units: "metric",
    intro_hold_seconds: 1.0,
    outro_hold_seconds: 1.5,
    show_overnight_stops: true,
    elevation_chart_overlay: false,
    show_photos: true,
    photo_display_seconds: 3.0,
    photo_group_gap_seconds: 1,
    pause_on_photo: false,
    stage_colors: {},
    stage_transitions: {},
    stage_camera_overrides: {},
    statistics_panel: createDefaultStatisticsPanelConfig(),
    is_preview: false,
    color_by: "stage",
    terrain_exaggeration: 1.4,
    elevation_curtain: false,
    camera_keyframes: [],
    director_preset: "none",
    show_chapter_cards: false,
    show_transfer_arcs: true,
    show_finale_poster: false,
    show_climb_callouts: true,
    climb_callout_duration_s: 4.0,
    photo_card_style: "corner",
    telemetry_smoothing: 5,
  };
}

const VALID_FPS_SET = new Set<RenderFps>([15, 30, 60]);
const VALID_RESOLUTION_SET = new Set<ResolutionPreset>([
  "1280x720",
  "1920x1080",
  "1080x1080",
  "1080x1350",
  "1080x1920",
  "720x1280",
  "720x900",
  "720x720",
]);
const VALID_MAP_STYLE_SET = new Set<MapStylePreset>([
  "liberty",
  "positron",
  "bright",
  "dark",
  "topo-relief",
  "satellite-hybrid",
  "editorial-mono",
]);
const VALID_COLOR_BY_SET = new Set<ColorByMode>(["stage", "grade", "speed", "hr", "watts"]);
const VALID_DIRECTOR_PRESET_SET = new Set<DirectorPreset>([
  "none",
  "expedition-doc",
  "climb-spotlight",
  "social-reel",
]);
const VALID_PHOTO_CARD_STYLE_SET = new Set<PhotoCardStyle>(["corner", "polaroid", "cinematic", "cinematic_stop"]);
const VALID_STATISTIC_METRIC_SET = new Set<StatisticMetric>(ALL_STATISTIC_METRICS);
const VALID_STAGE_FOCUS_MODE_SET = new Set<StageFocusMode>(["auto", "close", "street", "custom"]);

function clampFinite(value: unknown, min: number, max: number, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return fallback;
  }
  return Math.min(max, Math.max(min, value));
}

function normalizeStageCameraOverride(rawOverride: unknown): StageCameraOverride | null {
  if (!rawOverride || typeof rawOverride !== "object") {
    return null;
  }
  const ov = rawOverride as Partial<StageCameraOverride>;
  const focusMode: StageFocusMode =
    typeof ov.focus_mode === "string" &&
    VALID_STAGE_FOCUS_MODE_SET.has(ov.focus_mode as StageFocusMode)
      ? (ov.focus_mode as StageFocusMode)
      : "auto";
  const transitionMargin = clampFinite(ov.transition_margin, 0.005, 0.25, 0.02);
  let targetZoom: number | null =
    typeof ov.target_zoom === "number" && Number.isFinite(ov.target_zoom)
      ? clampFinite(ov.target_zoom, 3.0, 18.0, 15.8)
      : null;
  let speedMultiplier = clampFinite(ov.speed_multiplier, 0.05, 3.0, 1.0);

  if (focusMode === "street") {
    if (targetZoom === null) {
      targetZoom = 15.8;
    }
    if (speedMultiplier === 1.0) {
      speedMultiplier = 0.2;
    }
  } else if (focusMode === "close") {
    if (targetZoom === null) {
      targetZoom = 13.8;
    }
    if (speedMultiplier === 1.0) {
      speedMultiplier = 0.7;
    }
  } else if (focusMode === "auto") {
    targetZoom = null;
    speedMultiplier = 1.0;
  }

  return {
    focus_mode: focusMode,
    target_zoom: targetZoom,
    speed_multiplier: speedMultiplier,
    transition_margin: transitionMargin,
  };
}

function normalizeCameraKeyframe(raw: unknown): CameraKeyframe | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const kf = raw as Partial<CameraKeyframe>;
  if (typeof kf.progress !== "number" || !Number.isFinite(kf.progress)) {
    return null;
  }
  const cameraMode: CameraMode = kf.camera_mode === "fit-track" ? "fit-track" : "follow-marker";
  const cameraPreset: CameraPreset =
    kf.camera_preset === "flat" || kf.camera_preset === "drone-orbit"
      ? kf.camera_preset
      : "dynamic-3d";
  return {
    progress: clampFinite(kf.progress, 0, 1, 0),
    camera_mode: cameraMode,
    camera_preset: cameraPreset,
    pitch_deg: clampFinite(kf.pitch_deg, 0, 60, 45),
    bearing_offset_deg: clampFinite(kf.bearing_offset_deg, -180, 180, 0),
    zoom_offset: clampFinite(kf.zoom_offset, -3, 3, 0),
    speed_multiplier: clampFinite(kf.speed_multiplier, 0.25, 4.0, 1.0),
    label: typeof kf.label === "string" ? kf.label.slice(0, 64) : "",
  };
}

export function normalizeRenderSpecV2(raw?: Partial<RenderSpecV2> | null): RenderSpecV2 {
  const defaults = createDefaultRenderSpecV2();
  if (!raw || typeof raw !== "object") {
    return defaults;
  }
  const panelDefaults = defaults.statistics_panel;
  const rawPanel = raw.statistics_panel;
  const fps: RenderFps =
    typeof raw.fps === "number" && VALID_FPS_SET.has(raw.fps as RenderFps)
      ? (raw.fps as RenderFps)
      : defaults.fps;
  const resolution: ResolutionPreset =
    typeof raw.resolution === "string" && VALID_RESOLUTION_SET.has(raw.resolution as ResolutionPreset)
      ? (raw.resolution as ResolutionPreset)
      : defaults.resolution;
  const mapStyle: MapStylePreset =
    typeof raw.map_style === "string" && VALID_MAP_STYLE_SET.has(raw.map_style as MapStylePreset)
      ? (raw.map_style as MapStylePreset)
      : defaults.map_style;
  const cameraPreset: CameraPreset =
    raw.camera_preset === "dynamic-3d" || raw.camera_preset === "drone-orbit"
      ? raw.camera_preset
      : "flat";
  const cameraBearing = clampFinite(raw.camera_bearing_deg, -180, 180, 0.0);
  const cameraPitch = clampFinite(raw.camera_pitch_deg, 0, 60, defaults.camera_pitch_deg);
  const durationSeconds = clampFinite(raw.duration_seconds, 3, 180, defaults.duration_seconds);
  const followZoomMode: FollowZoomMode =
    raw.follow_zoom_mode === "fixed" ? "fixed" : "adaptive";
  const followZoomOffset = clampFinite(raw.follow_zoom_offset, -3, 3, defaults.follow_zoom_offset);
  const targetScreenPxPerSec = clampFinite(
    raw.target_screen_px_per_sec,
    80,
    500,
    defaults.target_screen_px_per_sec,
  );
  const photoDisplaySeconds = clampFinite(
    raw.photo_display_seconds,
    0.5,
    10,
    defaults.photo_display_seconds,
  );
  const colorBy: ColorByMode =
    typeof raw.color_by === "string" && VALID_COLOR_BY_SET.has(raw.color_by as ColorByMode)
      ? (raw.color_by as ColorByMode)
      : defaults.color_by;
  const terrainExaggeration = clampFinite(
    raw.terrain_exaggeration,
    0.5,
    3.0,
    defaults.terrain_exaggeration,
  );
  const directorPreset: DirectorPreset =
    typeof raw.director_preset === "string" &&
    VALID_DIRECTOR_PRESET_SET.has(raw.director_preset as DirectorPreset)
      ? (raw.director_preset as DirectorPreset)
      : defaults.director_preset;
  const photoCardStyle: PhotoCardStyle =
    typeof raw.photo_card_style === "string" &&
    VALID_PHOTO_CARD_STYLE_SET.has(raw.photo_card_style as PhotoCardStyle)
      ? (raw.photo_card_style as PhotoCardStyle)
      : defaults.photo_card_style;
  const telemetrySmoothing = Math.round(
    clampFinite(raw.telemetry_smoothing, 1, 31, defaults.telemetry_smoothing),
  );
  const climbCalloutDurationS = clampFinite(
    raw.climb_callout_duration_s,
    0,
    30,
    defaults.climb_callout_duration_s,
  );
  const stageCameraOverrides: Record<string, StageCameraOverride> = {};
  if (raw.stage_camera_overrides && typeof raw.stage_camera_overrides === "object") {
    for (const [stageId, rawOverride] of Object.entries(raw.stage_camera_overrides)) {
      const normalizedOverride = normalizeStageCameraOverride(rawOverride);
      if (normalizedOverride) {
        stageCameraOverrides[stageId] = normalizedOverride;
      }
    }
  }
  const cameraKeyframes = Array.isArray(raw.camera_keyframes)
    ? raw.camera_keyframes
        .slice(0, 40)
        .map(normalizeCameraKeyframe)
        .filter((kf): kf is CameraKeyframe => kf !== null)
        .sort((a, b) => a.progress - b.progress)
    : [];
  const filteredMetrics = Array.isArray(rawPanel?.metrics)
    ? rawPanel.metrics.filter((m): m is StatisticMetric => VALID_STATISTIC_METRIC_SET.has(m))
    : [];

  return {
    ...defaults,
    ...raw,
    version: 2,
    resolution,
    fps,
    map_style: mapStyle,
    duration_seconds: durationSeconds,
    camera_preset: cameraPreset,
    camera_bearing_deg: cameraBearing,
    camera_pitch_deg: cameraPitch,
    follow_zoom_mode: followZoomMode,
    follow_zoom_offset: followZoomOffset,
    target_screen_px_per_sec: targetScreenPxPerSec,
    show_photos: typeof raw.show_photos === "boolean" ? raw.show_photos : defaults.show_photos,
    photo_display_seconds: photoDisplaySeconds,
    photo_group_gap_seconds: clampFinite(raw.photo_group_gap_seconds, 0, 180, defaults.photo_group_gap_seconds),
    pause_on_photo:
      typeof raw.pause_on_photo === "boolean" ? raw.pause_on_photo : defaults.pause_on_photo,
    color_by: colorBy,
    terrain_exaggeration: terrainExaggeration,
    elevation_curtain:
      typeof raw.elevation_curtain === "boolean"
        ? raw.elevation_curtain
        : defaults.elevation_curtain,
    camera_keyframes: cameraKeyframes,
    director_preset: directorPreset,
    show_chapter_cards:
      typeof raw.show_chapter_cards === "boolean"
        ? raw.show_chapter_cards
        : defaults.show_chapter_cards,
    show_transfer_arcs:
      typeof raw.show_transfer_arcs === "boolean"
        ? raw.show_transfer_arcs
        : defaults.show_transfer_arcs,
    show_finale_poster:
      typeof raw.show_finale_poster === "boolean"
        ? raw.show_finale_poster
        : defaults.show_finale_poster,
    show_climb_callouts:
      typeof raw.show_climb_callouts === "boolean"
        ? raw.show_climb_callouts
        : defaults.show_climb_callouts,
    climb_callout_duration_s: climbCalloutDurationS,
    photo_card_style: photoCardStyle,
    telemetry_smoothing: telemetrySmoothing,
    stage_colors: raw.stage_colors ? { ...raw.stage_colors } : {},
    stage_transitions: raw.stage_transitions ? { ...raw.stage_transitions } : {},
    stage_camera_overrides: stageCameraOverrides,
    statistics_panel: {
      ...panelDefaults,
      ...(rawPanel ?? {}),
      collision_avoidance:
        typeof rawPanel?.collision_avoidance === "boolean"
          ? rawPanel.collision_avoidance
          : panelDefaults.collision_avoidance,
      metrics: filteredMetrics.length > 0 ? filteredMetrics : [...panelDefaults.metrics],
    },
  };
}
