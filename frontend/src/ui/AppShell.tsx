import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  BarChart3,
  CalendarDays,
  Home,
  Layers3,
  Menu,
  PackageSearch,
  PlugZap,
  Settings,
  Sparkles,
  X,
} from 'lucide-react'
import { StatusBadge } from './primitives'

export type ProductPage = 'home' | 'catalog' | 'create' | 'content' | 'calendar' | 'analytics' | 'connections' | 'settings'

export const PAGE_META: Record<ProductPage, { label: string; shortLabel: string; description: string }> = {
  home: { label: 'Home', shortLabel: 'Home', description: 'Your content operation at a glance' },
  catalog: { label: 'Catalog', shortLabel: 'Catalog', description: 'Products and product intelligence' },
  create: { label: 'Create', shortLabel: 'Create', description: 'Build Pinterest-ready content' },
  content: { label: 'Content', shortLabel: 'Content', description: 'Drafts, review, approvals and creative history' },
  calendar: { label: 'Calendar', shortLabel: 'Calendar', description: 'Schedule, queue and publishing activity' },
  analytics: { label: 'Analytics', shortLabel: 'Analytics', description: 'Performance and growth insights' },
  connections: { label: 'Connections', shortLabel: 'Connect', description: 'Pinterest, Shopify and publishing services' },
  settings: { label: 'Settings', shortLabel: 'Settings', description: 'Brand, automation and advanced controls' },
}

const desktopNav = [
  { id: 'home' as const, icon: Home },
  { id: 'catalog' as const, icon: PackageSearch },
  { id: 'create' as const, icon: Sparkles },
  { id: 'content' as const, icon: Layers3 },
  { id: 'calendar' as const, icon: CalendarDays },
  { id: 'analytics' as const, icon: BarChart3 },
  { id: 'connections' as const, icon: PlugZap },
  { id: 'settings' as const, icon: Settings },
]

const mobilePrimary = desktopNav.filter((item) => ['home', 'content', 'create', 'calendar'].includes(item.id))
const mobileMore = desktopNav.filter((item) => !['home', 'content', 'create', 'calendar'].includes(item.id))

export function AppShell({ activePage, onNavigate, backendStatus, publishingEnabled, children }: {
  activePage: ProductPage
  onNavigate: (page: ProductPage) => void
  backendStatus: 'checking' | 'connected' | 'unavailable'
  publishingEnabled: boolean | null
  children: ReactNode
}) {
  const [moreOpen, setMoreOpen] = useState(false)
  const moreButtonRef = useRef<HTMLButtonElement>(null)
  const firstMoreItemRef = useRef<HTMLButtonElement>(null)
  const page = PAGE_META[activePage]

  function navigate(pageId: ProductPage) {
    setMoreOpen(false)
    onNavigate(pageId)
  }

  useEffect(() => {
    if (moreOpen) firstMoreItemRef.current?.focus()
  }, [moreOpen])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape' && moreOpen) {
        setMoreOpen(false)
        moreButtonRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [moreOpen])

  const systemStatus = backendStatus === 'connected' ? 'CONNECTED' : backendStatus === 'unavailable' ? 'UNAVAILABLE' : 'CHECKING'

  return <div className="ds-app-shell">
    <a className="ds-skip-link" href="#main-content">Skip to main content</a>
    <aside className="ds-sidebar" aria-label="Primary">
      <div className="ds-brand">
        <div className="ds-brand__mark" aria-hidden="true">DS</div>
        <div className="ds-brand__copy"><strong>Diamond Shelf</strong><span>Social Studio</span></div>
      </div>
      <nav className="ds-nav" aria-label="Main navigation">
        {desktopNav.map(({ id, icon: Icon }) => <button
          key={id}
          type="button"
          className={`ds-nav__item ${activePage === id ? 'is-active' : ''}`}
          onClick={() => navigate(id)}
          aria-current={activePage === id ? 'page' : undefined}
          title={PAGE_META[id].label}
        >
          <Icon size={19} aria-hidden="true" />
          <span>{PAGE_META[id].label}</span>
        </button>)}
      </nav>
      <div className="ds-sidebar__footer">
        <StatusBadge status={systemStatus} label={backendStatus === 'connected' ? 'System healthy' : backendStatus === 'unavailable' ? 'System unavailable' : 'Checking system'} />
        <p>{publishingEnabled === true ? 'Publishing enabled' : publishingEnabled === false ? 'Publishing paused' : 'Publishing status loading'}</p>
      </div>
    </aside>

    <div className="ds-workspace">
      <div className="ds-topbar">
        <div>
          <span className="ds-topbar__section">Social Studio</span>
          <strong>{page.label}</strong>
          <small>{page.description}</small>
        </div>
        <div className="ds-topbar__status">
          <StatusBadge status={systemStatus} label={backendStatus === 'connected' ? 'Healthy' : backendStatus === 'unavailable' ? 'Unavailable' : 'Checking'} />
          {publishingEnabled !== null ? <StatusBadge status={publishingEnabled ? 'READY' : 'BLOCKED'} label={publishingEnabled ? 'Publishing on' : 'Publishing paused'} /> : null}
        </div>
      </div>
      <main id="main-content" className="ds-main" tabIndex={-1}>{children}</main>
    </div>

    <nav className="ds-mobile-nav" aria-label="Mobile navigation">
      {mobilePrimary.map(({ id, icon: Icon }) => <button
        key={id}
        type="button"
        className={activePage === id ? 'is-active' : ''}
        onClick={() => navigate(id)}
        aria-current={activePage === id ? 'page' : undefined}
      >
        <Icon size={19} aria-hidden="true" />
        <span>{PAGE_META[id].shortLabel}</span>
      </button>)}
      <button ref={moreButtonRef} type="button" onClick={() => setMoreOpen((open) => !open)} aria-expanded={moreOpen} aria-controls="mobile-more-menu">
        <Menu size={19} aria-hidden="true" /><span>More</span>
      </button>
    </nav>

    {moreOpen ? <div className="ds-mobile-sheet-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) {
        setMoreOpen(false)
        moreButtonRef.current?.focus()
      }
    }}>
      <div className="ds-mobile-sheet" id="mobile-more-menu" role="dialog" aria-modal="true" aria-label="More navigation">
        <div className="ds-mobile-sheet__header"><strong>More</strong><button type="button" className="ds-icon-button" onClick={() => { setMoreOpen(false); moreButtonRef.current?.focus() }} aria-label="Close navigation"><X size={20} /></button></div>
        <div className="ds-mobile-sheet__items">
          {mobileMore.map(({ id, icon: Icon }, index) => <button
            ref={index === 0 ? firstMoreItemRef : undefined}
            key={id}
            type="button"
            className={activePage === id ? 'is-active' : ''}
            onClick={() => navigate(id)}
          ><Icon size={20} aria-hidden="true" /><span><strong>{PAGE_META[id].label}</strong><small>{PAGE_META[id].description}</small></span></button>)}
        </div>
      </div>
    </div> : null}
  </div>
}
