import { ArrowDown, ArrowUp, Camera, Plus, Sliders, Sparkles, Trash2 } from "lucide-react";
import type {
  CameraKeyframe,
  CameraMode,
  CameraPreset,
  ColorByMode,
  DirectorPreset,
  FollowZoomMode,
  InvalidRenderSpecValue,
  MapStylePreset,
  PanelPosition,
  PhotoCardStyle,
  RenderFps,
  RenderSpecV2,
  ResolutionPreset,
  StatisticMetric,
  StatisticsPanelConfig,
  TimelineMode,
  UnitsMode,
  VisibilityMode,
} from "../../lib/api/types";
import { ALL_STATISTIC_METRICS } from "../../lib/api/types";
import { generateDirectorKeyframes } from "../../lib/geo/director";
import type { InspectorSection } from "../../lib/state/editor-store";
import { ExportPanel } from "./ExportPanel";
import type { LocalExportSource } from "./LocalExportControls";

const INSPECTOR_TABS: readonly { id: InspectorSection; label: string }[] = [
  { id: "route", label: "Route" },
  { id: "camera", label: "Camera" },
  { id: "hud", label: "HUD" },
  { id: "export", label: "Export" },
];

const METRIC_LABELS: Record<StatisticMetric, string> = {
  date: "Date",
  time: "Local time",
  "trip-day": "Trip day",
  "elapsed-time": "Elapsed time",
  speed: "Speed",
  elevation: "Elevation",
  "distance-travelled": "Distance travelled",
  "distance-remaining": "Distance remaining",
  grade: "Grade",
  hr: "Heart rate",
  power: "Power",
  vam: "VAM",
};

const DIRECTOR_PRESETS: readonly {
  id: DirectorPreset;
  label: string;
  hint: string;
}[] = [
  { id: "none", label: "Manual", hint: "Custom static / manual cues" },
  { id: "expedition-doc", label: "Expedition Doc", hint: "Fly-in, 3D chase, summit orbit" },
  { id: "social-reel", label: "Social Reel", hint: "Punchy fast-paced orbit cuts" },
];

const CAMERA_PRESETS: readonly {
  id: CameraPreset;
  label: string;
  hint: string;
}[] = [
  { id: "flat", label: "Flat", hint: "Top-down 2D" },
  { id: "dynamic-3d", label: "Dynamic 3D", hint: "Course-ahead chase" },
  { id: "drone-orbit", label: "Drone Orbit", hint: "3D sweeping azimuth" },
];

const COMMON_TIMEZONES = [
  "UTC",
  "Europe/Berlin",
  "Europe/Rome",
  "Europe/Zurich",
  "Europe/London",
  "America/New_York",
  "America/Denver",
  "America/Los_Angeles",
  "Asia/Tokyo",
  "Australia/Sydney",
] as const;

export interface InspectorPanelProps {
  projectId: string;
  section: InspectorSection;
  spec: RenderSpecV2;
  stageCount: number;
  dayCount?: number;
  currentProgress?: number;
  getCurrentProgress?: () => number;
  onSelectSection: (section: InspectorSection) => void;
  onUpdateSpec: (patch: Partial<RenderSpecV2>) => void;
  onUpdateHud: (patch: Partial<StatisticsPanelConfig>) => void;
  onApplyDirectorPreset?: (preset: DirectorPreset) => void;
  onFlushSave: () => Promise<boolean>;
  localExportSource?: LocalExportSource;
  invalidSpecValues?: InvalidRenderSpecValue[];
}

const INVALID_SPEC_FIELD_LABEL: Record<InvalidRenderSpecValue["field"], string> = {
  resolution: "Output resolution",
  fps: "Frame rate",
};

export function InspectorPanel({
  projectId,
  section,
  spec,
  stageCount,
  dayCount,
  currentProgress = 0,
  getCurrentProgress,
  onSelectSection,
  onUpdateSpec,
  onUpdateHud,
  onApplyDirectorPreset,
  onFlushSave,
  localExportSource,
  invalidSpecValues = [],
}: InspectorPanelProps) {
  const hud = spec.statistics_panel;
  const effectiveDays = Math.max(1, dayCount ?? stageCount);

  function handleSelectDirectorPreset(preset: DirectorPreset) {
    if (onApplyDirectorPreset) {
      onApplyDirectorPreset(preset);
      return;
    }
    if (preset === "none") {
      onUpdateSpec({ director_preset: "none", camera_keyframes: [] });
      return;
    }
    const generated = generateDirectorKeyframes(preset, { stages: [], spec });
    onUpdateSpec({ director_preset: preset, camera_keyframes: generated });
  }

  function handleAddKeyframe() {
    const rawProgress = getCurrentProgress ? getCurrentProgress() : currentProgress;
    const clampedProgress = Math.round(Math.min(1, Math.max(0, rawProgress)) * 1000) / 1000;
    const nextKf: CameraKeyframe = {
      progress: clampedProgress,
      camera_mode: spec.camera_mode,
      camera_preset: spec.camera_preset,
      pitch_deg: spec.camera_pitch_deg,
      bearing_offset_deg: spec.camera_bearing_deg,
      zoom_offset: 0,
      speed_multiplier: 1,
      label: `Cue ${spec.camera_keyframes.length + 1}`,
    };
    onUpdateSpec({
      director_preset: "none",
      camera_keyframes: [...spec.camera_keyframes, nextKf],
    });
  }

  function handleUpdateKeyframe(idx: number, patch: Partial<CameraKeyframe>) {
    const next = spec.camera_keyframes.map((kf, i) => (i === idx ? { ...kf, ...patch } : kf));
    onUpdateSpec({ camera_keyframes: next });
  }

  function handleRemoveKeyframe(idx: number) {
    const next = spec.camera_keyframes.filter((_, i) => i !== idx);
    onUpdateSpec({ camera_keyframes: next });
  }

  function toggleMetric(metric: StatisticMetric) {
    const exists = hud.metrics.includes(metric);
    const next = exists ? hud.metrics.filter((m) => m !== metric) : [...hud.metrics, metric];
    onUpdateHud({ metrics: next });
  }

  function moveMetric(metric: StatisticMetric, direction: -1 | 1) {
    const idx = hud.metrics.indexOf(metric);
    const target = idx + direction;
    if (idx < 0 || target < 0 || target >= hud.metrics.length) return;
    const next = [...hud.metrics];
    const [item] = next.splice(idx, 1);
    next.splice(target, 0, item!);
    onUpdateHud({ metrics: next });
  }

  const orderedMetrics = [
    ...hud.metrics,
    ...ALL_STATISTIC_METRICS.filter((m) => !hud.metrics.includes(m)),
  ];

  return (
    <aside
      aria-label="Animation and render inspector"
      className="flex min-h-0 flex-col border-t border-border bg-surface lg:col-span-3 lg:border-t-0 lg:border-l"
    >
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <Sliders className="h-4 w-4 text-sky-accent" aria-hidden="true" />
        <span className="text-xs font-semibold text-ink">Inspector</span>
      </div>

      <div
        role="tablist"
        aria-label="Inspector sections"
        className="grid grid-cols-4 border-b border-border bg-canvas/50 p-1"
      >
        {INSPECTOR_TABS.map((tab) => {
          const active = section === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onSelectSection(tab.id)}
              className={`rounded py-1.5 text-xs font-medium transition-colors ${
                active ? "bg-elevated text-sky-accent" : "text-ink-muted hover:text-ink"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4 text-xs">
        {section === "route" ? (
          <div className="space-y-4">
            <label className="block space-y-1.5">
              <span className="block font-medium text-ink-muted">Map style</span>
              <select
                aria-label="Map style"
                value={spec.map_style}
                onChange={(e) => onUpdateSpec({ map_style: e.currentTarget.value as MapStylePreset })}
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="liberty">OpenFreeMap Liberty</option>
                <option value="positron">OpenFreeMap Positron</option>
                <option value="bright">OpenFreeMap Bright</option>
                <option value="dark">OpenFreeMap Dark</option>
                <option value="topo-relief">Topo Relief (3D DEM)</option>
                <option value="satellite-hybrid">Satellite Hybrid</option>
                <option value="editorial-mono">Editorial Monochrome</option>
              </select>
            </label>

            <label className="block space-y-1.5">
              <span className="block font-medium text-ink-muted">Trip place labels</span>
              <select
                value={spec.settlement_label_mode}
                onChange={(e) => onUpdateSpec({
                  settlement_label_mode: e.currentTarget.value === "follow_zoom" ? "follow_zoom" : "uniform",
                })}
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="uniform">Uniform across zoom levels</option>
                <option value="follow_zoom">Follow map zoom</option>
              </select>
              <span className="block text-ink-muted">
                Uniform keeps town and village labels consistent across zoom changes on supported vector maps.
              </span>
            </label>

            <label className="block space-y-1.5">
              <span className="block font-medium text-ink-muted">Color route by</span>
              <select
                aria-label="Color route by"
                value={spec.color_by}
                onChange={(e) => onUpdateSpec({ color_by: e.currentTarget.value as ColorByMode })}
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="stage">Stage color</option>
                <option value="grade">Slope grade (%)</option>
                <option value="speed">Speed</option>
                <option value="hr">Heart rate (bpm)</option>
                <option value="watts">Power (W)</option>
              </select>
            </label>

            <label className="block space-y-1.5">
              <span className="block font-medium text-ink-muted">Timeline pacing</span>
              <select
                value={spec.timeline_mode}
                onChange={(e) =>
                  onUpdateSpec({ timeline_mode: e.currentTarget.value as TimelineMode })
                }
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="distance">Constant distance speed</option>
                <option value="recorded-time">Recorded GPS timestamps</option>
              </select>
            </label>

            <label className="block space-y-1.5">
              <span className="block font-medium text-ink-muted">Route visibility</span>
              <select
                value={spec.visibility_mode}
                onChange={(e) =>
                  onUpdateSpec({ visibility_mode: e.currentTarget.value as VisibilityMode })
                }
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="traversed">Progressive traversed path</option>
                <option value="trail">Animated comet trail</option>
                <option value="full">Full route static</option>
              </select>
            </label>

            {spec.visibility_mode === "trail" ? (
              <label className="block space-y-1">
                <div className="flex justify-between text-ink-muted">
                  <span>Trail window</span>
                  <span className="tabular-telemetry text-ink">{spec.trail_duration_seconds}s</span>
                </div>
                <input
                  type="range"
                  min={1}
                  max={30}
                  step={0.5}
                  value={spec.trail_duration_seconds}
                  onChange={(e) =>
                    onUpdateSpec({ trail_duration_seconds: Number(e.currentTarget.value) })
                  }
                  className="w-full accent-sky-accent"
                />
              </label>
            ) : null}

            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="block font-medium text-ink-muted">Default track</span>
                <div className="flex items-center gap-2 rounded-md border border-border bg-elevated px-2 py-1.5">
                  <input
                    type="color"
                    value={spec.track_color}
                    onChange={(e) => onUpdateSpec({ track_color: e.currentTarget.value })}
                    className="h-5 w-6 cursor-pointer border-0 bg-transparent p-0"
                  />
                  <span className="tabular-telemetry text-[11px] text-ink">{spec.track_color}</span>
                </div>
              </label>

              <label className="block space-y-1">
                <span className="block font-medium text-ink-muted">Marker color</span>
                <div className="flex items-center gap-2 rounded-md border border-border bg-elevated px-2 py-1.5">
                  <input
                    type="color"
                    value={spec.marker_color}
                    onChange={(e) => onUpdateSpec({ marker_color: e.currentTarget.value })}
                    className="h-5 w-6 cursor-pointer border-0 bg-transparent p-0"
                  />
                  <span className="tabular-telemetry text-[11px] text-ink">{spec.marker_color}</span>
                </div>
              </label>
            </div>

            <label className="block space-y-1">
              <div className="flex justify-between text-ink-muted">
                <span>Track width</span>
                <span className="tabular-telemetry text-ink">{spec.track_width_px} px</span>
              </div>
              <input
                type="range"
                min={2}
                max={16}
                step={0.5}
                value={spec.track_width_px}
                onChange={(e) => onUpdateSpec({ track_width_px: Number(e.currentTarget.value) })}
                className="w-full accent-sky-accent"
              />
            </label>

            <label className="block space-y-1.5">
              <span className="block font-medium text-ink-muted">Measurement units</span>
              <select
                value={spec.units}
                onChange={(e) => onUpdateSpec({ units: e.currentTarget.value as UnitsMode })}
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="metric">Metric (km, m, km/h)</option>
                <option value="imperial">Imperial (mi, ft, mph)</option>
              </select>
            </label>

            <label className="block space-y-1">
              <div className="flex justify-between text-ink-muted">
                <span>Telemetry smoothing</span>
                <span className="tabular-telemetry text-ink">
                  {spec.telemetry_smoothing <= 1 ? "Raw" : `${spec.telemetry_smoothing} pts`}
                </span>
              </div>
              <input
                type="range"
                aria-label="Telemetry smoothing"
                min={1}
                max={31}
                step={2}
                value={spec.telemetry_smoothing}
                onChange={(e) =>
                  onUpdateSpec({ telemetry_smoothing: Number(e.currentTarget.value) })
                }
                className="w-full accent-sky-accent"
              />
            </label>

            <div className="space-y-2 border-t border-border pt-3">
              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-ink">Show overnight / transition stops</span>
                <input
                  type="checkbox"
                  checked={spec.show_overnight_stops}
                  onChange={(e) =>
                    onUpdateSpec({ show_overnight_stops: e.currentTarget.checked })
                  }
                  className="accent-sky-accent"
                />
              </label>

              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-ink">Elevation mini-chart overlay</span>
                <input
                  type="checkbox"
                  checked={spec.elevation_chart_overlay}
                  onChange={(e) =>
                    onUpdateSpec({ elevation_chart_overlay: e.currentTarget.checked })
                  }
                  className="accent-sky-accent"
                />
              </label>

              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-ink">Show climb &amp; summit callouts</span>
                <input
                  type="checkbox"
                  checked={spec.show_climb_callouts}
                  onChange={(e) =>
                    onUpdateSpec({ show_climb_callouts: e.currentTarget.checked })
                  }
                  className="accent-sky-accent"
                />
              </label>

              {spec.show_climb_callouts ? (
                <div
                  data-testid="climb-callout-duration-controls"
                  className="space-y-1.5 rounded-md border border-border/70 bg-canvas/50 p-2.5"
                >
                  <div className="flex items-center justify-between gap-2 text-ink-muted">
                    <span>Tag display duration</span>
                    <div className="flex items-center gap-1.5">
                      <span className="tabular-telemetry text-ink">
                        {spec.climb_callout_duration_s <= 0
                          ? "Always"
                          : `${spec.climb_callout_duration_s}s`}
                      </span>
                      <button
                        type="button"
                        aria-pressed={spec.climb_callout_duration_s <= 0}
                        onClick={() =>
                          onUpdateSpec({
                            climb_callout_duration_s:
                              spec.climb_callout_duration_s <= 0 ? 4.0 : 0,
                          })
                        }
                        className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold transition-colors ${
                          spec.climb_callout_duration_s <= 0
                            ? "border-sky-accent bg-sky-accent/15 text-sky-accent"
                            : "border-border bg-elevated text-ink-muted hover:text-ink"
                        }`}
                      >
                        Always
                      </button>
                    </div>
                  </div>
                  <input
                    type="range"
                    aria-label="Climb tag display duration"
                    min={0}
                    max={15}
                    step={0.5}
                    value={Math.min(15, Math.max(0, spec.climb_callout_duration_s))}
                    onChange={(e) =>
                      onUpdateSpec({ climb_callout_duration_s: Number(e.currentTarget.value) })
                    }
                    className="w-full accent-sky-accent"
                  />
                </div>
              ) : null}

              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-ink">Show transfer arcs (&gt;2 km gaps)</span>
                <input
                  type="checkbox"
                  checked={spec.show_transfer_arcs}
                  onChange={(e) =>
                    onUpdateSpec({ show_transfer_arcs: e.currentTarget.checked })
                  }
                  className="accent-sky-accent"
                />
              </label>

              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-ink">Show stage chapter cards</span>
                <input
                  type="checkbox"
                  checked={spec.show_chapter_cards}
                  onChange={(e) =>
                    onUpdateSpec({ show_chapter_cards: e.currentTarget.checked })
                  }
                  className="accent-sky-accent"
                />
              </label>

              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-ink">Show trip finale poster</span>
                <input
                  type="checkbox"
                  checked={spec.show_finale_poster}
                  onChange={(e) =>
                    onUpdateSpec({ show_finale_poster: e.currentTarget.checked })
                  }
                  className="accent-sky-accent"
                />
              </label>

              <label className="flex items-center justify-between cursor-pointer">
                <span className="text-ink">Show photo stops</span>
                <input
                  type="checkbox"
                  checked={spec.show_photos}
                  onChange={(e) => onUpdateSpec({ show_photos: e.currentTarget.checked })}
                  className="accent-sky-accent"
                />
              </label>

              {spec.show_photos ? (
                <div className="space-y-2.5 rounded-md border border-border/70 bg-canvas/50 p-2.5">
                  <label className="block space-y-1">
                    <span className="block font-medium text-ink-muted">Photo card style</span>
                    <select
                      aria-label="Photo card style"
                      value={spec.photo_card_style}
                      onChange={(e) =>
                        onUpdateSpec({ photo_card_style: e.currentTarget.value as PhotoCardStyle })
                      }
                      className="w-full rounded-md border border-border bg-elevated px-2 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
                    >
                      <option value="corner">Corner card</option>
                      <option value="polaroid">Polaroid + pin leader</option>
                      <option value="cinematic">Cinematic Ken Burns</option>
                      <option value="cinematic_stop">Cinematic Photo Stop</option>
                    </select>
                  </label>

                  <label className="block space-y-1">
                    <div className="flex justify-between text-ink-muted">
                      <span>Photo display duration</span>
                      <span className="tabular-telemetry text-ink">{spec.photo_display_seconds}s</span>
                    </div>
                    <input
                      type="range"
                      min={0.5}
                      max={10}
                      step={0.5}
                      value={spec.photo_display_seconds}
                      onChange={(e) =>
                        onUpdateSpec({ photo_display_seconds: Number(e.currentTarget.value) })
                      }
                      className="w-full accent-sky-accent"
                    />
                  </label>

                  {spec.photo_card_style === "cinematic_stop" ? (
                    <label className="flex items-center justify-between gap-2">
                      <span className="text-ink">Group photos within (seconds)</span>
                      <input
                        type="number"
                        min={0}
                        max={180}
                        step="any"
                        value={spec.photo_group_gap_seconds}
                        onChange={(e) => {
                          const gap = e.currentTarget.valueAsNumber;
                          if (Number.isFinite(gap)) onUpdateSpec({ photo_group_gap_seconds: Math.min(180, Math.max(0, gap)) });
                        }}
                        className="w-20 rounded-md border border-border bg-elevated px-2 py-1 text-ink"
                      />
                    </label>
                  ) : <label className="flex items-center justify-between cursor-pointer">
                    <span className="text-ink">Pause timeline on photos</span>
                    <input
                      type="checkbox"
                      checked={spec.pause_on_photo}
                      onChange={(e) => onUpdateSpec({ pause_on_photo: e.currentTarget.checked })}
                      className="accent-sky-accent"
                    />
                  </label>}
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        {section === "camera" ? (
          <div className="space-y-4">
            {invalidSpecValues.length > 0 ? (
              <div role="alert" className="space-y-1 rounded-md border border-amber-500/60 bg-amber-500/10 px-2.5 py-2 text-xs text-ink">
                {invalidSpecValues.map((value) => (
                  <p key={value.field}>
                    {INVALID_SPEC_FIELD_LABEL[value.field]} &quot;{String(value.value)}&quot; is not supported and shows as {String(value.fallback)} until you pick one.
                  </p>
                ))}
              </div>
            ) : null}
            <label className="block space-y-1.5">
              <span className="block font-medium text-ink-muted">Output aspect &amp; resolution</span>
              <select
                value={spec.resolution}
                onChange={(e) =>
                  onUpdateSpec({ resolution: e.currentTarget.value as ResolutionPreset })
                }
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="1920x1080">1920×1080 (16:9 Full HD)</option>
                <option value="1280x720">1280×720 (16:9 HD)</option>
                <option value="1080x1080">1080×1080 (1:1 Square)</option>
                <option value="1080x1350">1080×1350 (4:5 Portrait)</option>
                <option value="1080x1920">1080×1920 (9:16 Vertical)</option>
                <option value="720x1280">720×1280 (9:16 HD Vertical)</option>
                <option value="720x900">720×900 (4:5 Portrait)</option>
                <option value="720x720">720×720 (1:1 Square)</option>
              </select>
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1.5">
                <span className="block font-medium text-ink-muted">Frame rate</span>
                <select
                  value={spec.fps}
                  onChange={(e) => onUpdateSpec({ fps: Number(e.currentTarget.value) as RenderFps })}
                  className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
                >
                  <option value={15}>15 fps</option>
                  <option value={30}>30 fps</option>
                  <option value={60}>60 fps</option>
                </select>
              </label>

              <label className="block space-y-1.5">
                <span className="block font-medium text-ink-muted">Duration (s)</span>
                <input
                  type="number"
                  min={5}
                  max={180}
                  value={spec.duration_seconds}
                  onChange={(e) => {
                    const next = Math.min(180, Math.max(5, Number(e.currentTarget.value) || 20));
                    onUpdateSpec({ duration_seconds: next });
                  }}
                  className="tabular-telemetry w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
                />
              </label>
            </div>

            <div className="space-y-1">
              <div className="flex items-center justify-between text-ink-muted">
                <span>Animation duration</span>
                <div className="flex items-center gap-2">
                  {effectiveDays > 1 ? (
                    <button
                      type="button"
                      onClick={() => {
                        const suggested = Math.min(180, Math.max(20, Math.round(effectiveDays * 8)));
                        onUpdateSpec({ duration_seconds: suggested });
                      }}
                      className="rounded border border-sky-accent/40 bg-sky-accent/10 px-1.5 py-0.5 text-[10px] font-semibold text-sky-accent hover:bg-sky-accent/20"
                      title={`Auto-fit duration for ${effectiveDays}-day trip`}
                    >
                      Auto-fit
                    </button>
                  ) : null}
                  <span className="tabular-telemetry text-ink">{spec.duration_seconds}s</span>
                </div>
              </div>
              <input
                type="range"
                aria-label="Animation duration"
                min={5}
                max={180}
                step={1}
                value={spec.duration_seconds}
                onChange={(e) => onUpdateSpec({ duration_seconds: Number(e.currentTarget.value) })}
                className="w-full accent-sky-accent"
              />
            </div>

            <label className="block space-y-1.5">
              <span className="flex items-center gap-1.5 font-medium text-ink-muted">
                <Camera className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Camera mode</span>
              </span>
              <select
                value={spec.camera_mode}
                onChange={(e) => onUpdateSpec({ camera_mode: e.currentTarget.value as CameraMode })}
                className="w-full rounded-md border border-border bg-elevated px-2.5 py-2 text-xs text-ink focus:border-sky-accent focus:outline-none"
              >
                <option value="follow-marker">Follow marker (dynamic zoom)</option>
                <option value="fit-track">Fit full trip bounds</option>
              </select>
            </label>

            {spec.camera_mode === "follow-marker" ? (
              <div className="space-y-3 rounded-md border border-border/70 bg-canvas/50 p-2.5">
                <label className="block space-y-1">
                  <span className="block font-medium text-ink-muted">Follow zoom mode</span>
                  <select
                    value={spec.follow_zoom_mode}
                    onChange={(e) =>
                      onUpdateSpec({ follow_zoom_mode: e.currentTarget.value as FollowZoomMode })
                    }
                    className="w-full rounded-md border border-border bg-elevated px-2 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
                  >
                    <option value="adaptive">Adaptive velocity zoom (multi-day smooth)</option>
                    <option value="fixed">Fixed bounds zoom</option>
                  </select>
                </label>

                <label className="block space-y-1">
                  <div className="flex justify-between text-ink-muted">
                    <span>Follow zoom offset</span>
                    <span className="tabular-telemetry text-ink">
                      {spec.follow_zoom_offset >= 0 ? `+${spec.follow_zoom_offset}` : spec.follow_zoom_offset}
                    </span>
                  </div>
                  <input
                    type="range"
                    min={-3}
                    max={3}
                    step={0.5}
                    value={spec.follow_zoom_offset}
                    onChange={(e) =>
                      onUpdateSpec({ follow_zoom_offset: Number(e.currentTarget.value) })
                    }
                    className="w-full accent-sky-accent"
                  />
                </label>

                {spec.follow_zoom_mode === "adaptive" ? (
                  <label className="block space-y-1">
                    <div className="flex justify-between text-ink-muted">
                      <span>Target pan speed</span>
                      <span className="tabular-telemetry text-ink">
                        {spec.target_screen_px_per_sec} px/s
                      </span>
                    </div>
                    <input
                      type="range"
                      min={80}
                      max={500}
                      step={10}
                      value={spec.target_screen_px_per_sec}
                      onChange={(e) =>
                        onUpdateSpec({ target_screen_px_per_sec: Number(e.currentTarget.value) })
                      }
                      className="w-full accent-sky-accent"
                    />
                  </label>
                ) : null}
              </div>
            ) : null}

            <div className="space-y-1.5">
              <span className="block font-medium text-ink-muted">3D Camera Preset</span>
              <div className="grid grid-cols-3 gap-1.5" role="group" aria-label="Camera preset">
                {CAMERA_PRESETS.map((preset) => {
                  const active = spec.camera_preset === preset.id;
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      aria-pressed={active}
                      onClick={() => {
                        if (preset.id === "flat") {
                          onUpdateSpec({
                            camera_preset: "flat",
                            terrain_3d: false,
                            camera_pitch_deg: 0,
                            camera_bearing_deg: 0,
                          });
                        } else if (preset.id === "dynamic-3d") {
                          onUpdateSpec({
                            camera_preset: "dynamic-3d",
                            terrain_3d: true,
                            camera_pitch_deg: Math.max(spec.camera_pitch_deg, 45),
                          });
                        } else {
                          onUpdateSpec({
                            camera_preset: "drone-orbit",
                            terrain_3d: true,
                            camera_pitch_deg: Math.max(spec.camera_pitch_deg, 50),
                          });
                        }
                      }}
                      className={`rounded-md border px-2 py-1.5 text-left transition-colors ${
                        active
                          ? "border-sky-accent bg-sky-accent/15 text-ink"
                          : "border-border bg-elevated/60 text-ink-muted hover:text-ink"
                      }`}
                    >
                      <div className="text-[11px] font-semibold leading-tight">{preset.label}</div>
                      <div className="mt-0.5 text-[9px] opacity-75 leading-tight">{preset.hint}</div>
                    </button>
                  );
                })}
              </div>
            </div>

            <label className="flex items-center justify-between cursor-pointer pt-1">
              <span className="text-ink">3D buildings &amp; horizon</span>
              <input
                type="checkbox"
                checked={spec.terrain_3d}
                onChange={(e) =>
                  onUpdateSpec({
                    terrain_3d: e.currentTarget.checked,
                    camera_pitch_deg: e.currentTarget.checked
                      ? Math.max(spec.camera_pitch_deg, 38)
                      : 0,
                  })
                }
                className="accent-sky-accent"
              />
            </label>

            <label className="block space-y-1">
              <div className="flex justify-between text-ink-muted">
                <span>Terrain exaggeration</span>
                <span className="tabular-telemetry text-ink">{spec.terrain_exaggeration.toFixed(1)}×</span>
              </div>
              <input
                type="range"
                aria-label="Terrain exaggeration"
                min={0.5}
                max={3.0}
                step={0.1}
                value={spec.terrain_exaggeration}
                onChange={(e) =>
                  onUpdateSpec({ terrain_exaggeration: Number(e.currentTarget.value) })
                }
                className="w-full accent-sky-accent"
              />
            </label>

            <label className="flex items-center justify-between cursor-pointer">
              <span className="text-ink">3D elevation curtain</span>
              <input
                type="checkbox"
                checked={spec.elevation_curtain}
                onChange={(e) =>
                  onUpdateSpec({ elevation_curtain: e.currentTarget.checked })
                }
                className="accent-sky-accent"
              />
            </label>

            <div className="space-y-2 border-t border-border pt-3">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 font-medium text-ink-muted">
                  <Sparkles className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
                  <span>Director Presets</span>
                </span>
                {spec.camera_keyframes.length > 0 ? (
                  <button
                    type="button"
                    onClick={() => onUpdateSpec({ director_preset: "none", camera_keyframes: [] })}
                    className="text-[10px] text-ink-muted hover:text-coral-accent"
                  >
                    Clear cues
                  </button>
                ) : null}
              </div>
              <div className="grid grid-cols-3 gap-1.5" role="group" aria-label="Director presets">
                {DIRECTOR_PRESETS.map((preset) => {
                  const active = spec.director_preset === preset.id;
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      aria-pressed={active}
                      onClick={() => handleSelectDirectorPreset(preset.id)}
                      className={`rounded-md border px-2 py-1.5 text-left transition-colors ${
                        active
                          ? "border-amber-accent bg-amber-accent/15 text-ink"
                          : "border-border bg-elevated/60 text-ink-muted hover:text-ink"
                      }`}
                    >
                      <div className="text-[11px] font-semibold leading-tight">{preset.label}</div>
                      <div className="mt-0.5 text-[9px] opacity-75 leading-tight">{preset.hint}</div>
                    </button>
                  );
                })}
              </div>

              <div className="space-y-1.5 pt-1">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-medium text-ink-muted">
                    Camera keyframes ({spec.camera_keyframes.length}/40)
                  </span>
                  <button
                    type="button"
                    onClick={handleAddKeyframe}
                    disabled={spec.camera_keyframes.length >= 40}
                    className="inline-flex items-center gap-1 rounded border border-sky-accent/40 bg-sky-accent/10 px-2 py-0.5 text-[10px] font-semibold text-sky-accent hover:bg-sky-accent/20 disabled:opacity-40"
                  >
                    <Plus className="h-3 w-3" aria-hidden="true" />
                    <span>Add cue</span>
                  </button>
                </div>

                {spec.camera_keyframes.length > 0 ? (
                  <ul className="space-y-1.5" aria-label="Camera keyframes">
                    {spec.camera_keyframes.map((kf, idx) => (
                      <li
                        key={`${idx}-${kf.progress}`}
                        className="space-y-1.5 rounded border border-border bg-canvas/70 p-2 text-[11px]"
                      >
                        <div className="flex items-center justify-between gap-1.5">
                          <input
                            type="text"
                            aria-label={`Keyframe ${idx + 1} label`}
                            value={kf.label}
                            onChange={(e) =>
                              handleUpdateKeyframe(idx, { label: e.currentTarget.value })
                            }
                            placeholder={`Cue ${idx + 1}`}
                            className="min-w-0 flex-1 rounded border border-border bg-elevated px-1.5 py-0.5 text-[11px] text-ink"
                          />
                          <span className="tabular-telemetry shrink-0 text-[10px] text-amber-accent">
                            {Math.round(kf.progress * 100)}%
                          </span>
                          <button
                            type="button"
                            aria-label={`Remove keyframe ${idx + 1}`}
                            onClick={() => handleRemoveKeyframe(idx)}
                            className="rounded p-0.5 text-ink-muted hover:text-coral-accent"
                          >
                            <Trash2 className="h-3 w-3" aria-hidden="true" />
                          </button>
                        </div>
                        <div className="grid grid-cols-3 gap-1">
                          <select
                            aria-label={`Keyframe ${idx + 1} preset`}
                            value={kf.camera_preset}
                            onChange={(e) =>
                              handleUpdateKeyframe(idx, {
                                camera_preset: e.currentTarget.value as CameraPreset,
                              })
                            }
                            className="rounded border border-border bg-elevated px-1 py-0.5 text-[10px] text-ink"
                          >
                            <option value="flat">Flat</option>
                            <option value="dynamic-3d">Dynamic 3D</option>
                            <option value="drone-orbit">Orbit</option>
                          </select>
                          <input
                            type="number"
                            aria-label={`Keyframe ${idx + 1} pitch`}
                            min={0}
                            max={60}
                            value={Math.round(kf.pitch_deg)}
                            onChange={(e) =>
                              handleUpdateKeyframe(idx, {
                                pitch_deg: Math.min(60, Math.max(0, Number(e.currentTarget.value) || 0)),
                              })
                            }
                            title="Pitch (deg)"
                            className="tabular-telemetry rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-ink"
                          />
                          <input
                            type="number"
                            aria-label={`Keyframe ${idx + 1} speed`}
                            min={0.25}
                            max={4}
                            step={0.25}
                            value={kf.speed_multiplier}
                            onChange={(e) =>
                              handleUpdateKeyframe(idx, {
                                speed_multiplier: Math.min(
                                  4,
                                  Math.max(0.25, Number(e.currentTarget.value) || 1),
                                ),
                              })
                            }
                            title="Speed multiplier"
                            className="tabular-telemetry rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-ink"
                          />
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </div>

            <label className="block space-y-1">
              <div className="flex justify-between text-ink-muted">
                <span>Camera pitch</span>
                <span className="tabular-telemetry text-ink">{spec.camera_pitch_deg}°</span>
              </div>
              <input
                type="range"
                min={0}
                max={60}
                step={1}
                value={spec.camera_pitch_deg}
                onChange={(e) => onUpdateSpec({ camera_pitch_deg: Number(e.currentTarget.value) })}
                className="w-full accent-sky-accent"
              />
            </label>

            <label className="block space-y-1">
              <div className="flex justify-between text-ink-muted">
                <span>Camera bearing offset</span>
                <span className="tabular-telemetry text-ink">{spec.camera_bearing_deg}°</span>
              </div>
              <input
                type="range"
                min={-180}
                max={180}
                step={5}
                value={spec.camera_bearing_deg}
                onChange={(e) =>
                  onUpdateSpec({ camera_bearing_deg: Number(e.currentTarget.value) })
                }
                className="w-full accent-sky-accent"
              />
            </label>

            <div className="grid grid-cols-2 gap-3 border-t border-border pt-3">
              <label className="block space-y-1">
                <div className="flex justify-between text-ink-muted">
                  <span>Intro hold</span>
                  <span className="tabular-telemetry text-ink">{spec.intro_hold_seconds}s</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={3}
                  step={0.5}
                  value={spec.intro_hold_seconds}
                  onChange={(e) =>
                    onUpdateSpec({ intro_hold_seconds: Number(e.currentTarget.value) })
                  }
                  className="w-full accent-sky-accent"
                />
              </label>

              <label className="block space-y-1">
                <div className="flex justify-between text-ink-muted">
                  <span>Outro hold</span>
                  <span className="tabular-telemetry text-ink">{spec.outro_hold_seconds}s</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={3}
                  step={0.5}
                  value={spec.outro_hold_seconds}
                  onChange={(e) =>
                    onUpdateSpec({ outro_hold_seconds: Number(e.currentTarget.value) })
                  }
                  className="w-full accent-sky-accent"
                />
              </label>
            </div>
          </div>
        ) : null}

        {section === "hud" ? (
          <div className="space-y-4">
            <label className="flex items-center justify-between cursor-pointer">
              <span className="font-medium text-ink">Enable statistics HUD</span>
              <input
                type="checkbox"
                checked={hud.enabled}
                onChange={(e) => onUpdateHud({ enabled: e.currentTarget.checked })}
                className="accent-sky-accent"
              />
            </label>

            <label className="flex items-center justify-between cursor-pointer">
              <span className="text-ink">Auto-avoid route collision</span>
              <input
                type="checkbox"
                checked={hud.collision_avoidance}
                onChange={(e) => onUpdateHud({ collision_avoidance: e.currentTarget.checked })}
                className="accent-sky-accent"
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="block font-medium text-ink-muted">Position</span>
                <select
                  value={hud.position}
                  onChange={(e) =>
                    onUpdateHud({ position: e.currentTarget.value as PanelPosition })
                  }
                  className="w-full rounded-md border border-border bg-elevated px-2 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
                >
                  <option value="top">Top</option>
                  <option value="bottom">Bottom</option>
                  <option value="left">Left</option>
                  <option value="right">Right</option>
                </select>
              </label>

              <label className="block space-y-1">
                <span className="block font-medium text-ink-muted">IANA timezone</span>
                <input
                  list="iana-timezones"
                  value={hud.time_zone}
                  onChange={(e) => onUpdateHud({ time_zone: e.currentTarget.value || "UTC" })}
                  className="w-full rounded-md border border-border bg-elevated px-2 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
                />
                <datalist id="iana-timezones">
                  {COMMON_TIMEZONES.map((tz) => (
                    <option key={tz} value={tz} />
                  ))}
                </datalist>
              </label>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <div className="flex justify-between text-ink-muted">
                  <span>Text size</span>
                  <span className="tabular-telemetry text-ink">{hud.text_size}px</span>
                </div>
                <input
                  type="range"
                  min={12}
                  max={72}
                  value={hud.text_size}
                  onChange={(e) => onUpdateHud({ text_size: Number(e.currentTarget.value) })}
                  className="w-full accent-sky-accent"
                />
              </label>

              <label className="block space-y-1">
                <div className="flex justify-between text-ink-muted">
                  <span>Scale</span>
                  <span className="tabular-telemetry text-ink">{hud.scale.toFixed(1)}×</span>
                </div>
                <input
                  type="range"
                  min={0.5}
                  max={2.0}
                  step={0.1}
                  value={hud.scale}
                  onChange={(e) => onUpdateHud({ scale: Number(e.currentTarget.value) })}
                  className="w-full accent-sky-accent"
                />
              </label>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="block font-medium text-ink-muted">Text color</span>
                <div className="flex items-center gap-2 rounded-md border border-border bg-elevated px-2 py-1">
                  <input
                    type="color"
                    value={hud.text_color}
                    onChange={(e) => onUpdateHud({ text_color: e.currentTarget.value })}
                    className="h-5 w-6 cursor-pointer border-0 bg-transparent p-0"
                  />
                  <span className="tabular-telemetry text-[11px]">{hud.text_color}</span>
                </div>
              </label>

              <label className="block space-y-1">
                <span className="block font-medium text-ink-muted">Panel color</span>
                <div className="flex items-center gap-2 rounded-md border border-border bg-elevated px-2 py-1">
                  <input
                    type="color"
                    value={hud.background_color}
                    onChange={(e) => onUpdateHud({ background_color: e.currentTarget.value })}
                    className="h-5 w-6 cursor-pointer border-0 bg-transparent p-0"
                  />
                  <span className="tabular-telemetry text-[11px]">{hud.background_color}</span>
                </div>
              </label>
            </div>

            <label className="block space-y-1">
              <div className="flex justify-between text-ink-muted">
                <span>Background opacity</span>
                <span className="tabular-telemetry text-ink">
                  {Math.round(hud.background_opacity * 100)}%
                </span>
              </div>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={hud.background_opacity}
                onChange={(e) =>
                  onUpdateHud({ background_opacity: Number(e.currentTarget.value) })
                }
                className="w-full accent-sky-accent"
              />
            </label>

            <div className="space-y-1.5 border-t border-border pt-3">
              <span className="block font-medium text-ink-muted">
                Metrics (toggle &amp; reorder)
              </span>
              <ul className="space-y-1">
                {orderedMetrics.map((metric) => {
                  const enabled = hud.metrics.includes(metric);
                  const activeIdx = hud.metrics.indexOf(metric);
                  return (
                    <li
                      key={metric}
                      className="flex items-center justify-between rounded border border-border bg-canvas/60 px-2.5 py-1.5"
                    >
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={enabled}
                          onChange={() => toggleMetric(metric)}
                          className="accent-sky-accent"
                        />
                        <span className={enabled ? "text-ink" : "text-ink-muted"}>
                          {METRIC_LABELS[metric]}
                        </span>
                      </label>

                      {enabled ? (
                        <div className="flex items-center gap-0.5">
                          <button
                            type="button"
                            aria-label={`Move ${METRIC_LABELS[metric]} up`}
                            disabled={activeIdx === 0}
                            onClick={() => moveMetric(metric, -1)}
                            className="rounded p-0.5 text-ink-muted hover:bg-elevated hover:text-ink disabled:opacity-30"
                          >
                            <ArrowUp className="h-3 w-3" aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            aria-label={`Move ${METRIC_LABELS[metric]} down`}
                            disabled={activeIdx === hud.metrics.length - 1}
                            onClick={() => moveMetric(metric, 1)}
                            className="rounded p-0.5 text-ink-muted hover:bg-elevated hover:text-ink disabled:opacity-30"
                          >
                            <ArrowDown className="h-3 w-3" aria-hidden="true" />
                          </button>
                        </div>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        ) : null}

        <div hidden={section !== "export"}>
          <ExportPanel
            projectId={projectId}
            spec={spec}
            stageCount={stageCount}
            onFlushSave={onFlushSave}
            localExportSource={localExportSource}
          />
        </div>
      </div>
    </aside>
  );
}
