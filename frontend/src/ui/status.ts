export type StatusTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'

export type StatusPresentation = {
  label: string
  tone: StatusTone
}

const STATUS_PRESENTATIONS: Record<string, StatusPresentation> = {
  DRAFT: { label: 'Draft', tone: 'neutral' },
  GENERATED: { label: 'Draft', tone: 'neutral' },
  READY_FOR_REVIEW: { label: 'Needs review', tone: 'warning' },
  REVIEW: { label: 'Needs review', tone: 'warning' },
  APPROVED: { label: 'Approved', tone: 'success' },
  SCHEDULED: { label: 'Scheduled', tone: 'info' },
  PUBLISHING: { label: 'Publishing', tone: 'info' },
  PUBLISHED: { label: 'Published', tone: 'success' },
  PUBLISH_UNKNOWN: { label: 'Needs verification', tone: 'warning' },
  PUBLISH_FAILED: { label: 'Failed', tone: 'danger' },
  FAILED: { label: 'Failed', tone: 'danger' },
  CANCELLED: { label: 'Cancelled', tone: 'neutral' },
  CONNECTED: { label: 'Connected', tone: 'success' },
  DISCONNECTED: { label: 'Disconnected', tone: 'neutral' },
  UNAVAILABLE: { label: 'Unavailable', tone: 'danger' },
  CHECKING: { label: 'Checking', tone: 'info' },
  READY: { label: 'Ready', tone: 'success' },
  BLOCKED: { label: 'Needs attention', tone: 'warning' },
  UNKNOWN: { label: 'Needs verification', tone: 'warning' },
}

function titleCase(value: string): string {
  return value
    .toLowerCase()
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
}

export function presentStatus(status: string | null | undefined): StatusPresentation {
  if (!status || !status.trim()) return { label: 'Unavailable', tone: 'neutral' }
  const normalized = status.trim().toUpperCase()
  return STATUS_PRESENTATIONS[normalized] ?? { label: titleCase(normalized), tone: 'neutral' }
}
