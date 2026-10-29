const API_ROOT = import.meta.env.VITE_API_URL ?? ''

function backendErrorMessage(payload: unknown, status: number, statusText: string): string {
  if (payload && typeof payload === 'object') {
    const value = payload as { detail?: unknown; error?: unknown; stage?: unknown }
    const withStage = (message: string, stage?: unknown) => typeof stage === 'string' && stage ? `${message} [${stage}]` : message
    if (typeof value.detail === 'string' && value.detail.trim()) return withStage(value.detail, value.stage)
    if (value.detail && typeof value.detail === 'object' && 'message' in value.detail) {
      const detail = value.detail as { message?: unknown; stage?: unknown }
      if (typeof detail.message === 'string' && detail.message.trim()) return withStage(detail.message, detail.stage)
    }
    if (value.error && typeof value.error === 'object' && 'message' in value.error) {
      const error = value.error as { message?: unknown; stage?: unknown }
      if (typeof error.message === 'string' && error.message.trim()) return withStage(error.message, error.stage)
    }
  }
  return statusText || `Request failed (${status})`
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}/api${path}`, {
    ...init,
    headers: init?.body instanceof FormData
      ? init.headers
      : { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(backendErrorMessage(payload, response.status, response.statusText))
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const post = <T>(path: string, body?: unknown) => api<T>(path, {
  method: 'POST',
  body: body === undefined ? undefined : JSON.stringify(body),
})

export const put = <T>(path: string, body: unknown) => api<T>(path, {
  method: 'PUT',
  body: JSON.stringify(body),
})

export const del = <T>(path: string) => api<T>(path, { method: 'DELETE' })
