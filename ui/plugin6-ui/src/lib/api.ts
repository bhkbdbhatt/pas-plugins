export type TenantId = string;

/** A failed API call, carrying the RFC 9457 problem detail when the server sent one. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail?: unknown
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

type Problem = { type?: string; title?: string; status?: number; detail?: string; code?: string };

/**
 * Call one plugin endpoint.
 *
 * The tenant header is always sent: the platform rejects a request without it, and
 * a UI that omitted it would look like an auth bug rather than a client bug.
 */
export async function call<T>(port: number, tenant: TenantId, path: string): Promise<T> {
  const response = await fetch(`http://localhost:${port}${path}`, {
    headers: { 'X-Tenant-Id': tenant, Accept: 'application/json' }
  });

  const text = await response.text();
  const body: unknown = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const problem = body as Problem | null;
    throw new ApiError(
      response.status,
      problem?.code ?? 'unknown',
      problem?.detail ?? problem?.title ?? `request failed with ${response.status}`,
      body
    );
  }
  return body as T;
}

export const api = {
  /** Describe what a section would show, without mutating anything. */
  async preview(port: number, tenant: TenantId, section: string): Promise<string> {
    try {
      const health = await call<{ status?: string; plugin?: string; version?: string }>(
        port,
        tenant,
        '/health'
      );
      return JSON.stringify({ section, health }, null, 2);
    } catch (error) {
      if (error instanceof ApiError) {
        return JSON.stringify({ section, error: error.code, detail: error.message }, null, 2);
      }
      return JSON.stringify({ section, error: 'unexpected', detail: String(error) }, null, 2);
    }
  }
};
