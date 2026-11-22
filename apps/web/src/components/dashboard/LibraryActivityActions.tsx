import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../../lib/api/client";
import { projectsApi } from "../../lib/api/endpoints";
import { libraryActionsApi } from "../../lib/api/library-actions";
import type { ProjectReuseEligibility, VaultActivityDetailOut } from "../../lib/api/types";

const buttonClass = "rounded border border-border px-3 py-1.5 text-sm disabled:opacity-50";

export function LibraryActivityActions({ ownerId, selectedIds, eligibilityById = {}, activity, onClearSelection, onDisconnected }: {
  ownerId: string | null; selectedIds: string[]; eligibilityById?: Record<string, ProjectReuseEligibility | undefined>; activity?: VaultActivityDetailOut; onClearSelection: () => void; onDisconnected: () => void;
}) {
  const client = useQueryClient();
  const [title, setTitle] = useState("");
  const [projectId, setProjectId] = useState("");
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof libraryActionsApi.disconnectPreview>> | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const projects = useQuery({ queryKey: ["projects", ownerId, "list"], queryFn: ({ signal }) => projectsApi.list(signal), enabled: Boolean(ownerId) });
  const invalidateOwner = () => { void client.invalidateQueries({ queryKey: ["vault", ownerId] }); void client.invalidateQueries({ queryKey: ["projects", ownerId] }); };
  const projectMutation = useMutation({
    mutationFn: async () => projectId ? libraryActionsApi.appendToProject(projectId, selectedIds) : libraryActionsApi.createProject(title.trim(), selectedIds),
    onSuccess: () => {
      invalidateOwner();
      void client.invalidateQueries({ queryKey: ["projects"], exact: true });
      onClearSelection();
      setTitle("");
    },
  });
  const visibility = useMutation({ mutationFn: (hidden: boolean) => libraryActionsApi.setHidden(activity!.id, hidden), onSuccess: () => { invalidateOwner(); void client.invalidateQueries({ queryKey: ["vault", "activity", ownerId, activity?.id] }); } });
  const disconnect = useMutation({ mutationFn: () => libraryActionsApi.disconnect(activity!.id, preview!.confirmation), onSuccess: () => { invalidateOwner(); onClearSelection(); onDisconnected(); setPreview(null); setConfirmation(""); } });
  const previewMutation = useMutation({ mutationFn: () => libraryActionsApi.disconnectPreview(activity!.id), onSuccess: (data) => { setPreview(data); setConfirmation(""); } });
  const error = projectMutation.error ?? visibility.error ?? disconnect.error ?? previewMutation.error;
  const notReady = selectedIds.filter((id) => eligibilityById[id] !== "ready");
  const canReuse = Boolean(ownerId) && selectedIds.length > 0 && notReady.length === 0;
  const reuseDisabledReason = notReady.map((id) => {
    const eligibility = eligibilityById[id];
    if (eligibility === "no_gps") return `${id}: no GPS track`;
    if (eligibility === "waiting" || eligibility === undefined) return `${id}: readiness is still being checked`;
    return `${id}: reusable track unavailable`;
  });
  return <div className="space-y-3 rounded border border-border bg-surface p-3">
    {selectedIds.length > 0 && <div className="space-y-2"><p>{selectedIds.length} activities selected</p>{reuseDisabledReason.length > 0 && <p role="status">Project reuse waiting: {reuseDisabledReason.join("; ")}.</p>}<label className="block text-xs text-ink-muted">New project title<input aria-label="New project title" className="ml-2 rounded border border-border bg-canvas px-2 py-1" value={title} onChange={(e) => setTitle(e.currentTarget.value)} /></label><label className="block text-xs text-ink-muted">Append to project<select aria-label="Append to project" className="ml-2 rounded border border-border bg-canvas px-2 py-1" value={projectId} onChange={(e) => setProjectId(e.currentTarget.value)}><option value="">Create a new project</option>{(projects.data ?? []).map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}</select></label><button className={buttonClass} disabled={!canReuse || projectMutation.isPending || (projectId === "" && !title.trim())} onClick={() => { if (canReuse) projectMutation.mutate(); }}>{projectMutation.isPending ? "Saving…" : projectId ? "Add to project" : "Create project"}</button><button className={buttonClass} onClick={onClearSelection}>Clear selection</button></div>}
    {activity && <div className="space-y-2 border-t border-border pt-3"><button className={buttonClass} disabled={visibility.isPending} onClick={() => visibility.mutate(!activity.hidden)}>{activity.hidden ? "Restore activity" : "Hide activity"}</button><button className={buttonClass} disabled={previewMutation.isPending || disconnect.isPending} onClick={() => previewMutation.mutate()}>Review disconnect</button>{preview && <div role="alertdialog" aria-label="Disconnect activity" className="space-y-2 rounded border border-amber-600 p-3"><p>{preview.retention_notice}</p><p>Stored source data and project copies are retained; disconnect is not permanent deletion.</p><p>Project usages ({preview.project_usages.length}): {preview.project_usages.length ? preview.project_usages.map((usage) => `${usage.title} (stage ${usage.stage_id})`).join(", ") : "none"}</p><label className="block">Type <strong>DISCONNECT</strong> to confirm<input aria-label="Disconnect confirmation" className="ml-2 rounded border border-border bg-canvas px-2 py-1" value={confirmation} onChange={(e) => setConfirmation(e.currentTarget.value)} /></label><button className={buttonClass} disabled={confirmation !== "DISCONNECT" || disconnect.isPending || !preview.preserves_project_material || preview.permanent_purge_available} onClick={() => disconnect.mutate()}>Disconnect from catalog</button><button className={buttonClass} onClick={() => setPreview(null)}>Cancel</button></div>}</div>}
    {projects.isError && <p role="alert">Could not load projects.</p>}{error && <p role="alert" className="text-red-600">{error instanceof ApiError ? error.message : "Activity Library action failed."}</p>}
  </div>;
}
