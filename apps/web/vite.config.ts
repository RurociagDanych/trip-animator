import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { serveMarkedTrailsStore } from "./vite-plugins/marked-trails-store";

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";

const DEFAULT_MARKED_TRAILS_STORE = fileURLToPath(
  new URL("../../.tmp/marked-trails/store", import.meta.url),
);

export default defineConfig({
  plugins: [react(), tailwindcss(), serveMarkedTrailsStore(DEFAULT_MARKED_TRAILS_STORE)],
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "preload-helper",
              test: /preload-helper/,
              minSize: 0,
            },
            {
              name: "maplibre-gl",
              test: /maplibre-gl(?!.*\.css)/,
            },
            {
              name: "deck-gl",
              test: /(?:@deck\.gl|@luma\.gl)(?!.*\.css)/,
            },
            {
              name: "uplot",
              test: /uplot(?!.*\.css)/,
            },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      "/health": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
