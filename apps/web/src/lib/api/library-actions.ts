import { apiClient } from "./client";
import type { LibraryDisconnectPreviewOut, ProjectOut } from "./types";

export const libraryActionsApi = {
  createProject(title: string, activity_ids: string[]) {
    return apiClient.post<{ project_id: string; title: string; stage_ids: string[] }>("/api/v1/library/projects", { title, activity_ids });
  },
  appendToProject(projectId: string, activity_ids: string[]) {
    return apiClient.post<{ project_id: string; title: string; stage_ids: string[] }>(`/api/v1/library/projects/${encodeURIComponent(projectId)}/activities`, { activity_ids });
  },
  projects(signal?: AbortSignal) {
    return apiClient.get<ProjectOut[]>("/api/v1/projects", signal);
  },
  setHidden(activityId: string, hidden: boolean) {
    return apiClient.patch<{ id: string; hidden: boolean }>(`/api/v1/vault/activities/${encodeURIComponent(activityId)}/visibility`, { hidden });
  },
  disconnectPreview(activityId: string, signal?: AbortSignal) {
    return apiClient.get<LibraryDisconnectPreviewOut>(`/api/v1/vault/activities/${encodeURIComponent(activityId)}/disconnect-preview`, signal);
  },
  disconnect(activityId: string, confirmation: string) {
    return apiClient.post<{ id: string; disconnected: boolean; permanently_deleted: boolean }>(`/api/v1/vault/activities/${encodeURIComponent(activityId)}/disconnect`, { confirmation });
  },
};
