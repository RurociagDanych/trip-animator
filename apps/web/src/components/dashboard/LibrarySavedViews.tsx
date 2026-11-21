/** Manage private server-backed Activity Library query definitions. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { VaultActivityFilters } from "../../lib/api/endpoints";
import { libraryViewsApi, type LibraryViewRules } from "../../lib/api/library-views";

interface Props {
  ownerId: string;
  filters: VaultActivityFilters;
  onApply: (filters: VaultActivityFilters) => void;
}

/** Save query rules rather than activity IDs and resolve relative dates on opening. */
export function LibrarySavedViews({ ownerId, filters, onApply }: Props) {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [relativeDays, setRelativeDays] = useState("");
  const [selected, setSelected] = useState("");
  const queryKey = ["vault", "saved-views", ownerId];
  const views = useQuery({ queryKey, queryFn: ({ signal }) => libraryViewsApi.list(signal), enabled: Boolean(ownerId), retry: false });
  const save = useMutation({
    mutationFn: () => {
      const { sort, order, ...selection } = filters;
      delete selection.offset;
      delete selection.limit;
      if (relativeDays) {
        delete selection.start_date;
        delete selection.end_date;
      }
      const rules: LibraryViewRules = {
        version: 1,
        filters: { ...selection, ...(relativeDays ? { relative_days: Number(relativeDays) } : {}) },
        sort: sort ?? "started_at",
        direction: order ?? "desc",
      };
      return libraryViewsApi.create(name.trim(), rules);
    },
    onSuccess: () => { setName(""); void client.invalidateQueries({ queryKey }); },
  });
  const open = useMutation({
    mutationFn: (id: string) => libraryViewsApi.open(id),
  });
  const remove = useMutation({
    mutationFn: libraryViewsApi.remove,
    onSuccess: () => { setSelected(""); void client.invalidateQueries({ queryKey }); },
  });
  const errors = [views.error, save.error, open.error, remove.error].filter(Boolean);
  return <fieldset className="rounded border border-border p-3">
    <legend className="px-1 text-sm font-medium">Private saved views</legend>
    <div className="flex flex-wrap items-end gap-2">
      <label className="text-xs">View name<input aria-label="Saved view name" maxLength={120} value={name} onChange={(event) => setName(event.target.value)} className="block rounded border border-border bg-surface p-2" /></label>
      <label className="text-xs">Date rule<select aria-label="Saved view date rule" value={relativeDays} onChange={(event) => setRelativeDays(event.target.value)} className="block rounded border border-border bg-surface p-2"><option value="">Current dates</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="365">Last 365 days</option></select></label>
      <button className="rounded border border-border px-3 py-2 text-sm disabled:opacity-40" disabled={!name.trim() || save.isPending || !ownerId} onClick={() => save.mutate()}>Save view</button>
      <label className="text-xs">Saved view<select aria-label="Saved view" value={selected} onChange={(event) => setSelected(event.target.value)} className="block max-w-full rounded border border-border bg-surface p-2"><option value="">Choose a view</option>{views.data?.items.map((view) => <option value={view.id} key={view.id}>{view.name}</option>)}</select></label>
      <button className="rounded border border-border px-3 py-2 text-sm disabled:opacity-40" disabled={!selected || open.isPending} onClick={() => open.mutate(selected, { onSuccess: (view) => onApply({ ...view.resolved_filters, sort: view.rules.sort ?? "started_at", order: view.rules.direction ?? "desc", offset: 0, limit: 50 }) })}>Open view</button>
      <button className="rounded border border-border px-3 py-2 text-sm disabled:opacity-40" disabled={!selected || remove.isPending} onClick={() => remove.mutate(selected)}>Delete view</button>
    </div>
    {errors.map((error, index) => <p role="alert" key={index} className="text-sm text-red-600">{error instanceof Error ? error.message : "Could not update saved views."}</p>)}
  </fieldset>;
}
