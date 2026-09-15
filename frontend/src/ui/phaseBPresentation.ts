export type BackendStatus = 'checking' | 'connected' | 'unavailable'

export type HomeAttentionInput = {
  backendStatus: BackendStatus
  publishingEnabled: boolean | null
  reviewCount: number
  qaWarnings: number
  shopifyConnected: boolean | null
}

export type AttentionItem = {
  key: 'backend' | 'publishing' | 'review' | 'catalog' | 'shopify'
  tone: 'danger' | 'warning' | 'info'
  title: string
  detail: string
}

export function formatOperationalCount(value: number | null | undefined): string {
  return typeof value === 'number' ? value.toLocaleString() : '—'
}

export function buildHomeAttention(input: HomeAttentionInput): AttentionItem[] {
  const items: AttentionItem[] = []

  if (input.backendStatus === 'unavailable') {
    items.push({ key: 'backend', tone: 'danger', title: 'System health needs attention', detail: 'Live operational data is currently unavailable.' })
  }
  if (input.publishingEnabled === false) {
    items.push({ key: 'publishing', tone: 'warning', title: 'Publishing is paused', detail: 'Review and scheduling remain available, but dispatch is gated.' })
  }
  if (input.reviewCount > 0) {
    items.push({ key: 'review', tone: 'warning', title: `${input.reviewCount.toLocaleString()} item${input.reviewCount === 1 ? '' : 's'} need review`, detail: 'Approve, reject, or revise content before it moves forward.' })
  }
  if (input.qaWarnings > 0) {
    items.push({ key: 'catalog', tone: 'warning', title: `${input.qaWarnings.toLocaleString()} catalog QA warning${input.qaWarnings === 1 ? '' : 's'}`, detail: 'Review product normalization warnings before generating content.' })
  }
  if (input.shopifyConnected === false) {
    items.push({ key: 'shopify', tone: 'warning', title: 'Shopify needs attention', detail: 'Reconnect Shopify to keep catalog data current.' })
  }

  return items
}

export function connectionStatus(connected: boolean | null | undefined): { status: string; label: string } {
  if (connected === true) return { status: 'CONNECTED', label: 'Connected' }
  if (connected === false) return { status: 'BLOCKED', label: 'Needs attention' }
  return { status: 'CHECKING', label: 'Checking' }
}
