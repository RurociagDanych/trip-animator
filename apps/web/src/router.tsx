import {
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from "@tanstack/react-router";
import { DashboardPage } from "./routes/DashboardPage";
import { EditorPage } from "./routes/EditorPage";
import { LoginPage } from "./routes/LoginPage";
import { NotFoundPage } from "./routes/NotFoundPage";
import { RenderHarnessPage } from "./routes/RenderHarnessPage";
import { ShareViewPage } from "./routes/ShareViewPage";
import { StravaCallbackPage } from "./routes/StravaCallbackPage";

const rootRoute = createRootRoute({
  component: Outlet,
});

const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: DashboardPage,
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
});

const stravaCallbackRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/strava/callback",
  component: StravaCallbackPage,
});

const editorRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/projects/$projectId",
  component: EditorPage,
});

const renderHarnessRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/render/$jobId",
  component: RenderHarnessPage,
});

const shareViewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/share/$slug",
  component: ShareViewPage,
});

const routeTree = rootRoute.addChildren([
  dashboardRoute,
  loginRoute,
  stravaCallbackRoute,
  editorRoute,
  renderHarnessRoute,
  shareViewRoute,
]);

export function createAppRouter(history?: RouterHistory) {
  return createRouter({
    routeTree,
    history,
    defaultPreload: "intent",
    defaultNotFoundComponent: NotFoundPage,
  });
}

export const appRouter = createAppRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof appRouter;
  }
}
