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

export type PinterestBoardSection = { id: string; external_section_id: string; name: string; is_active: boolean }
export type PinterestBoard = { id: string; external_board_id: string; name: string; description?: string | null; privacy?: string | null; owner_username?: string | null; pin_count?: number | null; follower_count?: number | null; collaborator_count?: number | null; is_ads_only?: boolean; image_cover_url?: string | null; is_active: boolean; is_eligible: boolean; routing_label?: string | null; last_seen_at?: string | null; last_synced_at?: string | null; sections?: PinterestBoardSection[] }
export type PinterestBoardsResponse = { connection_status: string; last_synced_at?: string | null; boards: PinterestBoard[] }
export async function getPinterestBoards(): Promise<PinterestBoardsResponse> { return json(await fetch('/api/channels/pinterest/boards')) }
export async function syncPinterestBoards(): Promise<PinterestBoardsResponse> { return json(await fetch('/api/channels/pinterest/boards/sync', { method: 'POST' })) }
export async function updatePinterestBoard(id: string, body: { is_eligible?: boolean; routing_label?: string | null }): Promise<PinterestBoard> { return json(await fetch(`/api/channels/pinterest/boards/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })) }
