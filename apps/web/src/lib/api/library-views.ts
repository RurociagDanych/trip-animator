/** Private rule-based Activity Library view API. */
import { apiRequest } from "./client";
import type { VaultActivityFilters } from "./endpoints";

export interface LibraryViewRules {
  version: 1;
  filters: Omit<VaultActivityFilters, "sort" | "order" | "offset" | "limit"> & { relative_days?: number };
  sort: string;
  direction: string;
}

export interface LibrarySavedView {
  id: string;
  name: string;
  rules: LibraryViewRules;
  resolved_filters: VaultActivityFilters;
}

export const libraryViewsApi = {
  list(signal?: AbortSignal): Promise<{ items: LibrarySavedView[] }> {
    return apiRequest("/api/v1/vault/views", { signal });
  },
  create(name: string, rules: LibraryViewRules): Promise<LibrarySavedView> {
    return apiRequest("/api/v1/vault/views", { method: "POST", body: { name, rules } });
  },
  open(id: string): Promise<LibrarySavedView> {
    return apiRequest(`/api/v1/vault/views/${encodeURIComponent(id)}`);
  },
  remove(id: string): Promise<void> {
    return apiRequest(`/api/v1/vault/views/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
};
