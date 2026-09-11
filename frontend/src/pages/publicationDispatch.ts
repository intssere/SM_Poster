export type DispatchProvider = 'direct' | 'buffer'

export function authorizationPath(id: string, provider: DispatchProvider): string {
  return `/api/publications/${id}/${provider === 'buffer' ? 'buffer-dispatch-authorization' : 'dispatch-authorization'}`
}

export function publishPath(id: string, provider: DispatchProvider): string {
  return `/api/publications/${id}/${provider === 'buffer' ? 'publish-buffer' : 'publish'}`
}

export function readinessPath(id: string, provider: DispatchProvider): string {
  const dispatchProvider = provider === 'buffer' ? 'buffer' : 'pinterest_direct'
  return `/api/publications/${id}/dispatch-readiness?dispatch_provider=${dispatchProvider}`
}

/** Publish endpoints intentionally receive no body: server-side evidence is authoritative. */
export function publishRequest(id: string, provider: DispatchProvider): RequestInit & { url: string } {
  return { url: publishPath(id, provider), method: 'POST', credentials: 'include' }
}

export function authorizationPayload(confirmationTextVersion: string) {
  return { confirmed: true, confirmation_text_version: confirmationTextVersion }
}

export function readinessKey(id: string, provider: DispatchProvider): string {
  return `${id}:${provider}`
}

export async function performOneShotDispatch<T>(
  send: () => Promise<T>,
  refresh: () => Promise<void>,
): Promise<{ response?: T; sendError?: unknown; refreshError?: unknown }> {
  let response: T | undefined
  let sendError: unknown
  let refreshError: unknown
  try {
    response = await send()
  } catch (error) {
    sendError = error
  }
  try {
    await refresh()
  } catch (error) {
    refreshError = error
  }
  return { response, sendError, refreshError }
}