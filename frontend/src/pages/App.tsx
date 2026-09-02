import { useEffect, useState, type FormEvent } from 'react'
import { CheckCircle2, Clock3, Image, Images, PackageSearch, Radio, ShieldCheck, Sparkles } from 'lucide-react'
import { ProductsPage } from './Products'
import { ProposalsPage } from './Proposals'
import { CreativeGalleryPage } from './CreativeGallery'
import { getCreativeQa, getProposalSummary, ProposalSummary } from '../api/proposals'
import { ChannelsPage } from './Channels'
import { CreativeStudioPage } from './CreativeStudio'
import { PublicationsPage } from './Publications'

export function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null)
  const [loginError, setLoginError] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  useEffect(() => { fetch('/api/auth/status', { credentials: 'include' }).then((r) => r.json()).then((v) => setAuthenticated(Boolean(v.authenticated))).catch(() => setAuthenticated(false)) }, [])
  async function login(event: FormEvent) {
    event.preventDefault(); setLoginError('')
    const response = await fetch('/api/auth/login', { method: 'POST', headers: {'Content-Type': 'application/json'}, credentials: 'include', body: JSON.stringify({ username, password }) })
    if (!response.ok) { setLoginError('Invalid credentials or unavailable authentication service.'); return }
    setPassword(''); setAuthenticated(true)
  }
  if (authenticated === null) return <div className="app-shell"><main><p>Checking authentication…</p></main></div>
  if (!authenticated) return <div className="app-shell"><main><section className="panel" style={{maxWidth: 420, margin: '10vh auto'}}><p className="eyebrow">DIAMOND SHELF</p><h2>Sign in</h2><form onSubmit={login}><label>Username<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></label><label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>{loginError && <p role="alert">{loginError}</p>}<button type="submit">Sign in</button></form></section></main></div>
  return <AuthenticatedDashboard />
}

function AuthenticatedDashboard() {
  const [backendStatus, setBackendStatus] = useState<'checking' | 'connected' | 'unavailable'>('checking')
  const [proposalSummary, setProposalSummary] = useState<ProposalSummary | null>(null)
  const [creativeCount, setCreativeCount] = useState<number | null>(null)
  type Page = 'overview' | 'products' | 'proposals' | 'gallery' | 'studio' | 'channels' | 'publications'
  function pageFromHash(hash: string): Page {
    if (hash === '#products') return 'products'
    if (hash === '#proposals') return 'proposals'
    if (hash === '#gallery' || hash === '#content-library') return 'gallery'
    if (hash === '#studio') return 'studio'
    if (hash === '#channels') return 'channels'
    if (hash === '#publications') return 'publications'
    return 'overview'
  }
  const [activePage, setActivePage] = useState<Page>(() => pageFromHash(window.location.hash))

  function selectPage(page: Page) {
    window.location.hash = page === 'overview' ? '' : page
    setActivePage(page)
  }

  useEffect(() => {
    const controller = new AbortController()

    fetch('/api/health', { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Health check failed: ${response.status}`)
        return response.json()
      })
      .then((health: { database_connected?: boolean }) => {
        setBackendStatus(health.database_connected === false ? 'unavailable' : 'connected')
      })
      .catch(() => setBackendStatus('unavailable'))
    getProposalSummary().then(setProposalSummary).catch(() => null)
    getCreativeQa().then((report) => setCreativeCount(typeof report.total === 'number' ? report.total : null)).catch(() => null)

    const onHashChange = () => setActivePage(pageFromHash(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => {
      controller.abort()
      window.removeEventListener('hashchange', onHashChange)
    }
  }, [])

  const backendLabel = {
    checking: 'Checking backend connection',
    connected: 'Backend connected',
    unavailable: 'Backend unavailable',
  }[backendStatus]
  const cards = [
    ['Catalog', '2,997', 'Shopify products target', PackageSearch],
      ['Content proposals', String(proposalSummary?.total || 0), `${proposalSummary?.review || 0} awaiting review`, Image],
    ['Approved', String(proposalSummary?.approved || 0), 'Human approval required', CheckCircle2],
    ['Scheduled', String(proposalSummary?.scheduled || 0), 'Publishing disabled in Phase 0', Clock3],
  ]

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-mark">DS</div>
       <div><h1>Diamond Shelf</h1><p>Social Studio</p></div>
       <nav><button className={activePage === 'overview' ? 'active' : ''} onClick={() => selectPage('overview')}>Overview</button><button className={activePage === 'products' ? 'active' : ''} onClick={() => selectPage('products')}>Products</button><button className={activePage === 'gallery' ? 'active' : ''} onClick={() => selectPage('gallery')}><Images size={15} />Content Library<span className="nav-count">{creativeCount ?? '—'}</span></button><button className={activePage === 'studio' ? 'active' : ''} onClick={() => selectPage('studio')}><Sparkles size={15} />Creative Studio</button><button className={activePage === 'channels' ? 'active' : ''} onClick={() => selectPage('channels')}><Radio size={15} />Channels</button><button className={activePage === 'proposals' ? 'active' : ''} onClick={() => selectPage('proposals')}>Approval Queue</button><span>Integrations</span><span>Drafts</span><span>Templates</span><span>Campaigns</span></nav>
    </aside>
     <main>{activePage === 'products' ? <ProductsPage/> : activePage === 'proposals' ? <ProposalsPage onOpenGallery={() => selectPage('gallery')} /> : activePage === 'gallery' ? <CreativeGalleryPage onOpenQueue={() => selectPage('proposals')} /> : activePage === 'channels' ? <ChannelsPage /> : activePage === 'publications' ? <PublicationsPage /> : activePage === 'studio' ? <CreativeStudioPage creativeCount={creativeCount} reviewCount={proposalSummary?.review || 0} onOpenLibrary={() => selectPage('gallery')} onOpenChannels={() => selectPage('channels')} onOpenQueue={() => selectPage('proposals')} /> : <>
      <header><div><p className="eyebrow">SOCIAL STUDIO FOUNDATION</p><h2>Content Operations Dashboard</h2><p>Catalog → content variants → human approval → channel adapters.</p></div><div className="header-status"><div className="gate"><ShieldCheck size={18}/><span>Production publishing disabled</span></div><div className={`connection-state ${backendStatus}`} aria-live="polite"><span className="status-dot" />{backendLabel}</div></div></header>
      <section className="cards">{cards.map(([name,value,note,Icon]: any) => <article key={name}><Icon size={22}/><p>{name}</p><strong>{value}</strong><small>{note}</small></article>)}</section>
      <section className="panel">
        <div><p className="eyebrow">CONTENT PIPELINE</p><h3>One trusted catalog, multiple future channels</h3><p>ProductIntelligence and fact-safe content stay platform-neutral until a channel adapter applies its own variant and media requirements.</p></div>
        <div className="flow"><span>Shopify</span><b>→</b><span>ProductIntelligence</span><b>→</b><span>Content Library</span><b>→</b><span>Approval</span><b>→</b><span>Channel Adapter</span></div>
      </section>
      <section className="panel approval">
        <div><p className="eyebrow">APPROVAL WORKFLOW</p><h3>Review stays separate from distribution</h3></div>
        <div className="status-row"><span>GENERATED</span><b>→</b><span>READY FOR REVIEW</span><b>→</b><span>APPROVED</span><b>→</b><span>CHANNEL READY</span></div>
      </section>
    </>}</main>
  </div>
}
