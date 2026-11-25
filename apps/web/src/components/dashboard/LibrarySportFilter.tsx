/** Show the account's available sports in a checkbox dropdown. */
import { useQuery } from "@tanstack/react-query";
import { useId, useRef } from "react";
import { vaultApi, type VaultActivityFilters } from "../../lib/api/endpoints";
import { ACTIVITY_ICONS, isActivityType } from "../icons/ActivityIcons";

type Props = {
  ownerId: string | null;
  includeHidden: boolean;
  value: VaultActivityFilters["sport"];
  onChange: (sports: string[] | undefined) => void;
};

/** Keep unavailable saved selections visible so they can still be removed. */
export function LibrarySportFilter({ ownerId, includeHidden, value, onChange }: Props) {
  const labelId = useId();
  const summaryId = useId();
  const summary = useRef<HTMLElement>(null);
  const sports = useQuery({
    queryKey: ["vault", ownerId, "sports", includeHidden],
    queryFn: ({ signal }) => vaultApi.sports(includeHidden, signal),
    enabled: Boolean(ownerId),
    retry: false,
  });
  const selected = typeof value === "string" ? (value ? [value] : []) : value ?? [];
  const available = new Set(sports.data ?? []);
  const options = [...new Set([...available, ...selected])].map((sport) => ({
    sport,
    label: isActivityType(sport) ? ACTIVITY_ICONS[sport].label : sport,
  })).sort((a, b) => a.label.localeCompare(b.label));

  return <div className="min-w-0 text-xs text-ink-muted">
    <span id={labelId}>Sport</span>
    <details className="group relative mt-1" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false;
    }} onKeyDown={(event) => {
      if (event.key !== "Escape" || !event.currentTarget.open) return;
      event.stopPropagation();
      summary.current?.focus();
      event.currentTarget.open = false;
    }}>
      <summary ref={summary} aria-labelledby={`${labelId} ${summaryId}`} className="flex cursor-pointer list-none items-center justify-between gap-2 rounded border border-border bg-surface px-2 py-1.5 text-sm text-ink [&::-webkit-details-marker]:hidden">
        <span id={summaryId}>{selected.length ? `${selected.length} selected` : "All sports"}</span>
        <span aria-hidden="true" className="transition-transform group-open:rotate-180">▾</span>
      </summary>
      <div role="group" aria-labelledby={labelId} className="absolute inset-x-0 top-full z-20 mt-1 max-h-60 overflow-y-auto rounded border border-border bg-surface p-2 text-sm text-ink shadow-lg">
        {sports.isLoading && <p>Loading sports…</p>}
        {sports.isError && <p>Could not load sports. <button type="button" className="underline" onClick={() => void sports.refetch()}>Retry</button></p>}
        {sports.isSuccess && available.size === 0 && <p>No sports available.</p>}
        {options.map(({ sport, label }) => <label key={sport} className="flex cursor-pointer items-start gap-2 rounded px-1 py-1.5 hover:bg-canvas">
          <input className="mt-0.5 shrink-0" type="checkbox" checked={selected.includes(sport)} onChange={(event) => {
            const next = event.currentTarget.checked ? [...selected, sport].sort() : selected.filter((item) => item !== sport);
            onChange(next.length ? next : undefined);
          }} />
          <span className="min-w-0 break-words">{label}{sports.isSuccess && !available.has(sport) && <span className="block text-xs text-ink-muted">Not in current catalog</span>}</span>
        </label>)}
        {selected.length > 0 && <button type="button" className="mt-1 rounded border border-border px-2 py-1" onClick={() => {
          summary.current?.focus();
          onChange(undefined);
        }}>Clear sports</button>}
      </div>
    </details>
  </div>;
}
