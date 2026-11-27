import { createReadStream, realpathSync, statSync } from "node:fs";
import path from "node:path";
import type { Plugin } from "vite";

export const MARKED_TRAILS_URL_PREFIX = "/tiles/marked-trails/";

/**
 * Returns the absolute path of a regular file in the store for a decoded relative URL path,
 * or null. Hidden files (the publisher's temporary files) and anything that resolves outside
 * the store, including through a symlink, are refused.
 */
export function resolveMarkedTrailsStoreFile(root: string, relativePath: string): string | null {
  if (relativePath.split(/[\\/]/).some((segment) => segment.startsWith("."))) {
    return null;
  }
  let realRoot: string;
  let realTarget: string;
  try {
    realRoot = realpathSync(root);
    realTarget = realpathSync(path.resolve(root, relativePath));
  } catch {
    return null;
  }
  if (!realTarget.startsWith(realRoot + path.sep)) {
    return null;
  }
  if (path.relative(realRoot, realTarget).split(path.sep).some((segment) => segment.startsWith("."))) {
    return null;
  }
  try {
    return statSync(realTarget).isFile() ? realTarget : null;
  } catch {
    return null;
  }
}

/**
 * Serves the local Marked Trails store under the same origin as the editor, so the browser
 * reads PMTiles with Range requests and no CORS setup. Production uses CloudFront instead.
 */
export function serveMarkedTrailsStore(storeDir: string): Plugin {
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
        const target = resolveMarkedTrailsStoreFile(root, relative);
        if (!target) {
          res.statusCode = 404;
          res.end();
          return;
        }
        const size = statSync(target).size;
        const range = parseSingleRange(req.headers.range, size);
        if (range === "invalid") {
          res.statusCode = 416;
          res.setHeader("Content-Range", `bytes */${size}`);
          res.end();
          return;
        }
        const isBuild = relative.startsWith("builds/");
        res.setHeader("Accept-Ranges", "bytes");
        res.setHeader(
          "Content-Type",
          relative.endsWith(".json") ? "application/json" : "application/vnd.pmtiles",
        );
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
