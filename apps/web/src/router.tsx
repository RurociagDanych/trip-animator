import {
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  type RouterHistory,
} from "@tanstack/react-router";
import { RouteLoadingFallback } from "./components/layout/RouteLoadingFallback";
import { DashboardPage } from "./routes/DashboardPage";
import { LoginPage } from "./routes/LoginPage";
import { NotFoundPage } from "./routes/NotFoundPage";
import { StravaCallbackPage } from "./routes/StravaCallbackPage";

const LazyEditorPage = lazyRouteComponent(
  () => import("./routes/EditorPage"),
  "EditorPage",
);
const LazyRenderHarnessPage = lazyRouteComponent(
  () => import("./routes/RenderHarnessPage"),
  "RenderHarnessPage",
);
const LazyShareViewPage = lazyRouteComponent(
  () => import("./routes/ShareViewPage"),
  "ShareViewPage",
);

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
  component: LazyEditorPage,
  pendingComponent: RouteLoadingFallback,
});

const renderHarnessRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/render/$jobId",
  component: LazyRenderHarnessPage,
});

const shareViewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/share/$slug",
  component: LazyShareViewPage,
  pendingComponent: RouteLoadingFallback,
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
