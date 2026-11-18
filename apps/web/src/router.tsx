import {
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  type RouterHistory,
} from "@tanstack/react-router";
import { RouteLoadingFallback } from "./components/layout/RouteLoadingFallback";
import { AccountPage } from "./routes/AccountPage";
import { DashboardPage } from "./routes/DashboardPage";
import { ForgotPasswordPage } from "./routes/ForgotPasswordPage";
import { LoginPage } from "./routes/LoginPage";
import { NotFoundPage } from "./routes/NotFoundPage";
import { ResetPasswordPage } from "./routes/ResetPasswordPage";
import { StravaCallbackPage } from "./routes/StravaCallbackPage";
import { VerifyEmailPage } from "./routes/VerifyEmailPage";

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

const verifyEmailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/verify-email",
  component: VerifyEmailPage,
});

const forgotPasswordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/forgot-password",
  component: ForgotPasswordPage,
});

const resetPasswordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/reset-password",
  component: ResetPasswordPage,
});

const accountRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/account",
  component: AccountPage,
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
  verifyEmailRoute,
  forgotPasswordRoute,
  resetPasswordRoute,
  accountRoute,
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
