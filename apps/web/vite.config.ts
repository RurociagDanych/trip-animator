import { createReadStream, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { type Plugin, defineConfig } from "vite";

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";

const MARKED_TRAILS_URL_PREFIX = "/tiles/marked-trails/";
const DEFAULT_MARKED_TRAILS_STORE = fileURLToPath(
  new URL("../../.tmp/marked-trails/store", import.meta.url),
);

/**
 * Serves the local Marked Trails store under the same origin as the editor, so the browser
 * reads PMTiles with Range requests and no CORS setup. Production uses CloudFront instead.
 */
function serveMarkedTrailsStore(storeDir: string): Plugin {
  const root = path.resolve(process.env.MARKED_TRAILS_STORE_DIR || storeDir);
  return {
    name: "marked-trails-local-store",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const urlPath = (req.url ?? "").split("?")[0] ?? "";
        if (!urlPath.startsWith(MARKED_TRAILS_URL_PREFIX)) {
          next();
          return;
        }
        let relative: string;
        try {
          relative = decodeURIComponent(urlPath.slice(MARKED_TRAILS_URL_PREFIX.length));
        } catch {
          res.statusCode = 400;
          res.end();
          return;
        }
        const target = path.resolve(root, relative);
        const isStoreObject = target.startsWith(root + path.sep);
        const stat = isStoreObject && statSafely(target);
        if (!stat || !stat.isFile()) {
          res.statusCode = 404;
          res.end();
          return;
        }
        const size = stat.size;
        const range = parseSingleRange(req.headers.range, size);
        if (range === "invalid") {
          res.statusCode = 416;
          res.setHeader("Content-Range", `bytes */${size}`);
          res.end();
          return;
        }
        const isBuild = relative.startsWith("builds/");
        res.setHeader("Accept-Ranges", "bytes");
        res.setHeader("Content-Type", relative.endsWith(".json") ? "application/json" : "application/vnd.pmtiles");
        res.setHeader(
          "Cache-Control",
          isBuild ? "public, max-age=31536000, immutable" : "no-cache, no-store, must-revalidate",
        );
        if (range) {
          res.statusCode = 206;
          res.setHeader("Content-Range", `bytes ${range.start}-${range.end}/${size}`);
          res.setHeader("Content-Length", String(range.end - range.start + 1));
          createReadStream(target, { start: range.start, end: range.end }).pipe(res);
          return;
        }
        res.statusCode = 200;
        res.setHeader("Content-Length", String(size));
        createReadStream(target).pipe(res);
      });
    },
  };
}

function statSafely(target: string) {
  try {
    return statSync(target);
  } catch {
    return null;
  }
}

/** Parses `bytes=start-end` (or `bytes=start-`); PMTiles readers send only these forms. */
function parseSingleRange(
  header: string | undefined,
  size: number,
): { start: number; end: number } | "invalid" | null {
  if (!header) {
    return null;
  }
  const match = /^bytes=(\d+)-(\d*)$/.exec(header.trim());
  if (!match) {
    return "invalid";
  }
  const start = Number(match[1]);
  const end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  if (start > end || start >= size) {
    return "invalid";
  }
  return { start, end };
}

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
