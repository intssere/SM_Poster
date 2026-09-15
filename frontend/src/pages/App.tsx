import { useEffect, useState, type FormEvent } from 'react'
import { Activity, ArrowRight, CalendarDays, CheckCircle2, Clock3, PackageSearch, PlugZap, ShieldCheck, Sparkles, TriangleAlert } from 'lucide-react'
import { ProductsPage } from './Products'
import { getCreativeQa, getProposalSummary, ProposalSummary } from '../api/proposals'
import { getIntelligenceSummary, getShopifyStatus, IntelligenceSummary, ShopifyStatus } from '../api/catalog'
import { ChannelsPage } from './Channels'
import { CreativeStudioPage } from './CreativeStudio'
import { PublicationsPage } from './Publications'
import { UnifiedContentWorkspace } from './UnifiedContentWorkspace'
import { AppShell, type ProductPage } from '../ui/AppShell'
import { Alert, Button, MetricCard, PageHeader, StatusBadge, Surface } from '../ui/primitives'
import { buildHomeAttention, connectionStatus, formatOperationalCount, type BackendStatus } from '../ui/phaseBPresentation'

export function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null)
  const [loginError, setLoginError] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  useEffect(() => {
    fetch('/api/auth/status', { credentials: 'include' })
      .then((response) => response.json())
      .then((value) => setAuthenticated(Boolean(value.authenticated)))
      .catch(() => setAuthenticated(false))
  }, [])

  async function login(event: FormEvent) {
    event.preventDefault()
    setLoginError('')
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    })
    if (!response.ok) {
      setLoginError('Invalid credentials or unavailable authentication service.')
      return
    }
    setPassword('')
    setAuthenticated(true)
  }

  if (authenticated === null) return <div className="login-shell"><main className="login-panel"><p role="status">Checking authentication…</p></main></div>

  if (!authenticated) return <div className="login-shell">
    <main className="login-panel">
      <p className="eyebrow">DIAMOND SHELF</p>
      <h2>Sign in</h2>
      <form className="login-form" onSubmit={login}>
        <div className="login-field">
          <label className="login-label" htmlFor="username">Username</label>
          <input className="login-input" id="username" type="text" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
        </div>
        <div className="login-field">
          <label className="login-label" htmlFor="password">Password</label>
          <input className="login-input" id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
        </div>
        {loginError && <p className="login-error" role="alert">{loginError}</p>}
        <button className="login-button" type="submit">Sign in</button>
      </form>
    </main>
  </div>

  return <AuthenticatedDashboard />
}

type ContentView = 'library' | 'review'

type RouteState = {
  page: ProductPage
  contentView: ContentView
}

function routeFromHash(hash: string): RouteState {
  const normalized = hash.toLowerCase()
  if (normalized === '#catalog' || normalized === '#products') return { page: 'catalog', contentView: 'library' }
  if (normalized === '#create' || normalized === '#studio') return { page: 'create', contentView: 'library' }
  if (normalized === '#content/review' || normalized === '#review' || normalized === '#proposals') return { page: 'content', contentView: 'review' }
  if (normalized === '#content' || normalized === '#content/library' || normalized === '#gallery' || normalized === '#content-library') return { page: 'content', contentView: 'library' }
  if (normalized === '#calendar' || normalized === '#publications') return { page: 'calendar', contentView: 'library' }
  if (normalized === '#analytics') return { page: 'analytics', contentView: 'library' }
  if (normalized === '#connections' || normalized === '#channels') return { page: 'connections', contentView: 'library' }
  if (normalized === '#settings') return { page: 'settings', contentView: 'library' }
  return { page: 'home', contentView: 'library' }
}

const PAGE_HASH: Record<ProductPage, string> = {
  home: '',
  catalog: '#catalog',
  create: '#create',
  content: '#content/library',
  calendar: '#calendar',
  analytics: '#analytics',
  connections: '#connections',
  settings: '#settings',
}

function AuthenticatedDashboard() {
  const initialRoute = routeFromHash(window.location.hash)
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('checking')
  const [publishingEnabled, setPublishingEnabled] = useState<boolean | null>(null)
  const [proposalSummary, setProposalSummary] = useState<ProposalSummary | null>(null)
  const [creativeCount, setCreativeCount] = useState<number | null>(null)
  const [catalogSummary, setCatalogSummary] = useState<IntelligenceSummary | null>(null)
  const [shopifyStatus, setShopifyStatus] = useState<ShopifyStatus | null>(null)
  const [activePage, setActivePage] = useState<ProductPage>(initialRoute.page)
  const [contentView, setContentView] = useState<ContentView>(initialRoute.contentView)

  function selectPage(page: ProductPage) {
    const nextHash = PAGE_HASH[page]
    if (nextHash) window.location.hash = nextHash
    else history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
    setActivePage(page)
    if (page === 'content') setContentView('library')
  }

  function selectContent(view: ContentView) {
    window.location.hash = view === 'review' ? '#content/review' : '#content/library'
    setActivePage('content')
    setContentView(view)
  }

  useEffect(() => {
    const controller = new AbortController()

    fetch('/api/health', { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Health check failed: ${response.status}`)
        return response.json()
      })
      .then((health: { database_connected?: boolean; publishing_enabled?: boolean }) => {
        setBackendStatus(health.database_connected === false ? 'unavailable' : 'connected')
        setPublishingEnabled(typeof health.publishing_enabled === 'boolean' ? health.publishing_enabled : null)
      })
      .catch(() => {
        setBackendStatus('unavailable')
        setPublishingEnabled(null)
      })

    getProposalSummary().then(setProposalSummary).catch(() => null)
    getCreativeQa().then((report) => setCreativeCount(typeof report.total === 'number' ? report.total : null)).catch(() => null)
    getIntelligenceSummary(controller.signal).then(setCatalogSummary).catch(() => null)
    getShopifyStatus(controller.signal).then(setShopifyStatus).catch(() => null)

    const onHashChange = () => {
      const route = routeFromHash(window.location.hash)
      setActivePage(route.page)
      setContentView(route.contentView)
    }
    window.addEventListener('hashchange', onHashChange)
    return () => {
      controller.abort()
      window.removeEventListener('hashchange', onHashChange)
    }
  }, [])

  return <AppShell
    activePage={activePage}
    onNavigate={selectPage}
    backendStatus={backendStatus}
    publishingEnabled={publishingEnabled}
  >
    {activePage === 'catalog' ? <ProductsPage /> : null}
    {activePage === 'create' ? <CreativeStudioPage
      creativeCount={creativeCount}
      reviewCount={proposalSummary?.review || 0}
      onOpenLibrary={() => selectContent('library')}
      onOpenChannels={() => selectPage('connections')}
      onOpenQueue={() => selectContent('review')}
    /> : null}
    {activePage === 'content' ? <ContentWorkspace view={contentView} /> : null}
    {activePage === 'calendar' ? <PublicationsPage /> : null}
    {activePage === 'connections' ? <ChannelsPage /> : null}
    {activePage === 'analytics' ? <FoundationPlaceholder
      title="Analytics"
      description="The professional analytics workspace is reserved in the final information architecture. Performance dashboards and Pinterest-to-product attribution will be implemented in a later Task #57 phase without changing publishing safety behavior."
      items={['Pin and board performance', 'Product-level Pinterest outcomes', 'Creative and template comparisons', 'Operational publishing health']}
    /> : null}
    {activePage === 'settings' ? <FoundationPlaceholder
      title="Settings"
      description="Settings now has a dedicated place in the final product structure. Later phases will move AI configuration, automation policy, connection details and advanced operations here instead of exposing them throughout normal workflows."
      items={['Brand and generation preferences', 'Automation controls', 'Connection settings', 'Advanced operations and audit access']}
    /> : null}
    {activePage === 'home' ? <HomePage
      proposalSummary={proposalSummary}
      publishingEnabled={publishingEnabled}
      backendStatus={backendStatus}
      catalogSummary={catalogSummary}
      shopifyStatus={shopifyStatus}
      onNavigate={selectPage}
      onOpenReview={() => selectContent('review')}
    /> : null}
  </AppShell>
}

function ContentWorkspace({ view }: { view: ContentView }) {
  return <UnifiedContentWorkspace initialView={view} />
}

function HomePage({ proposalSummary, publishingEnabled, backendStatus, catalogSummary, shopifyStatus, onNavigate, onOpenReview }: {
  proposalSummary: ProposalSummary | null
  publishingEnabled: boolean | null
  backendStatus: BackendStatus
  catalogSummary: IntelligenceSummary | null
  shopifyStatus: ShopifyStatus | null
  onNavigate: (page: ProductPage) => void
  onOpenReview: () => void
}) {
  const reviewCount = proposalSummary?.review || 0
  const scheduledCount = proposalSummary?.scheduled || 0
  const qaWarnings = catalogSummary?.qa_warning_products || 0
  const attention = buildHomeAttention({
    backendStatus,
    publishingEnabled,
    reviewCount,
    qaWarnings,
    shopifyConnected: shopifyStatus ? shopifyStatus.connected : null,
  })
  const healthText = backendStatus === 'connected' ? 'System healthy' : backendStatus === 'unavailable' ? 'System unavailable' : 'Checking system health'
  const shopifyPresentation = connectionStatus(shopifyStatus?.connected)

  function openAttention(key: string) {
    if (key === 'review') onOpenReview()
    else if (key === 'catalog') onNavigate('catalog')
    else if (key === 'shopify') onNavigate('connections')
  }

  return <>
    <PageHeader
      eyebrow="Home"
      title="Content Operations"
      description="See what needs attention, move work forward, and confirm the systems behind publishing are ready."
      actions={<StatusBadge status={backendStatus === 'connected' ? 'CONNECTED' : backendStatus === 'unavailable' ? 'UNAVAILABLE' : 'CHECKING'} label={healthText} />}
    />

    {backendStatus === 'unavailable' ? <Alert tone="danger" title="Backend unavailable">Live operational data may be incomplete. No publishing state has been changed.</Alert> : null}
    {backendStatus === 'connected' && publishingEnabled === false ? <Alert tone="warning" title="Publishing is paused">Content remains available for review and scheduling, but the production publishing gate is currently off.</Alert> : null}

    <section className="ds-metrics" aria-label="Operational summary" style={{ marginTop: backendStatus === 'unavailable' || publishingEnabled === false ? 18 : 0 }}>
      <MetricCard label="Catalog" value={formatOperationalCount(catalogSummary?.total)} note="Products currently available to the content system" icon={<PackageSearch size={19} aria-hidden="true" />} />
      <MetricCard label="Needs review" value={reviewCount} note="Content waiting for a decision" icon={<Activity size={19} aria-hidden="true" />} />
      <MetricCard label="Approved" value={proposalSummary?.approved || 0} note="Content approved for publishing workflows" icon={<CheckCircle2 size={19} aria-hidden="true" />} />
      <MetricCard label="Scheduled" value={scheduledCount} note={publishingEnabled ? 'Publishing gate is enabled' : 'Publishing gate is not currently enabled'} icon={<Clock3 size={19} aria-hidden="true" />} />
    </section>

    <div className="ds-home-grid">
      <Surface>
        <div className="ds-section-heading"><div><p className="ds-eyebrow">Attention</p><h2>What needs you now</h2><p>Only current operational conditions appear here.</p></div>{attention.length ? <StatusBadge status="BLOCKED" label={`${attention.length} signal${attention.length === 1 ? '' : 's'}`} /> : <StatusBadge status="READY" label="All clear" />}</div>
        {attention.length ? <div className="ds-attention-list">{attention.map((item) => <div className={`ds-attention-row ds-attention-row--${item.tone}`} key={item.key}>
          <span className="ds-attention-row__icon" aria-hidden="true"><TriangleAlert size={17} /></span>
          <div><strong>{item.title}</strong><small>{item.detail}</small></div>
          {['review', 'catalog', 'shopify'].includes(item.key) ? <Button variant="ghost" onClick={() => openAttention(item.key)}>Open <ArrowRight size={14} /></Button> : null}
        </div>)}</div> : <div className="ds-all-clear"><CheckCircle2 size={28} aria-hidden="true" /><strong>No operational attention items</strong><span>Review, catalog QA, Shopify connection, system health and publishing gate are clear.</span></div>}
      </Surface>

      <Surface>
        <div className="ds-section-heading"><div><p className="ds-eyebrow">Workflow</p><h2>Move work forward</h2><p>Jump directly to the next operating step.</p></div></div>
        <div className="ds-quick-actions">
          <button type="button" className="ds-quick-action" onClick={() => onNavigate('catalog')}><span><PackageSearch size={18} /><strong>Browse catalog</strong><small>Find products and inspect readiness.</small></span><ArrowRight size={16} /></button>
          <button type="button" className="ds-quick-action" onClick={() => onNavigate('create')}><span><Sparkles size={18} /><strong>Create content</strong><small>Start from an eligible product.</small></span><ArrowRight size={16} /></button>
          <button type="button" className="ds-quick-action" onClick={onOpenReview}><span><Activity size={18} /><strong>Review content</strong><small>{reviewCount ? `${reviewCount.toLocaleString()} waiting now.` : 'Nothing waiting right now.'}</small></span><ArrowRight size={16} /></button>
          <button type="button" className="ds-quick-action" onClick={() => onNavigate('calendar')}><span><CalendarDays size={18} /><strong>Open calendar</strong><small>{scheduledCount ? `${scheduledCount.toLocaleString()} scheduled.` : 'No scheduled count reported.'}</small></span><ArrowRight size={16} /></button>
        </div>
      </Surface>
    </div>

    <div className="ds-home-grid">
      <Surface>
        <div className="ds-section-heading"><div><p className="ds-eyebrow">Connections</p><h2>Operational readiness</h2><p>Connection health without exposing low-level provider detail.</p></div><Button variant="ghost" onClick={() => onNavigate('connections')}>Manage <PlugZap size={15} /></Button></div>
        <div className="ds-connection-pulse">
          <div className="ds-connection-pulse__row"><div><strong>Shopify</strong><small>{shopifyStatus?.connected ? shopifyStatus.shop_domain || 'Catalog source connected' : shopifyStatus?.message || 'Connection status loading'}</small></div><StatusBadge status={shopifyPresentation.status} label={shopifyPresentation.label} /></div>
          <div className="ds-connection-pulse__row"><div><strong>Publishing gate</strong><small>Controls whether approved scheduled work may dispatch.</small></div><StatusBadge status={publishingEnabled === true ? 'READY' : publishingEnabled === false ? 'BLOCKED' : 'CHECKING'} label={publishingEnabled === true ? 'Publishing on' : publishingEnabled === false ? 'Publishing paused' : 'Checking'} /></div>
          <div className="ds-connection-pulse__row"><div><strong>Catalog QA</strong><small>{qaWarnings ? `${qaWarnings.toLocaleString()} products have normalization warnings.` : 'No catalog QA warnings reported.'}</small></div><StatusBadge status={qaWarnings ? 'BLOCKED' : 'READY'} label={qaWarnings ? 'Needs review' : 'Ready'} /></div>
        </div>
      </Surface>

      <Surface>
        <div className="ds-section-heading"><div><p className="ds-eyebrow">Operating model</p><h2>Simple outside. Rigorous inside.</h2></div></div>
        <div className="status-row" aria-label="Content lifecycle"><span>CATALOG</span><b aria-hidden="true">→</b><span>CREATE</span><b aria-hidden="true">→</b><span>NEEDS REVIEW</span><b aria-hidden="true">→</b><span>APPROVED</span><b aria-hidden="true">→</b><span>SCHEDULED</span><b aria-hidden="true">→</b><span>PUBLISHED</span></div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 18 }}><ShieldCheck size={20} color="var(--ds-brand)" aria-hidden="true" /><strong>Publishing safety remains unchanged</strong></div>
        <p style={{ color: 'var(--ds-text-secondary)', lineHeight: 1.6, marginBottom: 0 }}>Approvals, immutable publication identity, one-shot dispatch protections, no-blind-retry behavior and reconciliation rules remain behind the workflow.</p>
      </Surface>
    </div>
  </>
}

function FoundationPlaceholder({ title, description, items }: { title: string; description: string; items: string[] }) {
  return <div className="ds-foundation-placeholder">
    <PageHeader eyebrow="Phase A foundation" title={title} description={description} />
    <Surface>
      <strong>Reserved capabilities</strong>
      <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>
    </Surface>
  </div>
}
