import type { ApiErrorBody } from "@/lib/contracts"

export class ApiError extends Error {
  status: number
  body: ApiErrorBody | string | null

  constructor(status: number, message: string, body: ApiErrorBody | string | null) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

/** The most specific message a JSON error body carries, if any. */
function structuredErrorMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null
  const value = body as { output?: unknown; error?: unknown; errors?: unknown }
  const first = Array.isArray(value.errors) ? value.errors[0] : null
  const detail = typeof first === "string"
    ? first
    : first && typeof first === "object" && typeof (first as { message?: unknown }).message === "string"
      ? (first as { message: string }).message
      : typeof value.error === "string" ? value.error : null
  const output = typeof value.output === "string" ? value.output : null
  return output && detail ? `${output} — ${detail}` : output ?? detail
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  const response = await fetch(path, { ...init, headers })
  const contentType = response.headers.get("content-type") ?? ""
  const body = contentType.includes("application/json")
    ? await response.json() as T | ApiErrorBody
    : await response.text()
  if (!response.ok) {
    const message = typeof body === "string"
      ? body
      : structuredErrorMessage(body) ?? `Request failed (${response.status})`
    throw new ApiError(response.status, message, body as ApiErrorBody | string)
  }
  return body as T
}

export function query(path: string, values: Record<string, string | number | boolean | null | undefined>) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value))
  }
  const encoded = params.toString()
  return encoded ? `${path}?${encoded}` : path
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unexpected error"
}
