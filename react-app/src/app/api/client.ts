/**
 * Fetch wrapper for ProPaths backend.
 *
 * Backend conventions:
 *   - JSON endpoints: `/api/results/<protein>`, `/api/chain/<id>`,
 *     `/api/pathway/<id>/interactors`, `/api/claims/<protein>`,
 *     `/api/protein/<symbol>/interactions`.
 *   - On error: `{error: {code, message}}` shape via `services.error_helpers.error_response`.
 *
 * No abort controllers or retry — TanStack Query handles caching + retries.
 */

export class ApiError extends Error {
  status: number;
  code: string | null;
  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const DEFAULT_HEADERS: HeadersInit = {
  "Accept": "application/json",
};

interface ErrorEnvelope {
  error?: { code?: string; message?: string };
}

export async function getJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { ...DEFAULT_HEADERS, ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    let code: string | null = null;
    try {
      const body = (await res.json()) as ErrorEnvelope;
      if (body?.error?.message) message = body.error.message;
      if (body?.error?.code) code = body.error.code;
    } catch {
      // body wasn't JSON; keep the HTTP status as the message.
    }
    throw new ApiError(message, res.status, code);
  }
  return (await res.json()) as T;
}
