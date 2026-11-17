import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { useState } from "react";
import { createAppQueryClient } from "./lib/query/query-client";
import { appRouter } from "./router";

export interface AppProps {
  router?: typeof appRouter;
  queryClient?: QueryClient;
}

export function App({ router = appRouter, queryClient }: AppProps) {
  const [defaultQueryClient] = useState(() => queryClient ?? createAppQueryClient());

  return (
    <QueryClientProvider client={defaultQueryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
