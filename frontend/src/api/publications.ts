export type EligibleDestination = {
  board_record_id: string
  display_name: string
  routing_label: string
  eligibility?: string
  sync_status?: string
  recommended?: boolean
}

export type BufferPilotActivation = {
  status: 'ARMED' | 'DISARMED' | 'REVOKED' | 'UNKNOWN'
  confirmation_text_version?: string
  activated_at?: string | null
  revoked_at?: string | null
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || body?.message || `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export async function getEligibleDestinations(approvalId: string): Promise<EligibleDestination[]> {
  const response = await fetch(`/api/publications/eligible-destinations?approval_id=${encodeURIComponent(approvalId)}`, { credentials: 'include' })
  return json(response)
}

export async function createPublication(approvalId: string, boardRecordId: string, scheduledFor?: string) {
  const body = { approval_id: approvalId, pinterest_board_record_id: boardRecordId, ...(scheduledFor ? { scheduled_for: scheduledFor } : {}) }
  const response = await fetch('/api/publications', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  return json<{ id: string }>(response)
}

export async function getBufferPilotActivation(publicationId: string): Promise<BufferPilotActivation> {
  const response = await fetch(`/api/publications/${encodeURIComponent(publicationId)}/buffer-pilot-activation`, { credentials: 'include' })
  return json(response)
}

export async function armBufferPilot(publicationId: string): Promise<BufferPilotActivation> {
  const response = await fetch(`/api/publications/${encodeURIComponent(publicationId)}/buffer-pilot-activation`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirmed: true, confirmation_text_version: 'BUFFER_PILOT_ACTIVATION_V1' }) })
  return json(response)
}

export async function revokeBufferPilot(publicationId: string): Promise<BufferPilotActivation> {
  const response = await fetch(`/api/publications/${encodeURIComponent(publicationId)}/buffer-pilot-activation/revoke`, { method: 'POST', credentials: 'include' })
  return json(response)
}