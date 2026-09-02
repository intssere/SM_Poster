import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, Clock3, Image, Radio, ShieldAlert } from 'lucide-react'
import { ChannelCapabilities, ChannelDescriptor, getChannelCapabilities, getPinterestBoards, PinterestBoard, syncPinterestBoards, updatePinterestBoard } from '../api/channels'

function readableStatus(channel: ChannelDescriptor) {
  return channel.status === 'INTERNAL_PREVIEW' ? 'Internal preview' : 'Future / not connected'
}

function readableMediaKind(kind: string) {
  return kind.replaceAll('_', ' ')
}

function ChannelCard({ channel, pinterest, onConnect, onDisconnect }: { channel: ChannelDescriptor; pinterest?: any; onConnect?: () => void; onDisconnect?: () => void }) {
  const internal = channel.status === 'INTERNAL_PREVIEW'
  return <article className={`channel-card ${internal ? 'channel-card-internal' : 'channel-card-future'}`}>
    <header className="channel-card-heading">
      <div className="channel-card-title"><span className="channel-icon"><Radio size={18} /></span><div><h3>{channel.label}</h3><p>{internal ? 'Existing workspace channel' : 'Planned channel adapter'}</p></div></div>
      <span className={`channel-status ${internal ? 'internal' : 'future'}`}>{internal ? <CheckCircle2 size={13} /> : <Clock3 size={13} />}{readableStatus(channel)}</span>
    </header>
    <p className="channel-summary">{channel.capability_summary}</p>
    <div className="channel-account"><span>Account status</span><strong>{pinterest ? (pinterest.connected ? 'Connected' : 'Not connected') : (channel.account.status === 'INTERNAL' ? 'Internal only' : 'Not connected')}</strong></div>
    {pinterest?.connected ? <div><p>{pinterest.account?.username || 'Pinterest account'} · {pinterest.account?.id}</p><p>Scopes: {(pinterest.account?.granted_scopes || []).join(', ')}</p><p>Access expires: {pinterest.account?.access_token_expires_at || 'Unknown'}<br/>Refresh expires: {pinterest.account?.refresh_token_expires_at || 'Unknown'}</p><button className="secondary-action" onClick={onDisconnect}>Disconnect</button></div> : pinterest && <button className="secondary-action" onClick={onConnect}>Connect Pinterest</button>}
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
  const [boards, setBoards] = useState<PinterestBoard[] | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [boardError, setBoardError] = useState<string | null>(null)
  const [lastSyncedAt, setLastSyncedAt] = useState<string | null>(null)
  const [pinterest, setPinterest] = useState<any>(null)

  const load = useCallback(async () => {
    try {
      setError(null)
      setCapabilities(await getChannelCapabilities())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not load channel capabilities.')
    }
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => { getPinterestBoards().then((result) => { setBoards(result.boards); setLastSyncedAt(result.last_synced_at || null) }).catch(() => setBoardError('Could not load boards.')) }, [])
  async function syncBoards() { setSyncing(true); setBoardError(null); try { const result = await syncPinterestBoards(); setBoards(result.boards); setLastSyncedAt(result.last_synced_at || new Date().toISOString()) } catch { setBoardError('Board sync failed.') } finally { setSyncing(false) } }
  async function updateBoard(board: PinterestBoard, body: { is_eligible?: boolean; routing_label?: string | null }) { try { const updated = await updatePinterestBoard(board.id, body); setBoards((items) => items?.map((item) => item.id === updated.id ? { ...item, ...updated } : item) || null) } catch { setBoardError('Could not update local configuration.') } }
  useEffect(() => { fetch('/api/channels/pinterest/status', { credentials: 'include' }).then(r => r.json()).then(setPinterest).catch(() => null); const params = new URLSearchParams(window.location.search); const result = params.get('result'); if (result) { setError(result === 'connected' ? null : `Pinterest connection: ${result}`); window.history.replaceState({}, '', `${window.location.pathname}#channels`) } }, [])
  async function connectPinterest() {
    const response = await fetch('/api/channels/pinterest/oauth/start', { method: 'POST', credentials: 'include' })
    if (!response.ok) { setError('Pinterest connection is unavailable.'); return }
    const payload = await response.json(); window.location.assign(payload.authorization_url)
  }
  async function disconnectPinterest() {
    const response = await fetch('/api/channels/pinterest/disconnect', { method: 'POST', credentials: 'include' })
    if (response.ok) setPinterest({ status: 'NOT_CONNECTED', connected: false })
  }

  return <div className="channels-page">
    <header className="page-heading">
      <div><p className="eyebrow">SOCIAL STUDIO / CHANNELS</p><h2>Channels</h2><p>One content workspace, with channel-specific adapters ready to grow independently.</p></div>
      <div className="channels-header-status"><span className="proposal-safety"><ShieldAlert size={15} /> Publishing disabled</span><span className="channels-readonly">Read-only capability view</span></div>
    </header>
    <section className="channel-intro"><div><p className="eyebrow">ADAPTER STATUS</p><h3>Build once. Adapt by channel.</h3><p>Channel requirements are modeled separately from the existing Pinterest proposal and creative records. No future platform is connected or contacted.</p></div><div className="channel-legend"><span><i className="legend-dot internal" /> Pinterest internal preview</span><span><i className="legend-dot future" /> Future / not connected</span></div></section>
    {error ? <div className="channel-error" role="alert"><ShieldAlert size={20} /><div><strong>Channel status unavailable</strong><p>{error}</p><button className="secondary-action" onClick={() => void load()}>Try again</button></div></div>
      : !capabilities ? <div className="channel-loading"><span /><span /><span /></div>
        : <><section className="channel-grid">{capabilities.channels.map((channel) => <ChannelCard channel={channel} pinterest={channel.key === 'pinterest' ? pinterest : undefined} onConnect={() => void connectPinterest()} onDisconnect={() => void disconnectPinterest()} key={channel.key} />)}</section><section className="panel board-manager"><div className="page-heading"><div><p className="eyebrow">PINTEREST / BOARD MANAGER</p><h3>Connected boards</h3><p>Provider metadata is read-only; eligibility and routing are local configuration.</p>{lastSyncedAt && <small>Last synchronized: {new Date(lastSyncedAt).toLocaleString()}</small>}</div><button className="secondary-action" onClick={() => void syncBoards()} disabled={syncing}>{syncing ? 'Syncing…' : 'Sync boards'}</button></div>{boardError && <p role="alert">{boardError}</p>}{boards?.map((board) => <article className="board-row" key={board.id}><div><strong>{board.name}</strong><small>Provider ID: {board.external_board_id} · {board.privacy || 'privacy unavailable'} · {board.owner_username || 'owner unavailable'}</small><small>{board.pin_count ?? '—'} pins · {board.follower_count ?? '—'} followers · {board.collaborator_count ?? '—'} collaborators · {board.is_active ? 'Active' : 'Inactive'}</small>{board.sections?.length ? <small>Sections: {board.sections.map((section) => section.name).join(', ')}</small> : null}</div><div className="board-local-config"><label><input type="checkbox" checked={board.is_eligible} onChange={(event) => void updateBoard(board, { is_eligible: event.target.checked })} /> Eligible for routing</label><label>Routing label<input value={board.routing_label || ''} maxLength={120} placeholder="Optional local label" onChange={(event) => void updateBoard(board, { routing_label: event.target.value || null })} /></label></div></article>)}</section></>}
  </div>
}
