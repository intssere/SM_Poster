export const BUFFER_ACTIVATION_VERSION = 'BUFFER_PILOT_ACTIVATION_V1'

export function publicationRequest(approvalId: string, boardRecordId: string, scheduledFor?: string) {
  return {
    url: '/api/publications',
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approval_id: approvalId, pinterest_board_record_id: boardRecordId, ...(scheduledFor ? { scheduled_for: scheduledFor } : {}) }),
  }
}

export function activationRequest(id: string) {
  return {
    url: `/api/publications/${encodeURIComponent(id)}/buffer-pilot-activation`,
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmed: true, confirmation_text_version: BUFFER_ACTIVATION_VERSION }),
  }
}

export function revokeActivationRequest(id: string) {
  return { url: `/api/publications/${encodeURIComponent(id)}/buffer-pilot-activation/revoke`, method: 'POST', credentials: 'include' }
}

export function isCurrentResponse<T>(requestToken: number, currentToken: number, value: T): T | undefined {
  return requestToken === currentToken ? value : undefined
}

export function canCreatePublication(approvalId?: string | null, boardRecordId?: string | null): boolean {
  return Boolean(approvalId && boardRecordId)
}

export async function submitSelectedPublication(
  approvalId: string | null | undefined,
  destinationChoice: Record<string, string>,
  submit: (approvalId: string, boardRecordId: string) => Promise<unknown>,
): Promise<boolean> {
  const boardRecordId = approvalId ? destinationChoice[approvalId] : undefined
  if (!canCreatePublication(approvalId, boardRecordId)) return false
  await submit(approvalId as string, boardRecordId as string)
  return true
}

export function canArmActivation(status?: string, loading = false): boolean {
  return !loading && (status === 'DISARMED' || status === 'REVOKED')
}

export function canRevokeActivation(status?: string, loading = false): boolean {
  return !loading && status === 'ARMED'
}

export function canDispatchBuffer(provider: string, authorizationMatches: boolean, readinessMatches: boolean, activationStatus?: string, activationLoading = false): boolean {
  return provider !== 'buffer' || (!activationLoading && activationStatus === 'ARMED' && authorizationMatches && readinessMatches)
}
