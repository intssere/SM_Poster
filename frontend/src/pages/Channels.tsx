import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, Clock3, Image, Radio, ShieldAlert } from 'lucide-react'
import { ChannelCapabilities, ChannelDescriptor, getChannelCapabilities } from '../api/channels'

function readableStatus(channel: ChannelDescriptor) {
  return channel.status === 'INTERNAL_PREVIEW' ? 'Internal preview' : 'Future / not connected'
}

function readableMediaKind(kind: string) {
  return kind.replaceAll('_', ' ')
}

function ChannelCard({ channel }: { channel: ChannelDescriptor }) {
  const internal = channel.status === 'INTERNAL_PREVIEW'
  return <article className={`channel-card ${internal ? 'channel-card-internal' : 'channel-card-future'}`}>
    <header className="channel-card-heading">
      <div className="channel-card-title"><span className="channel-icon"><Radio size={18} /></span><div><h3>{channel.label}</h3><p>{internal ? 'Existing workspace channel' : 'Planned channel adapter'}</p></div></div>
      <span className={`channel-status ${internal ? 'internal' : 'future'}`}>{internal ? <CheckCircle2 size={13} /> : <Clock3 size={13} />}{readableStatus(channel)}</span>
    </header>
    <p className="channel-summary">{channel.capability_summary}</p>
    <div className="channel-account"><span>Account status</span><strong>{channel.account.status === 'INTERNAL' ? 'Internal only' : 'Not connected'}</strong></div>
    <div className="channel-capabilities" aria-label={`${channel.label} capabilities`}>
      <span className={channel.capabilities.content_preview ? 'enabled' : ''}>Preview</span>
      <span className={channel.capabilities.account_connection ? 'enabled' : ''}>Connect</span>
      <span className={channel.capabilities.publishing ? 'enabled' : ''}>Publish</span>
      <span className={channel.capabilities.scheduling ? 'enabled' : ''}>Schedule</span>
      <span className={channel.capabilities.analytics ? 'enabled' : ''}>Analytics</span>
    </div>
    <div className="channel-variants"><p className="eyebrow">CONTENT VARIANTS</p>{channel.variants.map((variant) => <div className="channel-variant" key={variant.key}>
      <div><strong>{variant.label}</strong><span>{variant.required_text_fields.length ? `Text: ${variant.required_text_fields.join(', ')}` : 'Media-only variant'}</span></div>
      {variant.media_requirements.map((media) => <div className="channel-media" key={`${variant.key}-${media.media_kind}`}><Image size={14} /><span>{readableMediaKind(media.media_kind)} · {media.aspect_ratios.join(' / ')}</span><small>{media.min_width && media.min_height ? `${media.min_width}×${media.min_height}px minimum` : 'Platform dimensions vary'}</small></div>)}
    </div>)}</div>
    {!internal && <div className="channel-future-note"><ShieldAlert size={14} /> No account setup or external API connection is available yet.</div>}
  </article>
}

export function ChannelsPage() {
  const [capabilities, setCapabilities] = useState<ChannelCapabilities | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setError(null)
      setCapabilities(await getChannelCapabilities())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not load channel capabilities.')
    }
  }, [])

  useEffect(() => { void load() }, [load])

  return <div className="channels-page">
    <header className="page-heading">
      <div><p className="eyebrow">SOCIAL STUDIO / CHANNELS</p><h2>Channels</h2><p>One content workspace, with channel-specific adapters ready to grow independently.</p></div>
      <div className="channels-header-status"><span className="proposal-safety"><ShieldAlert size={15} /> Publishing disabled</span><span className="channels-readonly">Read-only capability view</span></div>
    </header>
    <section className="channel-intro"><div><p className="eyebrow">ADAPTER STATUS</p><h3>Build once. Adapt by channel.</h3><p>Channel requirements are modeled separately from the existing Pinterest proposal and creative records. No future platform is connected or contacted.</p></div><div className="channel-legend"><span><i className="legend-dot internal" /> Pinterest internal preview</span><span><i className="legend-dot future" /> Future / not connected</span></div></section>
    {error ? <div className="channel-error" role="alert"><ShieldAlert size={20} /><div><strong>Channel status unavailable</strong><p>{error}</p><button className="secondary-action" onClick={() => void load()}>Try again</button></div></div>
      : !capabilities ? <div className="channel-loading"><span /><span /><span /></div>
        : <section className="channel-grid">{capabilities.channels.map((channel) => <ChannelCard channel={channel} key={channel.key} />)}</section>}
  </div>
}