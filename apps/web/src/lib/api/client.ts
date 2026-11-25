export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

const MUTATING_METHODS: ReadonlySet<HttpMethod> = new Set([
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
]);

const CSRF_HEADER_NAME = "X-CSRF-Token";
const CSRF_COOKIE_NAMES = ["trip_csrf", "csrf_token"] as const;

let cachedCsrfToken: string | null = null;

export class ApiError extends Error {
  public readonly status: number;
  public readonly code: string;
  public readonly details: unknown;

  public constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function setCsrfToken(token: string | null): void {
  cachedCsrfToken = token && token.trim().length > 0 ? token.trim() : null;
}

export function clearCsrfToken(): void {
  cachedCsrfToken = null;
}

function readCookieValue(name: string): string | null {
  if (typeof document === "undefined" || !document.cookie) {
    return null;
  }
  const cookies = document.cookie.split(";");
  for (const rawEntry of cookies) {
    const trimmed = rawEntry.trim();
    const separatorIndex = trimmed.indexOf("=");
    if (separatorIndex <= 0) {
      continue;
    }
    const key = trimmed.slice(0, separatorIndex).trim();
    if (key === name) {
      const value = trimmed.slice(separatorIndex + 1).trim();
      return value ? decodeURIComponent(value) : null;
    }
  }
  return null;
}

export function getCsrfToken(): string | null {
  if (cachedCsrfToken) {
    return cachedCsrfToken;
  }
  for (const cookieName of CSRF_COOKIE_NAMES) {
    const fromCookie = readCookieValue(cookieName);
    if (fromCookie) {
      return fromCookie;
    }
  }
  return null;
}

export function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `render-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

export interface ApiRequestOptions {
  method?: HttpMethod;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractErrorFields(payload: unknown, fallbackStatus: number): { code: string; message: string } {
  if (isPlainRecord(payload)) {
    const detail = payload.detail;
    if (typeof detail === "string" && detail.trim().length > 0) {
      return {
        code: typeof payload.code === "string" ? payload.code : `HTTP_${fallbackStatus}`,
        message: detail,
      };
    }
    const title = payload.title;
    if (typeof title === "string" && title.trim().length > 0) {
      return {
        code: typeof payload.code === "string" ? payload.code : `HTTP_${fallbackStatus}`,
        message: title,
      };
    }
    const message = payload.message;
    if (typeof message === "string" && message.trim().length > 0) {
      return {
        code: typeof payload.code === "string" ? payload.code : `HTTP_${fallbackStatus}`,
        message,
      };
    }
  }
  return {
    code: `HTTP_${fallbackStatus}`,
    message: `Request failed with status ${fallbackStatus}`,
  };
}

export async function apiRequest<TResponse>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<TResponse> {
  const method = options.method ?? "GET";
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");

  let requestBody: BodyInit | undefined;
  if (options.body instanceof FormData) {
    requestBody = options.body;
  } else if (options.body !== undefined) {
    if (!headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    requestBody = JSON.stringify(options.body);
  }

  if (MUTATING_METHODS.has(method)) {
    const csrfToken = getCsrfToken();
    if (csrfToken) {
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }
  }

  const response = await fetch(path, {
    method,
    headers,
    body: requestBody,
    credentials: "same-origin",
    signal: options.signal,
  });

  const responseCsrf = response.headers.get(CSRF_HEADER_NAME);
  if (responseCsrf) {
    setCsrfToken(responseCsrf);
  }

  const contentType = response.headers.get("Content-Type") ?? "";
  const hasJsonBody = contentType.includes("application/json");
  let parsedPayload: unknown = null;

  if (response.status !== 204) {
    if (hasJsonBody) {
      parsedPayload = (await response.json()) as unknown;
    } else {
      const text = await response.text();
      parsedPayload = text.length > 0 ? text : null;
    }
  }

  if (isPlainRecord(parsedPayload)) {
    if (typeof parsedPayload.csrf_token === "string") {
      setCsrfToken(parsedPayload.csrf_token);
    } else if (typeof parsedPayload.csrfToken === "string") {
      setCsrfToken(parsedPayload.csrfToken);
    }
  }

  if (!response.ok) {
    const { code, message } = extractErrorFields(parsedPayload, response.status);
    throw new ApiError(response.status, code, message, parsedPayload);
  }

  return parsedPayload as TResponse;
}

export const apiClient = {
  get<TResponse>(path: string, signal?: AbortSignal): Promise<TResponse> {
    return apiRequest<TResponse>(path, { method: "GET", signal });
  },
  post<TResponse>(path: string, body?: unknown, headers?: Record<string, string>, signal?: AbortSignal): Promise<TResponse> {
    return apiRequest<TResponse>(path, { method: "POST", body, headers, signal });
  },
  put<TResponse>(path: string, body?: unknown, headers?: Record<string, string>): Promise<TResponse> {
    return apiRequest<TResponse>(path, { method: "PUT", body, headers });
  },
  patch<TResponse>(path: string, body?: unknown, headers?: Record<string, string>): Promise<TResponse> {
    return apiRequest<TResponse>(path, { method: "PATCH", body, headers });
  },
  delete<TResponse>(path: string, headers?: Record<string, string>): Promise<TResponse> {
    return apiRequest<TResponse>(path, { method: "DELETE", headers });
  },
};
