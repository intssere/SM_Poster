import { useEffect, useState } from 'react'

type Attempt = {
  attempt_number: number
  status: string
  started_at?: string | null
  completed_at?: string | null
  provider_pin_id?: string | null
  error_code?: string | null
  safe_response_metadata?: Record<string, unknown>
}

type Publication = {
  id: string
  status: string
  revision_id?: string | null
  creative_id?: string | null
  approval_id?: string | null
  pinterest_connection_id?: string | null
  pinterest_board_record_id?: string | null
  pinterest_board_id?: string | null
  scheduled_for?: string | null
  published_at?: string | null
  pinterest_pin_id?: string | null
  error_code?: string | null
  scheduler_foundation_available?: boolean
  live_publishing_enabled?: boolean
  publishing_readiness_reason?: string | null
  attempts?: Attempt[]
}

const scheduleableStatuses = new Set(['APPROVED', 'SCHEDULED', 'PUBLISH_FAILED'])
const cancellableStatuses = new Set(['APPROVED', 'SCHEDULED', 'PUBLISH_FAILED', 'PUBLISH_UNKNOWN'])

async function readApiError(response: Response): Promise<string> {
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string') return body.detail
  } catch {
    // Keep the fallback bounded; never dump arbitrary HTML or raw bodies.
  }
  return `Request failed (${response.status})`
}

function toScheduledIso(value: string): string | null {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return null
  return parsed.toISOString()
}

function scheduleLabel(status: string): string {
  return status === 'APPROVED' ? 'Schedule' : 'Reschedule'
}

export function PublicationsPage() {
  const [rows, setRows] = useState<Publication[]>([])
  const [error, setError] = useState<string | null>(null)
  const [scheduleValues, setScheduleValues] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = async () => {
    try {
      const response = await fetch('/api/publications', { credentials: 'include' })
      if (!response.ok) throw new Error(await readApiError(response))
      setRows(await response.json())
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Request failed')
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const mutate = async (id: string, action: 'schedule' | 'cancel', body?: object) => {
    setError(null)
    setBusyId(id)
    try {
      const response = await fetch(`/api/publications/${id}/${action}`, {
        method: 'POST',
        credentials: 'include',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      if (!response.ok) throw new Error(await readApiError(response))
      await load()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Request failed')
    } finally {
      setBusyId(null)
    }
  }

  const schedulePublication = async (row: Publication) => {
    const value = scheduleValues[row.id] || ''
    const scheduledFor = toScheduledIso(value)
    if (!scheduledFor) {
      setError('Choose a valid scheduled time.')
      return
    }
    await mutate(row.id, 'schedule', { scheduled_for: scheduledFor })
  }

  const schedulerKnown = rows.length === 0 || rows.some((row) => row.scheduler_foundation_available)
  const publishingEnabled = rows.some((row) => row.live_publishing_enabled)

  return (
    <section>
      <header>
        <div>
          <p className="eyebrow">PUBLICATIONS / SCHEDULER</p>
          <h2>Publication queue</h2>
          <p>
            Scheduler foundation: {schedulerKnown ? 'available' : 'ready'} · Live publishing:{' '}
            {publishingEnabled ? 'enabled' : 'disabled'}
          </p>
        </div>
      </header>

      {error && (
        <section className="panel">
          <strong>{error}</strong>
        </section>
      )}

      {rows.length === 0 && (
        <section className="panel">
          <p>No publications are queued yet.</p>
        </section>
      )}

      {rows.map((row) => {
        const canSchedule = scheduleableStatuses.has(row.status)
        const canCancel = cancellableStatuses.has(row.status)
        const value = scheduleValues[row.id] || ''
        return (
          <article className="panel" key={row.id}>
            <h3>{row.id}</h3>
            <p>
              Status: {row.status} · Readiness: {row.publishing_readiness_reason || '—'}
            </p>
            <p>
              Revision: {row.revision_id || 'original'} · Creative: {row.creative_id || '—'} ·
              Approval: {row.approval_id || '—'}
            </p>
            <p>
              Connection: {row.pinterest_connection_id || '—'} · Board record:{' '}
              {row.pinterest_board_record_id || '—'} · Board: {row.pinterest_board_id || '—'}
            </p>
            <p>
              Scheduled: {row.scheduled_for || '—'} · Published: {row.published_at || '—'} · Pin:{' '}
              {row.pinterest_pin_id || '—'}
            </p>
            <p>
              Scheduler foundation: {row.scheduler_foundation_available ? 'available' : 'unavailable'} ·
              Live publishing: {row.live_publishing_enabled ? 'enabled' : 'disabled'}
            </p>
            {row.error_code && <p>Safe error: {row.error_code}</p>}

            {canSchedule && (
              <>
                <input
                  type="datetime-local"
                  value={value}
                  onChange={(event) =>
                    setScheduleValues((current) => ({
                      ...current,
                      [row.id]: event.target.value,
                    }))
                  }
                  aria-label={`Scheduled time for ${row.id}`}
                />
                <button disabled={!value || busyId === row.id} onClick={() => void schedulePublication(row)}>
                  {scheduleLabel(row.status)}
                </button>
              </>
            )}

            {canCancel && (
              <button disabled={busyId === row.id} onClick={() => void mutate(row.id, 'cancel')}>
                Cancel
              </button>
            )}

            {row.attempts?.map((attempt) => (
              <p key={attempt.attempt_number}>
                Attempt #{attempt.attempt_number}: {attempt.status} · started {attempt.started_at || '—'} ·
                completed {attempt.completed_at || '—'} · {attempt.provider_pin_id || '—'} ·{' '}
                {attempt.error_code || '—'}{' '}
                {attempt.safe_response_metadata && JSON.stringify(attempt.safe_response_metadata)}
              </p>
            ))}
          </article>
        )
      })}
    </section>
  )
}
