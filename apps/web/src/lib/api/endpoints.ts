import type { ActivityType } from "../../components/icons";
import { apiClient, clearCsrfToken, generateIdempotencyKey } from "./client";
import type {
  AnalyticsHeatmapOut,
  AuditEventOut,
  AuthSessionOut,
  CareerAnalyticsOut,
  ChangePasswordRequest,
  ChangePasswordResponse,
  ForgotPasswordResponse,
  HealthResponse,
  LodLevel,
  LogoutAllResponse,
  PhotoPinRequest,
  PhotoShiftClockOut,
  PhotoShiftClockRequest,
  ProjectCreateRequest,
  ProjectDetailOut,
  ProjectHighlightsOut,
  ProjectKpisOut,
  ProjectOut,
  ProjectPhotoOut,
  ProjectPhotoUpdateRequest,
  ProjectShareOut,
  ProjectUpdateRequest,
  PublicConfigOut,
  PublicProjectOut,
  RegisterRequest,
  RegisterResponse,
  RenderJobOut,
  RenderSpecV2,
  ResendVerificationResponse,
  ResetPasswordRequest,
  ResetPasswordResponse,
  SessionInfoOut,
  StageAutoOrganizeOut,
  StageAutoOrganizeRequest,
  StageOut,
  StageReorderRequest,
  StageUpdateRequest,
  StravaActivityOut,
  StravaBatchImportOut,
  StravaBatchImportRequest,
  StravaCallbackOut,
  StravaConnectOut,
  StravaDisconnectOut,
  StravaImportAllOut,
  StravaImportAllRequest,
  StravaImportRequest,
  StravaImportStatusOut,
  TelemetryChartOut,
  TelemetryGeometryFeature,
  TelemetryGeometryOut,
} from "./types";

export const systemApi = {
  health(signal?: AbortSignal): Promise<HealthResponse> {
    return apiClient.get<HealthResponse>("/health", signal);
  },
  publicConfig(signal?: AbortSignal): Promise<PublicConfigOut> {
    return apiClient.get<PublicConfigOut>("/api/v1/public-config", signal);
  },
};

export const authApi = {
  me(signal?: AbortSignal): Promise<SessionInfoOut> {
    return apiClient.get<SessionInfoOut>("/api/v1/auth/me", signal);
  },
  login(email: string, password: string): Promise<SessionInfoOut> {
    return apiClient.post<SessionInfoOut>("/api/v1/auth/login", { email, password });
  },
  register(payload: RegisterRequest): Promise<RegisterResponse> {
    return apiClient.post<RegisterResponse>("/api/v1/auth/register", payload);
  },
  verifyEmail(token: string): Promise<SessionInfoOut> {
    return apiClient.post<SessionInfoOut>("/api/v1/auth/verify-email", { token });
  },
  resendVerification(email: string): Promise<ResendVerificationResponse> {
    return apiClient.post<ResendVerificationResponse>("/api/v1/auth/resend-verification", { email });
  },
  forgotPassword(email: string): Promise<ForgotPasswordResponse> {
    return apiClient.post<ForgotPasswordResponse>("/api/v1/auth/forgot-password", { email });
  },
  resetPassword(payload: ResetPasswordRequest): Promise<ResetPasswordResponse> {
    return apiClient.post<ResetPasswordResponse>("/api/v1/auth/reset-password", payload);
  },
  changePassword(payload: ChangePasswordRequest): Promise<ChangePasswordResponse> {
    return apiClient.post<ChangePasswordResponse>("/api/v1/auth/change-password", payload);
  },
  listSessions(signal?: AbortSignal): Promise<AuthSessionOut[]> {
    return apiClient.get<AuthSessionOut[]>("/api/v1/auth/sessions", signal);
  },
  revokeSession(sessionId: string): Promise<{ ok: boolean }> {
    return apiClient.delete<{ ok: boolean }>(
      `/api/v1/auth/sessions/${encodeURIComponent(sessionId)}`,
    );
  },
  devLogin(): Promise<SessionInfoOut> {
    return apiClient.post<SessionInfoOut>("/api/v1/auth/dev-login");
  },
  async logout(): Promise<{ ok: boolean }> {
    const res = await apiClient.post<{ ok: boolean }>("/api/v1/auth/logout");
    clearCsrfToken();
    return res;
  },
  async logoutAll(): Promise<LogoutAllResponse> {
    const res = await apiClient.post<LogoutAllResponse>("/api/v1/auth/logout-all");
    clearCsrfToken();
    return res;
  },
};

export const projectsApi = {
  list(signal?: AbortSignal): Promise<ProjectOut[]> {
    return apiClient.get<ProjectOut[]>("/api/v1/projects", signal);
  },
  get(projectId: string, signal?: AbortSignal): Promise<ProjectDetailOut> {
    return apiClient.get<ProjectDetailOut>(`/api/v1/projects/${projectId}`, signal);
  },
  create(payload: ProjectCreateRequest): Promise<ProjectOut> {
    return apiClient.post<ProjectOut>("/api/v1/projects", payload);
  },
  update(projectId: string, payload: ProjectUpdateRequest): Promise<ProjectDetailOut> {
    return apiClient.patch<ProjectDetailOut>(`/api/v1/projects/${projectId}`, payload);
  },
  delete(projectId: string): Promise<void> {
    return apiClient.delete<void>(`/api/v1/projects/${projectId}`);
  },
  enableShare(projectId: string, options?: { rotate?: boolean }): Promise<ProjectShareOut> {
    return apiClient.post<ProjectShareOut>(`/api/v1/projects/${projectId}/share`, {
      rotate: options?.rotate ?? false,
    });
  },
  disableShare(projectId: string): Promise<ProjectShareOut> {
    return apiClient.delete<ProjectShareOut>(`/api/v1/projects/${projectId}/share`);
  },
  audit(projectId: string, limit = 50, signal?: AbortSignal): Promise<AuditEventOut[]> {
    return apiClient.get<AuditEventOut[]>(
      `/api/v1/projects/${projectId}/audit?limit=${encodeURIComponent(String(limit))}`,
      signal,
    );
  },
  exportUrl(projectId: string, format: "gpx" | "geojson"): string {
    return `/api/v1/projects/${projectId}/export?format=${format}`;
  },
};

export const stagesApi = {
  uploadGpx(
    projectId: string,
    file: File,
    options?: { dayIndex?: number; name?: string; activityType?: ActivityType },
  ): Promise<StageOut> {
    const formData = new FormData();
    formData.append("file", file);
    if (options?.dayIndex !== undefined) {
      formData.append("day_index", String(options.dayIndex));
    }
    if (options?.name && options.name.trim().length > 0) {
      formData.append("name", options.name.trim());
    }
    if (options?.activityType) {
      formData.append("activity_type", options.activityType);
    }
    return apiClient.post<StageOut>(`/api/v1/projects/${projectId}/gpx`, formData);
  },
  uploadTrack(
    projectId: string,
    file: File,
    options?: { dayIndex?: number; name?: string; activityType?: ActivityType },
  ): Promise<StageOut> {
    const formData = new FormData();
    formData.append("file", file);
    if (options?.dayIndex !== undefined) {
      formData.append("day_index", String(options.dayIndex));
    }
    if (options?.name && options.name.trim().length > 0) {
      formData.append("name", options.name.trim());
    }
    if (options?.activityType) {
      formData.append("activity_type", options.activityType);
    }
    return apiClient.post<StageOut>(`/api/v1/projects/${projectId}/tracks`, formData);
  },
  update(projectId: string, stageId: string, payload: StageUpdateRequest): Promise<StageOut> {
    return apiClient.patch<StageOut>(
      `/api/v1/projects/${projectId}/stages/${stageId}`,
      payload,
    );
  },
  delete(projectId: string, stageId: string): Promise<void> {
    return apiClient.delete<void>(`/api/v1/projects/${projectId}/stages/${stageId}`);
  },
  reorder(projectId: string, payload: StageReorderRequest): Promise<StageOut[]> {
    return apiClient.post<StageOut[]>(`/api/v1/projects/${projectId}/stages/reorder`, payload);
  },
  autoOrganize(
    projectId: string,
    payload: StageAutoOrganizeRequest = {},
  ): Promise<StageAutoOrganizeOut> {
    return apiClient.post<StageAutoOrganizeOut>(
      `/api/v1/projects/${projectId}/stages/auto-organize`,
      payload,
    );
  },
};

export const telemetryApi = {
  chart(
    projectId: string,
    options?: { stageId?: string; maxPoints?: number; signal?: AbortSignal },
  ): Promise<TelemetryChartOut> {
    const params = new URLSearchParams();
    if (options?.stageId) {
      params.set("stage_id", options.stageId);
    }
    if (options?.maxPoints) {
      params.set("max_points", String(options.maxPoints));
    }
    const qs = params.toString();
    return apiClient.get<TelemetryChartOut>(
      `/api/v1/projects/${projectId}/telemetry/chart${qs ? `?${qs}` : ""}`,
      options?.signal,
    );
  },
  geometry(
    projectId: string,
    options?: { stageId?: string; lod?: LodLevel; signal?: AbortSignal },
  ): Promise<TelemetryGeometryOut> {
    const params = new URLSearchParams();
    if (options?.stageId) {
      params.set("stage_id", options.stageId);
    }
    if (options?.lod) {
      params.set("lod", options.lod);
    }
    const qs = params.toString();
    return apiClient.get<TelemetryGeometryOut>(
      `/api/v1/projects/${projectId}/telemetry/geometry${qs ? `?${qs}` : ""}`,
      options?.signal,
    );
  },
  stageGeometry(
    projectId: string,
    stageId: string,
    options?: { lod?: LodLevel; signal?: AbortSignal },
  ): Promise<TelemetryGeometryFeature> {
    const lod = options?.lod ?? "medium";
    return apiClient.get<TelemetryGeometryFeature>(
      `/api/v1/projects/${projectId}/stages/${stageId}/geometry?lod=${encodeURIComponent(lod)}`,
      options?.signal,
    );
  },
  kpis(projectId: string, signal?: AbortSignal): Promise<ProjectKpisOut> {
    return apiClient.get<ProjectKpisOut>(`/api/v1/projects/${projectId}/kpis`, signal);
  },
  highlights(projectId: string, signal?: AbortSignal): Promise<ProjectHighlightsOut> {
    return apiClient.get<ProjectHighlightsOut>(
      `/api/v1/projects/${projectId}/analytics/highlights`,
      signal,
    );
  },
};

export const stravaApi = {
  connect(signal?: AbortSignal): Promise<StravaConnectOut> {
    return apiClient.get<StravaConnectOut>("/api/v1/strava/connect", signal);
  },
  callback(code: string, state: string): Promise<StravaCallbackOut> {
    const params = new URLSearchParams({ code, state });
    return apiClient.get<StravaCallbackOut>(`/api/v1/strava/callback?${params.toString()}`);
  },
  disconnect(): Promise<StravaDisconnectOut> {
    return apiClient.post<StravaDisconnectOut>("/api/v1/strava/disconnect");
  },
  activities(
    page = 1,
    perPage = 30,
    options?: { after?: string; before?: string; signal?: AbortSignal } | AbortSignal,
  ): Promise<StravaActivityOut[]> {
    const resolved =
      options && typeof AbortSignal !== "undefined" && options instanceof AbortSignal
        ? { signal: options }
        : (options as { after?: string; before?: string; signal?: AbortSignal } | undefined);
    const params = new URLSearchParams({
      page: String(page),
      per_page: String(perPage),
    });
    if (resolved?.after) {
      params.set("after", resolved.after);
    }
    if (resolved?.before) {
      params.set("before", resolved.before);
    }
    return apiClient.get<StravaActivityOut[]>(
      `/api/v1/strava/activities?${params.toString()}`,
      resolved?.signal,
    );
  },
  importActivity(projectId: string, payload: StravaImportRequest): Promise<StageOut> {
    return apiClient.post<StageOut>(`/api/v1/projects/${projectId}/import-strava`, payload);
  },
  importBatch(
    projectId: string,
    payload: StravaBatchImportRequest,
  ): Promise<StravaBatchImportOut> {
    return apiClient.post<StravaBatchImportOut>(
      `/api/v1/projects/${projectId}/import-strava/batch`,
      payload,
    );
  },
  status(signal?: AbortSignal): Promise<StravaImportStatusOut> {
    return apiClient.get<StravaImportStatusOut>("/api/v1/strava/import-status", signal);
  },
  importStatus(signal?: AbortSignal): Promise<StravaImportStatusOut> {
    return apiClient.get<StravaImportStatusOut>("/api/v1/strava/import-status", signal);
  },
  importAll(payload: StravaImportAllRequest = {}): Promise<StravaImportAllOut> {
    return apiClient.post<StravaImportAllOut>("/api/v1/strava/import-all", payload);
  },
};

export const shareApi = {
  get(slug: string, signal?: AbortSignal): Promise<PublicProjectOut> {
    return apiClient.get<PublicProjectOut>(`/api/v1/share/${encodeURIComponent(slug)}`, signal);
  },
  geometry(
    slug: string,
    options?: { lod?: LodLevel; signal?: AbortSignal },
  ): Promise<TelemetryGeometryOut> {
    const lod = options?.lod ?? "medium";
    return apiClient.get<TelemetryGeometryOut>(
      `/api/v1/share/${encodeURIComponent(slug)}/geometry?lod=${encodeURIComponent(lod)}`,
      options?.signal,
    );
  },
  stageGeometry(
    slug: string,
    stageId: string,
    options?: { lod?: LodLevel; signal?: AbortSignal },
  ): Promise<TelemetryGeometryFeature> {
    const lod = options?.lod ?? "medium";
    return apiClient.get<TelemetryGeometryFeature>(
      `/api/v1/share/${encodeURIComponent(slug)}/stages/${encodeURIComponent(stageId)}/geometry?lod=${encodeURIComponent(lod)}`,
      options?.signal,
    );
  },
  chart(
    slug: string,
    options?: { maxPoints?: number; signal?: AbortSignal },
  ): Promise<TelemetryChartOut> {
    const maxPoints = options?.maxPoints ?? 1200;
    return apiClient.get<TelemetryChartOut>(
      `/api/v1/share/${encodeURIComponent(slug)}/chart?max_points=${encodeURIComponent(String(maxPoints))}`,
      options?.signal,
    );
  },
  videoUrl(slug: string): string {
    return `/api/v1/share/${encodeURIComponent(slug)}/render/video`;
  },
  photoUrl(slug: string, photoId: string, variant: "full" | "thumb" = "thumb"): string {
    return `/api/v1/share/${encodeURIComponent(slug)}/photos/${encodeURIComponent(photoId)}/image?variant=${variant}`;
  },
  highlights(slug: string, signal?: AbortSignal): Promise<ProjectHighlightsOut> {
    return apiClient.get<ProjectHighlightsOut>(
      `/api/v1/share/${encodeURIComponent(slug)}/analytics/highlights`,
      signal,
    );
  },
};

export const photosApi = {
  list(projectId: string, signal?: AbortSignal): Promise<ProjectPhotoOut[]> {
    return apiClient.get<ProjectPhotoOut[]>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/photos`,
      signal,
    );
  },
  upload(
    projectId: string,
    file: File,
    options?: { caption?: string; taken_at_utc?: string; lat?: number; lon?: number },
  ): Promise<ProjectPhotoOut> {
    const formData = new FormData();
    formData.append("file", file);
    if (options?.caption && options.caption.trim().length > 0) {
      formData.append("caption", options.caption.trim());
    }
    if (options?.taken_at_utc && options.taken_at_utc.trim().length > 0) {
      formData.append("taken_at_utc", options.taken_at_utc.trim());
    }
    if (options?.lat !== undefined && Number.isFinite(options.lat)) {
      formData.append("lat", String(options.lat));
    }
    if (options?.lon !== undefined && Number.isFinite(options.lon)) {
      formData.append("lon", String(options.lon));
    }
    return apiClient.post<ProjectPhotoOut>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/photos`,
      formData,
    );
  },
  update(
    projectId: string,
    photoId: string,
    payload: ProjectPhotoUpdateRequest,
  ): Promise<ProjectPhotoOut> {
    return apiClient.patch<ProjectPhotoOut>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/photos/${encodeURIComponent(photoId)}`,
      payload,
    );
  },
  delete(projectId: string, photoId: string): Promise<void> {
    return apiClient.delete<void>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/photos/${encodeURIComponent(photoId)}`,
    );
  },
  imageUrl(
    projectId: string,
    photoId: string,
    variant: "full" | "thumb" = "thumb",
  ): string {
    return `/api/v1/projects/${encodeURIComponent(projectId)}/photos/${encodeURIComponent(photoId)}/image?variant=${variant}`;
  },
  shiftClock(
    projectId: string,
    payload: PhotoShiftClockRequest,
  ): Promise<PhotoShiftClockOut> {
    return apiClient.post<PhotoShiftClockOut>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/photos/shift-clock`,
      payload,
    );
  },
  pin(
    projectId: string,
    photoId: string,
    payload: PhotoPinRequest,
  ): Promise<ProjectPhotoOut> {
    return apiClient.post<ProjectPhotoOut>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/photos/${encodeURIComponent(photoId)}/pin`,
      payload,
    );
  },
};

export const analyticsApi = {
  career(options?: {
    year?: number;
    activityType?: ActivityType;
    signal?: AbortSignal;
  }): Promise<CareerAnalyticsOut> {
    const params = new URLSearchParams();
    if (options?.year !== undefined) {
      params.set("year", String(options.year));
    }
    if (options?.activityType) {
      params.set("activity_type", options.activityType);
    }
    const qs = params.toString();
    return apiClient.get<CareerAnalyticsOut>(
      `/api/v1/analytics/career${qs ? `?${qs}` : ""}`,
      options?.signal,
    );
  },
  heatmap(options?: {
    year?: number;
    activityType?: ActivityType;
    lod?: LodLevel;
    signal?: AbortSignal;
  }): Promise<AnalyticsHeatmapOut> {
    const params = new URLSearchParams();
    if (options?.year !== undefined) {
      params.set("year", String(options.year));
    }
    if (options?.activityType) {
      params.set("activity_type", options.activityType);
    }
    if (options?.lod) {
      params.set("lod", options.lod);
    }
    const qs = params.toString();
    return apiClient.get<AnalyticsHeatmapOut>(
      `/api/v1/analytics/heatmap${qs ? `?${qs}` : ""}`,
      options?.signal,
    );
  },
};

export const rendersApi = {
  create(
    projectId: string,
    spec: RenderSpecV2,
    idempotencyKey = generateIdempotencyKey(),
  ): Promise<RenderJobOut> {
    return apiClient.post<RenderJobOut>(`/api/v1/projects/${projectId}/renders`, spec, {
      "Idempotency-Key": idempotencyKey,
    });
  },
  listForProject(projectId: string, signal?: AbortSignal): Promise<RenderJobOut[]> {
    return apiClient.get<RenderJobOut[]>(`/api/v1/projects/${projectId}/renders`, signal);
  },
  get(jobId: string, signal?: AbortSignal): Promise<RenderJobOut> {
    return apiClient.get<RenderJobOut>(`/api/v1/renders/${jobId}`, signal);
  },
  cancel(jobId: string): Promise<RenderJobOut> {
    return apiClient.post<RenderJobOut>(`/api/v1/renders/${jobId}/cancel`);
  },
  downloadUrl(jobId: string): string {
    return `/api/v1/renders/${jobId}/download`;
  },
  thumbnailUrl(jobId: string): string {
    return `/api/v1/renders/${jobId}/thumbnail`;
  },
  eventsUrl(jobId: string): string {
    return `/api/v1/renders/${jobId}/events`;
  },
};
