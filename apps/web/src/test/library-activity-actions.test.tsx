import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LibraryActivityActions } from "../components/dashboard/LibraryActivityActions";
import { projectsApi } from "../lib/api/endpoints";
import { libraryActionsApi } from "../lib/api/library-actions";
import type { VaultActivityDetailOut } from "../lib/api/types";

const preview = {
  operation: "disconnect" as const,
  confirmation: "DISCONNECT",
  project_usages: [{ project_id: "p1", title: "Tour", stage_id: "s1" }],
  preserves_project_material: true as const,
  permanent_purge_available: false as const,
  retention_notice: "Source activity data is retained.",
};
const activity = { id: "a1", name: "Ride", hidden: false } as VaultActivityDetailOut;
function TripsList() {
  const projects = useQuery({ queryKey: ["projects"], queryFn: ({ signal }) => projectsApi.list(signal) });
  return <ul aria-label="Trips">{projects.data?.map((project) => <li key={project.id}>{project.title}</li>)}</ul>;
}

function renderActions(props: Partial<React.ComponentProps<typeof LibraryActivityActions>> = {}, sibling?: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><LibraryActivityActions ownerId="owner-1" selectedIds={["a1", "a2"]} activity={activity} onClearSelection={vi.fn()} onDisconnected={vi.fn()} {...props} />{sibling}</QueryClientProvider>);
}

afterEach(() => vi.restoreAllMocks());

describe("LibraryActivityActions", () => {
  it("creates a project with the selected activity IDs and refreshes Trips", async () => {
    type Project = Awaited<ReturnType<typeof projectsApi.list>>[number];
    const projects: Project[] = [{ id: "p0", title: "Existing trip" } as Project];
    vi.spyOn(projectsApi, "list").mockImplementation(async () => [...projects]);
    const create = vi.spyOn(libraryActionsApi, "createProject").mockImplementation(async () => {
      projects.push({ id: "p1", title: "Alps" } as Project);
      return { project_id: "p1", title: "Alps", stage_ids: [] };
    });
    renderActions({ eligibilityById: { a1: "ready", a2: "ready" } }, <TripsList />);
    await waitFor(() => expect(screen.getByRole("list", { name: "Trips" })).toHaveTextContent("Existing trip"));
    fireEvent.change(screen.getByRole("textbox", { name: "New project title" }), { target: { value: "Alps" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith("Alps", ["a1", "a2"]));
    await waitFor(() => expect(screen.getByRole("list", { name: "Trips" })).toHaveTextContent("Alps"));
  });

  it("keeps reuse waiting when eligibility is not confirmed for an off-page selection", async () => {
    vi.spyOn(projectsApi, "list").mockResolvedValue([]);
    const create = vi.spyOn(libraryActionsApi, "createProject").mockResolvedValue({ project_id: "p1", title: "Alps", stage_ids: [] });
    renderActions({ eligibilityById: { a1: "ready" } });
    fireEvent.change(screen.getByRole("textbox", { name: "New project title" }), { target: { value: "Alps" } });
    expect(screen.getByText(/a2.*readiness is still being checked/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create project" })).toBeDisabled();
    expect(create).not.toHaveBeenCalled();
  });

  it("prevents adding a mixed selection containing a no-GPS activity", async () => {
    vi.spyOn(projectsApi, "list").mockResolvedValue([{ id: "p1", title: "Alps" } as never]);
    const append = vi.spyOn(libraryActionsApi, "appendToProject").mockResolvedValue({ project_id: "p1", title: "Alps", stage_ids: [] });
    renderActions({ eligibilityById: { a1: "ready", a2: "no_gps" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Append to project" }), { target: { value: "p1" } });
    await screen.findByRole("option", { name: "Alps" });
    fireEvent.change(screen.getByRole("combobox", { name: "Append to project" }), { target: { value: "p1" } });
    expect(screen.getByText(/a2.*no GPS track/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add to project" })).toBeDisabled();
    expect(append).not.toHaveBeenCalled();
  });

  it("requires explicit confirmation and presents retention before disconnecting", async () => {
    vi.spyOn(projectsApi, "list").mockResolvedValue([]);
    const getPreview = vi.spyOn(libraryActionsApi, "disconnectPreview").mockResolvedValue(preview);
    const disconnect = vi.spyOn(libraryActionsApi, "disconnect").mockResolvedValue({ id: "a1", disconnected: true, permanently_deleted: false });
    renderActions();
    fireEvent.click(screen.getByRole("button", { name: "Review disconnect" }));
    expect(await screen.findByText(preview.retention_notice)).toBeInTheDocument();
    expect(screen.getByText(/not permanent deletion/)).toBeInTheDocument();
    expect(getPreview).toHaveBeenCalledWith("a1");
    const submit = screen.getByRole("button", { name: "Disconnect from catalog" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Disconnect confirmation" }), { target: { value: "DISCONNECT" } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(disconnect).toHaveBeenCalledWith("a1", "DISCONNECT"));
  });
});
