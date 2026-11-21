/** Manage photo stops and expose the outcome of each selected upload. */
import { Camera, Check, Clock, ImagePlus, Loader2, MapPin, Pencil, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { photosApi } from "../../lib/api/endpoints";
import type { PhotoMatchMethod, PhotoShiftClockOut, ProjectPhotoOut } from "../../lib/api/types";
import type { MatchedPhoto } from "../../lib/geo/photos";

export interface PhotoStopsSectionProps {
  projectId: string;
  photos: readonly ProjectPhotoOut[];
  matchedPhotos?: readonly MatchedPhoto[];
  googlePhotosConfigured?: boolean;
  pinModePhotoId?: string | null;
  onTogglePinMode?: (photoId: string | null) => void;
  onPhotosMutated?: () => Promise<void> | void;
}

type UploadStatus = "queued" | "uploading" | "success" | "duplicate" | "error";

interface PendingPhoto {
  id: string;
  file: File;
  status: UploadStatus;
  fingerprint?: string;
  uploadedId?: string;
  error?: string;
}

const MATCH_BADGE_STYLES: Record<PhotoMatchMethod, string> = {
  timestamp: "border-teal-accent/50 bg-teal-accent/15 text-teal-accent",
  gps: "border-sky-accent/50 bg-sky-accent/15 text-sky-accent",
  manual: "border-amber-accent/50 bg-amber-accent/15 text-amber-accent",
  unmatched: "border-border bg-surface text-ink-muted",
};

function formatOffsetSummary(offsetSeconds: number): string {
  if (offsetSeconds === 0) return "0m";
  const sign = offsetSeconds >= 0 ? "+" : "-";
  const abs = Math.abs(offsetSeconds);
  const h = Math.floor(abs / 3600);
  const m = Math.round((abs % 3600) / 60);
  if (h > 0 && m > 0) return `${sign}${h}h ${m}m`;
  if (h > 0) return `${sign}${h}h`;
  return `${sign}${m}m`;
}

/** Edit project photo stops and show individual upload progress. */
export function PhotoStopsSection({
  projectId,
  photos,
  matchedPhotos = [],
  googlePhotosConfigured = false,
  pinModePhotoId = null,
  onTogglePinMode,
  onPhotosMutated,
}: PhotoStopsSectionProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const previewUrls = useRef(new Set<string>());
  const uploadInProgress = useRef(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [pendingPhotos, setPendingPhotos] = useState<PendingPhoto[]>([]);
  const [busyPhotoId, setBusyPhotoId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftCaption, setDraftCaption] = useState("");
  const [draftTakenAt, setDraftTakenAt] = useState("");
  const [draftLat, setDraftLat] = useState("");
  const [draftLon, setDraftLon] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [clockSyncOpen, setClockSyncOpen] = useState(false);
  const [shiftHours, setShiftHours] = useState(0);
  const [shiftMinutes, setShiftMinutes] = useState(0);
  const [clockBusy, setClockBusy] = useState(false);
  const [clockPreview, setClockPreview] = useState<PhotoShiftClockOut | null>(null);
  const [googlePickerBusy, setGooglePickerBusy] = useState(false);
  const [googlePickerNotice, setGooglePickerNotice] = useState<string | null>(null);

  useEffect(() => () => {
    for (const url of previewUrls.current) URL.revokeObjectURL(url);
  }, []);

  useEffect(() => {
    setPendingPhotos((current) => current.filter((pending) => {
      if (!pending.uploadedId || !photos.some((photo) => photo.id === pending.uploadedId)) return true;
      URL.revokeObjectURL(pending.id);
      previewUrls.current.delete(pending.id);
      return false;
    }));
  }, [photos]);

  const matchedById = new Map(matchedPhotos.map((m) => [m.id, m]));

  async function handlePreviewClockShift(autoDetect: boolean) {
    setClockBusy(true);
    setErrorMsg(null);
    try {
      const offsetSeconds = autoDetect ? null : shiftHours * 3600 + shiftMinutes * 60;
      const res = await photosApi.shiftClock(projectId, {
        offset_seconds: offsetSeconds,
        dry_run: true,
      });
      setClockPreview(res);
      if (autoDetect) {
        const totalMin = Math.round(res.applied_offset_seconds / 60);
        const h = Math.trunc(totalMin / 60);
        const m = totalMin - h * 60;
        setShiftHours(h);
        setShiftMinutes(m);
      }
    } catch (err) {
      setErrorMsg(err instanceof ApiError ? err.message : "Clock-shift preview failed.");
    } finally {
      setClockBusy(false);
    }
  }

  async function handleApplyClockShift() {
    const offsetSeconds = clockPreview
      ? clockPreview.applied_offset_seconds
      : shiftHours * 3600 + shiftMinutes * 60;
    setClockBusy(true);
    setErrorMsg(null);
    try {
      await photosApi.shiftClock(projectId, {
        offset_seconds: offsetSeconds,
        dry_run: false,
      });
      setClockPreview(null);
      await onPhotosMutated?.();
    } catch (err) {
      setErrorMsg(err instanceof ApiError ? err.message : "Failed to apply camera clock shift.");
    } finally {
      setClockBusy(false);
    }
  }

  async function handleGooglePhotosPicker() {
    setGooglePickerBusy(true);
    setGooglePickerNotice(null);
    setErrorMsg(null);
    try {
      const res = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/photos/google-picker`, {
        method: "POST",
        credentials: "same-origin",
      });
      if (!res.ok) {
        setGooglePickerNotice(
          "Google Photos Picker session requires an active Google Photos OAuth link on this server.",
        );
        return;
      }
      const data = (await res.json()) as { picker_uri?: string };
      if (data.picker_uri && typeof window !== "undefined") {
        window.open(data.picker_uri, "_blank", "noopener,noreferrer");
        setGooglePickerNotice("Google Photos Picker opened in a new tab. Refresh once selection completes.");
      } else {
        await onPhotosMutated?.();
      }
    } catch {
      setGooglePickerNotice(
        "Unable to launch Google Photos Picker right now. Drop local JPEG/PNG/WebP files above.",
      );
    } finally {
      setGooglePickerBusy(false);
    }
  }

  async function handlePhotoFiles(fileList: FileList | File[]) {
    const files = Array.from(fileList);
    if (files.length === 0) return;
    if (uploadInProgress.current) {
      setErrorMsg("Please wait for the current photo batch to finish before adding more files.");
      return;
    }
    uploadInProgress.current = true;
    for (const url of previewUrls.current) URL.revokeObjectURL(url);
    previewUrls.current.clear();

    const batch: PendingPhoto[] = files.map((file) => {
      const id = URL.createObjectURL(file);
      previewUrls.current.add(id);
      return { id, file, status: "queued" };
    });
    setPendingPhotos(batch);
    setUploading(true);
    setErrorMsg(null);
    const knownFingerprints = new Set(
      photos.flatMap((photo) => photo.content_hash ? [photo.content_hash] : []),
    );

    for (const pending of batch) {
      try {
        setPendingPhotos((current) => current.map((item) =>
          item.id === pending.id ? { ...item, status: "uploading" } : item,
        ));
        let fingerprint: string | undefined;
        try {
          fingerprint = await sha256(pending.file);
        } catch {
          // Server-side deduplication remains available without browser Web Crypto.
        }
        if (fingerprint && knownFingerprints.has(fingerprint)) {
          setPendingPhotos((current) => current.map((item) =>
            item.id === pending.id ? { ...item, fingerprint, status: "duplicate" } : item,
          ));
          continue;
        }
        const uploaded = await photosApi.upload(projectId, pending.file);
        if (uploaded.content_hash) knownFingerprints.add(uploaded.content_hash);
        if (fingerprint) knownFingerprints.add(fingerprint);
        setPendingPhotos((current) => current.map((item) =>
          item.id === pending.id ? { ...item, fingerprint, uploadedId: uploaded.id, status: "success" } : item,
        ));
        try {
          await onPhotosMutated?.();
          setRefreshFailed(false);
        } catch {
          setRefreshFailed(true);
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          setPendingPhotos((current) => current.map((item) =>
            item.id === pending.id ? { ...item, status: "duplicate" } : item,
          ));
        } else {
          const message = err instanceof ApiError ? err.message : `Failed to upload ${pending.file.name}`;
          setPendingPhotos((current) => current.map((item) =>
            item.id === pending.id ? { ...item, status: "error", error: message } : item,
          ));
        }
      }
    }

    setUploading(false);
    uploadInProgress.current = false;
  }

  async function sha256(file: File): Promise<string> {
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function startEditing(photo: ProjectPhotoOut) {
    setEditingId(photo.id);
    setDraftCaption(photo.caption ?? "");
    setDraftTakenAt(photo.taken_at_utc ?? "");
    setDraftLat(photo.lat !== null ? String(photo.lat) : "");
    setDraftLon(photo.lon !== null ? String(photo.lon) : "");
    setErrorMsg(null);
  }

  async function saveEdit(photo: ProjectPhotoOut) {
    const parsedLat = draftLat.trim() === "" ? null : Number(draftLat);
    const parsedLon = draftLon.trim() === "" ? null : Number(draftLon);
    if (parsedLat !== null && (!Number.isFinite(parsedLat) || parsedLat < -90 || parsedLat > 90)) {
      setErrorMsg("Latitude must be between -90 and 90.");
      return;
    }
    if (
      parsedLon !== null &&
      (!Number.isFinite(parsedLon) || parsedLon < -180 || parsedLon > 180)
    ) {
      setErrorMsg("Longitude must be between -180 and 180.");
      return;
    }

    setBusyPhotoId(photo.id);
    setErrorMsg(null);
    try {
      await photosApi.update(projectId, photo.id, {
        caption: draftCaption.trim() || null,
        taken_at_utc: draftTakenAt.trim() || null,
        lat: parsedLat,
        lon: parsedLon,
      });
      setEditingId(null);
      await onPhotosMutated?.();
    } catch (err) {
      setErrorMsg(err instanceof ApiError ? err.message : "Failed to update photo.");
    } finally {
      setBusyPhotoId(null);
    }
  }

  async function toggleEnabled(photo: ProjectPhotoOut) {
    setBusyPhotoId(photo.id);
    setErrorMsg(null);
    try {
      await photosApi.update(projectId, photo.id, {
        enabled: photo.enabled === false,
      });
      await onPhotosMutated?.();
    } catch (err) {
      setErrorMsg(err instanceof ApiError ? err.message : "Failed to toggle photo.");
    } finally {
      setBusyPhotoId(null);
    }
  }

  async function deletePhoto(photoId: string) {
    setBusyPhotoId(photoId);
    setErrorMsg(null);
    try {
      await photosApi.delete(projectId, photoId);
      if (editingId === photoId) {
        setEditingId(null);
      }
      await onPhotosMutated?.();
    } catch (err) {
      setErrorMsg(err instanceof ApiError ? err.message : "Failed to delete photo.");
    } finally {
      setBusyPhotoId(null);
    }
  }

  return (
    <section aria-label="Photo stops" className="space-y-2.5 border-t border-border pt-3">
      <div className="flex items-center justify-between px-0.5">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-ink">
          <Camera className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
          <span>Photo stops</span>
        </div>
        <span className="tabular-telemetry rounded bg-elevated px-1.5 py-0.5 text-[10px] text-ink-muted">
          {photos.length} / 50
        </span>
      </div>

      <div
        role="button"
        tabIndex={0}
        aria-label="Drop photos or click to upload"
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragEnter={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setIsDragOver(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setIsDragOver(true);
        }}
        onDragLeave={() => setIsDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setIsDragOver(false);
          if (e.dataTransfer.files.length > 0) {
            void handlePhotoFiles(Array.from(e.dataTransfer.files));
          }
        }}
        className={`cursor-pointer rounded-md border border-dashed p-2.5 text-center transition-colors ${
          isDragOver
            ? "border-sky-accent bg-sky-accent/10"
            : "border-border bg-canvas/60 hover:border-sky-accent/50"
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
          multiple
          disabled={uploading}
          className="hidden"
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            if (e.currentTarget.files && e.currentTarget.files.length > 0) {
              const selectedFiles = Array.from(e.currentTarget.files);
              e.currentTarget.value = "";
              void handlePhotoFiles(selectedFiles);
            }
          }}
        />
        {uploading ? (
          <Loader2 className="mx-auto h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
        ) : (
          <ImagePlus className="mx-auto h-4 w-4 text-sky-accent" aria-hidden="true" />
        )}
        <p className="mt-1 text-[11px] font-medium text-ink">
          {uploading ? "Uploading photos…" : "Drop JPEG/PNG/WebP photos"}
        </p>
        <p className="text-[10px] text-ink-muted">Auto-matched by EXIF time or GPS</p>
        {photos.some((photo) => photo.content_hash == null) ? (
          <p className="text-[10px] text-ink-muted">Older photos are checked for duplicates by the server after transfer.</p>
        ) : null}
      </div>

      {pendingPhotos.length > 0 ? (
        <ul aria-label="Photo upload status" className="grid grid-cols-2 gap-1.5">
          {pendingPhotos.map((pending) => (
            <li key={pending.id} className="flex min-w-0 flex-col items-center gap-1 rounded border border-border bg-canvas/70 p-1.5">
              <img src={pending.id} alt="" className="h-20 w-full rounded object-cover" />
              <span className="w-full truncate text-[10px] text-ink">{pending.file.name}</span>
              <span role="status" className={`text-[10px] ${pending.status === "error" ? "text-red-500" : "text-ink-muted"}`}>
                {pending.status === "queued" ? "Queued" : null}
                {pending.status === "uploading" ? "Uploading…" : null}
                {pending.status === "success" ? "Uploaded" : null}
                {pending.status === "duplicate" ? "Duplicate — skipped" : null}
                {pending.status === "error" ? `Error${pending.error ? `: ${pending.error}` : ""}` : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {refreshFailed ? (
        <div role="alert" className="text-[11px] text-ink-muted">
          Photos were uploaded, but the photo list could not be refreshed.
          <button type="button" className="ml-1 underline" onClick={() => {
            void Promise.resolve().then(() => onPhotosMutated?.()).then(() => setRefreshFailed(false)).catch(() => setRefreshFailed(true));
          }}>Refresh photos</button>
        </div>
      ) : null}

      {googlePhotosConfigured ? (
        <div className="space-y-1.5">
          <button
            type="button"
            disabled={googlePickerBusy}
            onClick={() => void handleGooglePhotosPicker()}
            className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-border bg-elevated px-3 py-1.5 text-[11px] font-medium text-ink hover:border-sky-accent/50 disabled:opacity-50"
          >
            {googlePickerBusy ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-accent" aria-hidden="true" />
            ) : (
              <Camera className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
            )}
            <span>Import from Google Photos</span>
          </button>
          {googlePickerNotice ? (
            <p className="rounded border border-border bg-canvas/70 px-2 py-1.5 text-[10px] text-ink-muted">
              {googlePickerNotice}
            </p>
          ) : null}
        </div>
      ) : null}

      {photos.length > 0 ? (
        <div className="space-y-2">
          <button
            type="button"
            aria-expanded={clockSyncOpen}
            onClick={() => setClockSyncOpen((v) => !v)}
            className="inline-flex w-full items-center justify-between rounded-md border border-border bg-elevated/70 px-2.5 py-1.5 text-[11px] font-medium text-ink hover:border-sky-accent/50"
          >
            <span className="inline-flex items-center gap-1.5">
              <Clock className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
              <span>Camera clock shift</span>
            </span>
            <span className="text-[10px] text-ink-muted">{clockSyncOpen ? "Hide" : "Sync EXIF"}</span>
          </button>

          {clockSyncOpen ? (
            <div
              data-testid="camera-clock-shift-panel"
              className="space-y-2 rounded-md border border-border bg-canvas/80 p-2.5 text-[11px]"
            >
              <div className="grid grid-cols-2 gap-2">
                <label className="block space-y-0.5">
                  <span className="text-[10px] text-ink-muted">Hours (±h)</span>
                  <input
                    type="number"
                    aria-label="Clock shift hours"
                    min={-14}
                    max={14}
                    value={shiftHours}
                    onChange={(e) => {
                      setShiftHours(Number(e.currentTarget.value) || 0);
                      setClockPreview(null);
                    }}
                    className="tabular-telemetry w-full rounded border border-border bg-elevated px-2 py-1 text-xs text-ink"
                  />
                </label>
                <label className="block space-y-0.5">
                  <span className="text-[10px] text-ink-muted">Minutes (±m)</span>
                  <input
                    type="number"
                    aria-label="Clock shift minutes"
                    min={-59}
                    max={59}
                    value={shiftMinutes}
                    onChange={(e) => {
                      setShiftMinutes(Number(e.currentTarget.value) || 0);
                      setClockPreview(null);
                    }}
                    className="tabular-telemetry w-full rounded border border-border bg-elevated px-2 py-1 text-xs text-ink"
                  />
                </label>
              </div>

              <div className="grid grid-cols-2 gap-1.5">
                <button
                  type="button"
                  disabled={clockBusy}
                  onClick={() => void handlePreviewClockShift(false)}
                  className="rounded border border-border bg-elevated px-2 py-1 text-[10px] font-medium text-ink hover:border-sky-accent/50 disabled:opacity-50"
                >
                  Preview shift
                </button>
                <button
                  type="button"
                  disabled={clockBusy}
                  onClick={() => void handlePreviewClockShift(true)}
                  className="inline-flex items-center justify-center gap-1 rounded border border-amber-accent/50 bg-amber-accent/10 px-2 py-1 text-[10px] font-medium text-amber-accent hover:bg-amber-accent/20 disabled:opacity-50"
                >
                  <Sparkles className="h-3 w-3" aria-hidden="true" />
                  <span>Auto-detect clock offset</span>
                </button>
              </div>

              {clockPreview ? (
                <div
                  data-testid="clock-shift-preview"
                  className="space-y-1.5 rounded border border-teal-accent/40 bg-teal-accent/10 p-2 text-[10px]"
                >
                  <div className="flex items-center justify-between text-ink">
                    <span>Offset: {formatOffsetSummary(clockPreview.applied_offset_seconds)}</span>
                    <span className="tabular-telemetry font-semibold text-teal-accent">
                      {clockPreview.matched_before} → {clockPreview.matched_after} matched
                      {clockPreview.newly_matched_count > 0
                        ? ` (+${clockPreview.newly_matched_count})`
                        : ""}
                    </span>
                  </div>
                  <button
                    type="button"
                    disabled={clockBusy}
                    onClick={() => void handleApplyClockShift()}
                    className="w-full rounded bg-teal-accent px-2 py-1 text-[11px] font-semibold text-canvas hover:bg-teal-accent/90 disabled:opacity-50"
                  >
                    {clockBusy ? "Applying…" : "Apply clock shift"}
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {errorMsg ? (
        <div
          role="alert"
          data-testid="photo-upload-error"
          className="rounded-md border border-coral-accent/40 bg-coral-accent/10 px-2.5 py-2 text-[11px] text-ink"
        >
          {errorMsg}
        </div>
      ) : null}

      {photos.length > 0 ? (
        <ul className="space-y-2" aria-label="Uploaded photo stops">
          {photos.map((photo) => {
            const matched = matchedById.get(photo.id);
            const effectiveMethod: PhotoMatchMethod = matched?.matchMethod ?? photo.match_method;
            const isEditing = editingId === photo.id;
            const isBusy = busyPhotoId === photo.id;
            const isEnabled = photo.enabled !== false;

            return (
              <li
                key={photo.id}
                data-testid={`photo-item-${photo.id}`}
                className={`space-y-2 rounded-md border border-border bg-canvas/70 p-2 text-[11px] ${
                  isEnabled ? "" : "opacity-70"
                }`}
              >
                <div className="flex items-start gap-2">
                  <img
                    src={photosApi.imageUrl(projectId, photo.id, "thumb")}
                    alt={photo.caption ?? "Photo thumbnail"}
                    className="h-11 w-14 shrink-0 rounded border border-border object-cover"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-1">
                      {isEnabled ? (
                        <span
                          className={`rounded border px-1.5 py-0.5 text-[9px] font-semibold uppercase ${MATCH_BADGE_STYLES[effectiveMethod]}`}
                        >
                          {effectiveMethod}
                        </span>
                      ) : (
                        <span
                          data-testid={`photo-hidden-badge-${photo.id}`}
                          className="rounded border border-border/80 bg-elevated px-1.5 py-0.5 text-[9px] font-semibold uppercase text-ink-muted"
                        >
                          HIDDEN
                        </span>
                      )}
                      <div className="flex items-center gap-1">
                        <input
                          type="checkbox"
                          aria-label={`Include photo ${photo.caption ?? photo.id}`}
                          checked={isEnabled}
                          disabled={isBusy}
                          onChange={() => void toggleEnabled(photo)}
                          className="accent-sky-accent"
                        />
                        <button
                          type="button"
                          aria-label={`Edit photo ${photo.caption ?? photo.id}`}
                          disabled={isBusy}
                          onClick={() => (isEditing ? setEditingId(null) : startEditing(photo))}
                          className="rounded p-0.5 text-ink-muted hover:bg-elevated hover:text-ink"
                        >
                          <Pencil className="h-3 w-3" aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          aria-label={`Delete photo ${photo.caption ?? photo.id}`}
                          disabled={isBusy}
                          onClick={() => void deletePhoto(photo.id)}
                          className="rounded p-0.5 text-coral-accent hover:bg-coral-accent/15"
                        >
                          <Trash2 className="h-3 w-3" aria-hidden="true" />
                        </button>
                      </div>
                    </div>
                    <p className="mt-1 truncate text-ink">
                      {photo.caption || (photo.taken_at_utc ? photo.taken_at_utc.slice(0, 16) : "Untitled photo")}
                    </p>
                    {onTogglePinMode ? (
                      <div className="mt-1.5">
                        <button
                          type="button"
                          aria-pressed={pinModePhotoId === photo.id}
                          onClick={() =>
                            onTogglePinMode(pinModePhotoId === photo.id ? null : photo.id)
                          }
                          className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
                            pinModePhotoId === photo.id
                              ? "border-amber-accent bg-amber-accent/20 text-amber-accent"
                              : "border-border bg-elevated text-ink-muted hover:border-sky-accent/50 hover:text-ink"
                          }`}
                        >
                          <MapPin className="h-2.5 w-2.5" aria-hidden="true" />
                          <span>
                            {pinModePhotoId === photo.id ? "Click map to pin…" : "Pin on route"}
                          </span>
                        </button>
                      </div>
                    ) : null}
                  </div>
                </div>

                {isEditing ? (
                  <div className="space-y-1.5 border-t border-border/70 pt-2">
                    <input
                      type="text"
                      placeholder="Caption"
                      aria-label="Photo caption"
                      value={draftCaption}
                      onChange={(e) => setDraftCaption(e.currentTarget.value)}
                      className="w-full rounded border border-border bg-elevated px-2 py-1 text-[11px] text-ink focus:border-sky-accent focus:outline-none"
                    />
                    <input
                      type="text"
                      placeholder="UTC timestamp (ISO-8601)"
                      aria-label="Photo timestamp UTC"
                      value={draftTakenAt}
                      onChange={(e) => setDraftTakenAt(e.currentTarget.value)}
                      className="w-full rounded border border-border bg-elevated px-2 py-1 text-[11px] text-ink focus:border-sky-accent focus:outline-none"
                    />
                    <div className="grid grid-cols-2 gap-1.5">
                      <input
                        type="number"
                        step="any"
                        placeholder="Lat"
                        aria-label="Photo latitude"
                        value={draftLat}
                        onChange={(e) => setDraftLat(e.currentTarget.value)}
                        className="w-full rounded border border-border bg-elevated px-2 py-1 text-[11px] text-ink focus:border-sky-accent focus:outline-none"
                      />
                      <input
                        type="number"
                        step="any"
                        placeholder="Lon"
                        aria-label="Photo longitude"
                        value={draftLon}
                        onChange={(e) => setDraftLon(e.currentTarget.value)}
                        className="w-full rounded border border-border bg-elevated px-2 py-1 text-[11px] text-ink focus:border-sky-accent focus:outline-none"
                      />
                    </div>
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => void saveEdit(photo)}
                      className="inline-flex w-full items-center justify-center gap-1 rounded border border-sky-accent/50 bg-sky-accent/15 py-1 text-[11px] font-medium text-sky-accent hover:bg-sky-accent/25"
                    >
                      <Check className="h-3 w-3" aria-hidden="true" />
                      <span>Save photo details</span>
                    </button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
