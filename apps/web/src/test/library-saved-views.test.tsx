/** Regression coverage for server-backed rule-based views. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LibrarySavedViews } from "../components/dashboard/LibrarySavedViews";
import { libraryViewsApi, type LibrarySavedView } from "../lib/api/library-views";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("saves relative rules without pagination and opens freshly resolved dates", async () => {
  const view: LibrarySavedView = { id: "view-1", name: "Recent rides", rules: { version: 1, filters: { sport: "Ride", relative_days: 7 }, sort: "distance_m", direction: "asc" }, resolved_filters: { sport: "Ride", start_date: "2026-11-15", end_date: "2026-11-21" } };
  vi.spyOn(libraryViewsApi, "list").mockResolvedValue({ items: [view] });
  const create = vi.spyOn(libraryViewsApi, "create").mockResolvedValue(view);
  const open = vi.spyOn(libraryViewsApi, "open").mockResolvedValue(view);
  const apply = vi.fn();
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><LibrarySavedViews ownerId="owner-1" filters={{ sport: "Ride", start_date: "2020-01-01", sort: "distance_m", order: "asc", offset: 100, limit: 50 }} onApply={apply} /></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText("Saved view name"), { target: { value: "Recent rides" } });
  fireEvent.change(screen.getByLabelText("Saved view date rule"), { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "Save view" }));
  await waitFor(() => expect(create).toHaveBeenCalledWith("Recent rides", view.rules));
  await screen.findByRole("option", { name: "Recent rides" });
  fireEvent.change(screen.getByLabelText("Saved view"), { target: { value: "view-1" } });
  fireEvent.click(screen.getByRole("button", { name: "Open view" }));
  await waitFor(() => expect(open).toHaveBeenCalledWith("view-1"));
  await waitFor(() => expect(apply).toHaveBeenCalledWith({ ...view.resolved_filters, sort: "distance_m", order: "asc", offset: 0, limit: 50 }));
});
