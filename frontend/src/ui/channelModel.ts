export type SocialChannel = 'pinterest' | 'instagram' | 'facebook' | 'linkedin' | 'tiktok' | 'youtube'
export type SocialChannelFilter = 'all' | SocialChannel
export type GenerationChannel = 'pinterest' | 'instagram' | 'facebook' | 'linkedin' | 'tiktok' | 'youtube_shorts'

export const SOCIAL_CHANNELS: ReadonlyArray<{ key: SocialChannel; label: string }> = [
  { key: 'pinterest', label: 'Pinterest' },
  { key: 'instagram', label: 'Instagram' },
  { key: 'facebook', label: 'Facebook' },
  { key: 'linkedin', label: 'LinkedIn' },
  { key: 'tiktok', label: 'TikTok' },
  { key: 'youtube', label: 'YouTube' },
]

export function normalizeSocialChannel(value?: string | null): SocialChannel {
  if (value === 'youtube_shorts' || value === 'youtube') return 'youtube'
  if (SOCIAL_CHANNELS.some((channel) => channel.key === value)) return value as SocialChannel
  return 'pinterest'
}

export function generationChannel(channel: SocialChannel): GenerationChannel {
  return channel === 'youtube' ? 'youtube_shorts' : channel
}

export function channelLabel(channel: SocialChannel | string | null | undefined): string {
  const normalized = normalizeSocialChannel(channel)
  return SOCIAL_CHANNELS.find((item) => item.key === normalized)?.label || 'Pinterest'
}

type RevisionLike = {
  active?: boolean
  intended_channel?: string | null
  kind?: string
}

export function contentChannel(versions?: RevisionLike[] | null): SocialChannel {
  const active = versions?.find((version) => version.active)
  const intended = active?.intended_channel || versions?.find((version) => version.intended_channel)?.intended_channel
  return normalizeSocialChannel(intended)
}

export function matchesChannel(versions: RevisionLike[] | null | undefined, filter: SocialChannelFilter): boolean {
  return filter === 'all' || contentChannel(versions) === filter
}
