import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, PackageSearch, PlugZap, Radio, RefreshCw, ShieldCheck } from 'lucide-react'
import {
  ChannelCapabilities,
  ChannelDescriptor,
  getChannelCapabilities,
  getPinterestBoards,
  PinterestBoard,
  syncPinterestBoards,
  updatePinterestBoard,
} from '../api/channels'
import { getShopifyStatus, ShopifyStatus } from '../api/catalog'
import { Alert, Button, EmptyState, PageHeader, StatusBadge, Surface } from '../ui/primitives'
import { connectionStatus } from '../ui/phaseBPresentation'
import { SOCIAL_CHANNELS } from '../ui/channelModel'

function CapabilityFlag({ enabled, children }: { enabled: boolean; children: string }) {
  return <span className={enabled ? 'ds-capability-flag is-ready' : 'ds-capability-flag'}>{children}</span>
}

function SocialChannelCard({
  channel,
  pinterest,
  onConnectPinterest,
  onDisconnectPinterest,
}: {
  channel: ChannelDescriptor
  pinterest: any
  onConnectPinterest: () => void
  onDisconnectPinterest: () => void
}) {
  const isPinterest = channel.key === 'pinterest'
  const connected = isPinterest ? Boolean(pinterest?.connected) : false
  const primaryStatus = isPinterest
    ? connected
      ? { status: 'CONNECTED' as const, label: 'Connected' }
      : { status: 'BLOCKED' as const, label: 'Connection available' }
    : { status: 'READY' as const, label: 'Content ready' }

  return <Surface className="ds-social-channel-card">
    <div className="ds-social-channel-card__header">
      <div className="ds-connection-card__identity">
        <span className="ds-connection-card__icon"><Radio size={20} /></span>
        <div><h2>{channel.label}</h2><p>{channel.capability_summary}</p></div>
      </div>
      <StatusBadge status={primaryStatus.status} label={primaryStatus.label} />
    </div>

    <div className="ds-channel-capabilities" aria-label={`${channel.label} capabilities`}>
      <CapabilityFlag enabled={channel.capabilities.content_generation}>Generate</CapabilityFlag>
      <CapabilityFlag enabled={channel.capabilities.review}>Review</CapabilityFlag>
      <CapabilityFlag enabled={channel.capabilities.account_connection}>Connect</CapabilityFlag>
      <CapabilityFlag enabled={channel.capabilities.scheduling}>Schedule</CapabilityFlag>
      <CapabilityFlag enabled={channel.capabilities.publishing}>Publish</CapabilityFlag>
      <CapabilityFlag enabled={channel.capabilities.analytics}>Analytics</CapabilityFlag>
    </div>

    {isPinterest ? <>
      <div className="ds-connection-card__facts">
        <div className="ds-connection-card__fact"><span>Account</span><strong>{pinterest?.account?.username || (connected ? 'Pinterest account' : 'Not connected')}</strong></div>
        <div className="ds-connection-card__fact"><span>Distribution</span><strong>{channel.capabilities.publishing ? 'Publishing gate on' : 'Publishing gate paused'}</strong></div>
      </div>
      <div className="ds-connection-card__actions">
        {connected
          ? <Button variant="ghost" onClick={onDisconnectPinterest}>Disconnect Pinterest</Button>
          : <Button variant="primary" onClick={onConnectPinterest}><PlugZap size={15} />Connect Pinterest</Button>}
      </div>
      {connected ? <details className="ds-technical-details"><summary>Technical details</summary><p>Granted scopes: {(pinterest.account?.granted_scopes || []).join(', ') || '—'}</p><p>Access expires: {pinterest.account?.access_token_expires_at || 'Unknown'} · Refresh expires: {pinterest.account?.refresh_token_expires_at || 'Unknown'}</p></details> : null}
    </> : <div className="ds-channel-boundary">
      <ShieldCheck size={16} />
      <span><strong>No external account is connected.</strong> Content can be generated and reviewed for {channel.label}; OAuth, scheduling, publishing, and analytics require a separate future adapter.</span>
    </div>}
  </Surface>
}

export function ChannelsPage() {
  const [capabilities, setCapabilities] = useState<ChannelCapabilities | null>(null)
  const [shopify, setShopify] = useState<ShopifyStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [boards, setBoards] = useState<PinterestBoard[] | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [boardError, setBoardError] = useState<string | null>(null)
  const [lastSyncedAt, setLastSyncedAt] = useState<string | null>(null)
  const [pinterest, setPinterest] = useState<any>(null)

  const load = useCallback(async () => {
    try {
      setError(null)
      const [channelCapabilities, shopifyStatus] = await Promise.all([getChannelCapabilities(), getShopifyStatus()])
      setCapabilities(channelCapabilities)
      setShopify(shopifyStatus)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not load connection status.')
    }
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    getPinterestBoards().then((result) => {
      setBoards(result.boards)
      setLastSyncedAt(result.last_synced_at || null)
    }).catch(() => setBoardError('Could not load boards.'))
  }, [])

  useEffect(() => {
    fetch('/api/channels/pinterest/status', { credentials: 'include' }).then((response) => response.json()).then(setPinterest).catch(() => null)
    const params = new URLSearchParams(window.location.search)
    const result = params.get('result')
    if (result) {
      setError(result === 'connected' ? null : `Pinterest connection: ${result}`)
      window.history.replaceState({}, '', `${window.location.pathname}#connections`)
    }
  }, [])

  async function syncBoards() {
    setSyncing(true)
    setBoardError(null)
    try {
      const result = await syncPinterestBoards()
      setBoards(result.boards)
      setLastSyncedAt(result.last_synced_at || null)
    } catch {
      setBoardError('Board sync failed.')
    } finally {
      setSyncing(false)
    }
  }

  async function updateBoard(board: PinterestBoard, body: { is_eligible?: boolean; routing_label?: string | null }) {
    try {
      const updated = await updatePinterestBoard(board.id, body)
      setBoards((items) => items?.map((item) => item.id === updated.id ? { ...item, ...updated } : item) || null)
    } catch {
      setBoardError('Could not update local configuration.')
    }
  }

  async function connectPinterest() {
    const response = await fetch('/api/channels/pinterest/oauth/start', { method: 'POST', credentials: 'include' })
    if (!response.ok) {
      setError('Pinterest connection is unavailable.')
      return
    }
    const payload = await response.json()
    window.location.assign(payload.authorization_url)
  }

  async function disconnectPinterest() {
    const response = await fetch('/api/channels/pinterest/disconnect', { method: 'POST', credentials: 'include' })
    if (response.ok) setPinterest({ status: 'NOT_CONNECTED', connected: false })
  }

  const shopifyPresentation = connectionStatus(shopify?.connected)
  const orderedChannels = SOCIAL_CHANNELS.map(({ key }) => capabilities?.channels.find((channel) => channel.key === key)).filter(Boolean) as ChannelDescriptor[]

  return <div className="ds-connections-page">
    <PageHeader
      eyebrow="Connections"
      title="Channels & services"
      description="Prepare content for Pinterest, Instagram, Facebook, LinkedIn, TikTok, and YouTube from one system. Connection and publishing status remain explicit per channel."
      actions={capabilities ? <StatusBadge status={capabilities.publishing_enabled ? 'READY' : 'BLOCKED'} label={capabilities.publishing_enabled ? 'Pinterest publishing on' : 'Pinterest publishing paused'} /> : <StatusBadge status="CHECKING" label="Checking" />}
    />

    {error ? <Alert tone="danger" title="Connection status needs attention">{error}</Alert> : null}

    <Surface className="ds-source-card">
      <div className="ds-connection-card__header">
        <div className="ds-connection-card__identity"><span className="ds-connection-card__icon"><PackageSearch size={20} /></span><div><h2>Shopify catalog source</h2><p>Product facts, media, pricing, and inventory used to ground social content.</p></div></div>
        <StatusBadge status={shopifyPresentation.status} label={shopifyPresentation.label} />
      </div>
      <div className="ds-connection-card__facts">
        <div className="ds-connection-card__fact"><span>Store</span><strong>{shopify?.shop_domain || 'Not available'}</strong></div>
        <div className="ds-connection-card__fact"><span>Last successful sync</span><strong>{shopify?.last_successful_sync_at ? new Date(shopify.last_successful_sync_at).toLocaleString() : 'Never'}</strong></div>
      </div>
      {!shopify?.connected && shopify ? <Alert tone="warning" title={shopify.message}>{shopify.guidance}</Alert> : null}
      <details className="ds-technical-details"><summary>Technical details</summary><p>API version: {shopify?.api_version || '—'} · Authentication: {shopify?.authentication_method?.replaceAll('_', ' ') || 'Not configured'}</p><p>Required scopes: {shopify?.required_scopes?.join(', ') || '—'}</p></details>
    </Surface>

    <section>
      <div className="ds-section-heading"><div><p className="ds-eyebrow">Social channels</p><h2>Six first-class content targets</h2><p>“Content ready” means generation and human review are implemented. It does not imply an external account or publishing adapter exists.</p></div></div>
      {capabilities ? <div className="ds-social-channel-grid">{orderedChannels.map((channel) => <SocialChannelCard key={channel.key} channel={channel} pinterest={pinterest} onConnectPinterest={() => void connectPinterest()} onDisconnectPinterest={() => void disconnectPinterest()} />)}</div> : <EmptyState title="Loading channel capabilities" description="Reading the server-owned capability contract." />}
    </section>

    <Surface>
      <div className="ds-section-heading"><div><p className="ds-eyebrow">Pinterest boards</p><h2>Routing destinations</h2><p>Pinterest remains the only implemented publishing destination. Provider board metadata is read-only; eligibility and routing labels are local configuration.</p>{lastSyncedAt ? <small style={{ color: 'var(--ds-text-tertiary)' }}>Last synchronized: {new Date(lastSyncedAt).toLocaleString()}</small> : null}</div><Button onClick={() => void syncBoards()} disabled={syncing || !pinterest?.connected}><RefreshCw size={15} className={syncing ? 'spin' : ''} />{syncing ? 'Syncing' : 'Sync boards'}</Button></div>
      {boardError ? <Alert tone="warning" title="Board status needs attention">{boardError}</Alert> : null}
      {boards === null ? <EmptyState title="Loading Pinterest boards" description="Reading the currently stored board inventory." /> : boards.length === 0 ? <EmptyState title="No boards available" description={pinterest?.connected ? 'Synchronize boards to refresh the stored Pinterest board inventory.' : 'Connect Pinterest before managing board routing.'} /> : <div className="ds-board-list">{boards.map((board) => <article className="ds-board-card" key={board.id}>
        <div><strong>{board.name}</strong><small>{board.pin_count ?? '—'} pins · {board.follower_count ?? '—'} followers · {board.is_active ? 'Active' : 'Inactive'}</small>{board.sections?.length ? <small>Sections: {board.sections.map((section) => section.name).join(', ')}</small> : null}<details className="ds-technical-details"><summary>Board details</summary><p>Provider ID: {board.external_board_id} · Privacy: {board.privacy || 'unavailable'} · Owner: {board.owner_username || 'unavailable'}</p></details></div>
        <div className="ds-board-card__controls"><label><input type="checkbox" checked={board.is_eligible} onChange={(event) => void updateBoard(board, { is_eligible: event.target.checked })} /> Eligible for routing</label><label>Routing label<input type="text" value={board.routing_label || ''} maxLength={120} placeholder="Optional local label" onChange={(event) => void updateBoard(board, { routing_label: event.target.value || null })} /></label></div>
      </article>)}</div>}
    </Surface>

    <Surface>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><CheckCircle2 size={18} color="var(--ds-success)" /><strong>Safety model unchanged</strong></div>
      <p style={{ color: 'var(--ds-text-secondary)', marginBottom: 0, lineHeight: 1.6 }}>Multi-channel content preparation does not create provider connections. Pinterest OAuth, immutable publication snapshots, explicit dispatch authorization, UNKNOWN no-retry behavior, and reconciliation remain unchanged.</p>
    </Surface>
  </div>
}
