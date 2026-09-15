import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, Clock3, PackageSearch, PlugZap, Radio, RefreshCw, ShieldAlert } from 'lucide-react'
import { ChannelCapabilities, ChannelDescriptor, getChannelCapabilities, getPinterestBoards, PinterestBoard, syncPinterestBoards, updatePinterestBoard } from '../api/channels'
import { getShopifyStatus, ShopifyStatus } from '../api/catalog'
import { Alert, Button, EmptyState, PageHeader, StatusBadge, Surface } from '../ui/primitives'
import { connectionStatus } from '../ui/phaseBPresentation'

function FutureChannel({ channel }: { channel: ChannelDescriptor }) {
  return <article className="ds-future-channel">
    <strong>{channel.label}</strong>
    <small>{channel.capability_summary}</small>
    <StatusBadge status="DISCONNECTED" label="Not connected" />
  </article>
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
  const pinterestPresentation = connectionStatus(pinterest?.connected)
  const futureChannels = capabilities?.channels.filter((channel) => channel.key !== 'pinterest') || []

  return <div className="ds-connections-page">
    <PageHeader
      eyebrow="Connections"
      title="Connected services"
      description="Keep product data and Pinterest publishing services healthy from one workspace. Technical details stay available when you need them."
      actions={capabilities ? <StatusBadge status={capabilities.publishing_enabled ? 'READY' : 'BLOCKED'} label={capabilities.publishing_enabled ? 'Publishing on' : 'Publishing paused'} /> : <StatusBadge status="CHECKING" label="Checking" />}
    />

    {error ? <Alert tone="danger" title="Connection status needs attention">{error}</Alert> : null}

    <div className="ds-connection-grid">
      <Surface className="ds-connection-card">
        <div className="ds-connection-card__header">
          <div className="ds-connection-card__identity"><span className="ds-connection-card__icon"><PackageSearch size={20} /></span><div><h2>Shopify</h2><p>Source of truth for product facts used by the content workflow.</p></div></div>
          <StatusBadge status={shopifyPresentation.status} label={shopifyPresentation.label} />
        </div>
        <div className="ds-connection-card__facts">
          <div className="ds-connection-card__fact"><span>Store</span><strong>{shopify?.shop_domain || 'Not available'}</strong></div>
          <div className="ds-connection-card__fact"><span>Last successful sync</span><strong>{shopify?.last_successful_sync_at ? new Date(shopify.last_successful_sync_at).toLocaleString() : 'Never'}</strong></div>
        </div>
        {!shopify?.connected && shopify ? <Alert tone="warning" title={shopify.message}>{shopify.guidance}</Alert> : null}
        <details className="ds-technical-details"><summary>Technical details</summary><p>API version: {shopify?.api_version || '—'} · Authentication: {shopify?.authentication_method?.replaceAll('_', ' ') || 'Not configured'}</p><p>Required scopes: {shopify?.required_scopes?.join(', ') || '—'}</p></details>
      </Surface>

      <Surface className="ds-connection-card">
        <div className="ds-connection-card__header">
          <div className="ds-connection-card__identity"><span className="ds-connection-card__icon"><Radio size={20} /></span><div><h2>Pinterest</h2><p>Publishing destination, board inventory, and routing configuration.</p></div></div>
          <StatusBadge status={pinterestPresentation.status} label={pinterestPresentation.label} />
        </div>
        <div className="ds-connection-card__facts">
          <div className="ds-connection-card__fact"><span>Account</span><strong>{pinterest?.account?.username || (pinterest?.connected ? 'Pinterest account' : 'Not connected')}</strong></div>
          <div className="ds-connection-card__fact"><span>Boards</span><strong>{boards ? boards.filter((board) => board.is_active).length.toLocaleString() : '—'} active</strong></div>
        </div>
        <div className="ds-connection-card__actions">{pinterest?.connected ? <Button variant="ghost" onClick={() => void disconnectPinterest()}>Disconnect Pinterest</Button> : <Button variant="primary" onClick={() => void connectPinterest()}><PlugZap size={15} />Connect Pinterest</Button>}</div>
        {pinterest?.connected ? <details className="ds-technical-details"><summary>Technical details</summary><p>Granted scopes: {(pinterest.account?.granted_scopes || []).join(', ') || '—'}</p><p>Access expires: {pinterest.account?.access_token_expires_at || 'Unknown'} · Refresh expires: {pinterest.account?.refresh_token_expires_at || 'Unknown'}</p></details> : null}
      </Surface>
    </div>

    <Surface>
      <div className="ds-section-heading"><div><p className="ds-eyebrow">Pinterest boards</p><h2>Routing destinations</h2><p>Provider board metadata remains read-only. Eligibility and routing labels are local configuration.</p>{lastSyncedAt ? <small style={{ color: 'var(--ds-text-tertiary)' }}>Last synchronized: {new Date(lastSyncedAt).toLocaleString()}</small> : null}</div><Button onClick={() => void syncBoards()} disabled={syncing || !pinterest?.connected}><RefreshCw size={15} className={syncing ? 'spin' : ''} />{syncing ? 'Syncing' : 'Sync boards'}</Button></div>
      {boardError ? <Alert tone="warning" title="Board status needs attention">{boardError}</Alert> : null}
      {boards === null ? <EmptyState title="Loading Pinterest boards" description="Reading the currently stored board inventory." /> : boards.length === 0 ? <EmptyState title="No boards available" description={pinterest?.connected ? 'Synchronize boards to refresh the stored Pinterest board inventory.' : 'Connect Pinterest before managing board routing.'} /> : <div className="ds-board-list">{boards.map((board) => <article className="ds-board-card" key={board.id}>
        <div><strong>{board.name}</strong><small>{board.pin_count ?? '—'} pins · {board.follower_count ?? '—'} followers · {board.is_active ? 'Active' : 'Inactive'}</small>{board.sections?.length ? <small>Sections: {board.sections.map((section) => section.name).join(', ')}</small> : null}<details className="ds-technical-details"><summary>Board details</summary><p>Provider ID: {board.external_board_id} · Privacy: {board.privacy || 'unavailable'} · Owner: {board.owner_username || 'unavailable'}</p></details></div>
        <div className="ds-board-card__controls"><label><input type="checkbox" checked={board.is_eligible} onChange={(event) => void updateBoard(board, { is_eligible: event.target.checked })} /> Eligible for routing</label><label>Routing label<input type="text" value={board.routing_label || ''} maxLength={120} placeholder="Optional local label" onChange={(event) => void updateBoard(board, { routing_label: event.target.value || null })} /></label></div>
      </article>)}</div>}
    </Surface>

    <Surface>
      <div className="ds-section-heading"><div><p className="ds-eyebrow">Future channels</p><h2>Planned adapters</h2><p>These capabilities are intentionally secondary until their connection and publishing paths are implemented and authorized.</p></div><StatusBadge status="DISCONNECTED" label="Not active" /></div>
      {futureChannels.length ? <div className="ds-future-channels">{futureChannels.map((channel) => <FutureChannel key={channel.key} channel={channel} />)}</div> : <div className="ds-all-clear"><Clock3 size={24} /><strong>No additional channel adapters reported</strong></div>}
    </Surface>

    <Surface>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><CheckCircle2 size={18} color="var(--ds-success)" /><strong>Safety model unchanged</strong></div>
      <p style={{ color: 'var(--ds-text-secondary)', marginBottom: 0, lineHeight: 1.6 }}>This workspace changes connection presentation only. Existing OAuth, board synchronization, local routing configuration, publishing authorization, retry, and reconciliation behavior is unchanged.</p>
    </Surface>
  </div>
}
