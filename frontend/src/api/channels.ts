export type ChannelStatus = 'INTERNAL_PREVIEW' | 'NOT_CONNECTED'
export type AccountStatus = 'INTERNAL' | 'NOT_CONNECTED'

export type ChannelMediaRequirement = {
  media_kind: string
  accepted_formats: string[]
  aspect_ratios: string[]
  min_width: number | null
  min_height: number | null
  max_duration_seconds: number | null
  catalog_source_required: boolean
  notes: string
}

export type PlatformContentVariant = {
  key: string
  label: string
  required_text_fields: string[]
  media_requirements: ChannelMediaRequirement[]
  destination_required: boolean
}

export type ChannelDescriptor = {
  key: string
  label: string
  status: ChannelStatus
  future: boolean
  adapter_key: string | null
  account: {
    channel_key: string
    status: AccountStatus
    mode: string
    external_account_id: string | null
  }
  capabilities: {
    content_preview: boolean
    account_connection: boolean
    publishing: boolean
    scheduling: boolean
    analytics: boolean
  }
  variants: PlatformContentVariant[]
  capability_summary: string
}

export type ChannelCapabilities = {
  publishing_enabled: boolean
  channels: ChannelDescriptor[]
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.detail || payload?.message || `Request failed (${response.status})`)
  }
  return response.json()
}

export async function getChannelCapabilities(): Promise<ChannelCapabilities> {
  return json(await fetch('/api/channels/capabilities'))
}