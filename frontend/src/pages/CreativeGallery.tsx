import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronLeft, ChevronRight, ExternalLink, Fingerprint, Image as ImageIcon,
  Images, Maximize2, ShieldAlert, X,
} from 'lucide-react'
import { getProposals, PinProposal } from '../api/proposals'

type FilterKey = 'template' | 'category' | 'audience' | 'designer' | 'arabian' | 'niche'
type Filters = Record<FilterKey, string>

const FILTERS: Array<{ key: FilterKey; label: string }> = [
  { key: 'template', label: 'Template' },
  { key: 'category', label: 'Category' },
  { key: 'audience', label: 'Audience' },
  { key: 'designer', label: 'Designer' },
  { key: 'arabian', label: 'Arabian' },
  { key: 'niche', label: 'Niche' },
]

const EMPTY_FILTERS: Filters = {
  template: 'all', category: 'all', audience: 'all',
  designer: 'all', arabian: 'all', niche: 'all',
}

function fact(proposal: PinProposal, key: string) {
  const value = proposal.intelligence_facts_used?.[key]
  return value === null || value === undefined || value === '' ? null : String(value)
}

function pretty(value: unknown) {
  if (value === null || value === undefined || value === '') return 'Not supplied'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function templateLabel(proposal: PinProposal) {
  return proposal.creative_template || proposal.creative_template_key.replaceAll('_', ' ')
}

function metadata(proposal: PinProposal) {
  const creative = proposal.creative!
  const specification = creative.specification || {}
  const image = typeof specification.image === 'object' && specification.image
    ? specification.image as Record<string, unknown>
    : {}
  return { creative, specification, image }
}

function warningsFor(proposal: PinProposal) {
  const warnings = [...proposal.warnings]
  if (proposal.missing_facts.length) warnings.push(`Missing or unknown: ${proposal.missing_facts.join(', ')}`)
  if (proposal.unsupported_claims.length) warnings.push(`Unsupported claims: ${proposal.unsupported_claims.join(', ')}`)
  if (proposal.creative?.error) warnings.push(proposal.creative.error)
  return [...new Set(warnings)]
}

function CreativeModal({
  proposals, index, onClose, onMove, returnFocus,
}: {
  proposals: PinProposal[]
  index: number
  onClose: () => void
  onMove: (index: number) => void
  returnFocus: HTMLElement | null
}) {
  const [compare, setCompare] = useState(true)
  const dialogRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const proposal = proposals[index]
  const { creative, image } = metadata(proposal)
  const warnings = warningsFor(proposal)

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
      if (event.key === 'ArrowLeft') onMove((index - 1 + proposals.length) % proposals.length)
      if (event.key === 'ArrowRight') onMove((index + 1) % proposals.length)
      if (event.key === 'Tab' && dialogRef.current) {
        const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>('button, a[href], select, [tabindex]:not([tabindex="-1"])')]
          .filter((element) => !element.hasAttribute('disabled'))
        if (!focusable.length) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [index, onClose, onMove, proposals.length])

  useEffect(() => {
    closeRef.current?.focus()
    return () => returnFocus?.focus()
  }, [returnFocus])

  return <div className="gallery-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }}>
    <section ref={dialogRef} className="gallery-modal" role="dialog" aria-modal="true" aria-label={`Creative preview for ${proposal.product_title}`}>
      <header className="gallery-modal-header">
        <div>
          <p className="eyebrow">CONTENT CREATIVE {index + 1} OF {proposals.length}</p>
          <h2>{proposal.product_title}</h2>
          <p>{proposal.vendor || 'Unknown brand'} · {proposal.content_angle}</p>
        </div>
        <button ref={closeRef} className="icon-action" onClick={onClose} aria-label="Close creative preview"><X size={20} /></button>
      </header>
      <div className="gallery-modal-toolbar">
        <button className={compare ? 'gallery-toggle active' : 'gallery-toggle'} onClick={() => setCompare(!compare)}>
          <Images size={15} /> {compare ? 'Compare source' : 'Show source'}
        </button>
        <span className={`creative-status creative-status-${creative.status.toLowerCase()}`}><span className="status-dot" />{creative.status}</span>
        <div className="gallery-modal-nav">
          <button className="icon-action" onClick={() => onMove((index - 1 + proposals.length) % proposals.length)} aria-label="Previous creative"><ChevronLeft size={19} /></button>
          <button className="icon-action" onClick={() => onMove((index + 1) % proposals.length)} aria-label="Next creative"><ChevronRight size={19} /></button>
        </div>
      </div>
      <div className={compare ? 'gallery-modal-images compare' : 'gallery-modal-images'}>
        {compare && <div className="gallery-modal-image-frame">
          <span className="creative-label">Authentic Shopify source <span>catalog</span></span>
          <div className="gallery-modal-image"><img src={proposal.image_url} alt={`Authentic Shopify source for ${proposal.product_title}`} /></div>
        </div>}
        <div className="gallery-modal-image-frame">
          <span className="creative-label">Rendered Pinterest creative <span>2:3</span></span>
          <div className="gallery-modal-image"><img src={creative.image_url || ''} alt={`Rendered Pinterest creative for ${proposal.product_title}`} /></div>
        </div>
      </div>
      <div className="gallery-modal-details">
        <div className="gallery-detail-copy">
          <p className="eyebrow">REVIEW CONTEXT</p>
          <h3>{proposal.headline}</h3>
          <div className="gallery-tags"><span>{templateLabel(proposal)} v{creative.template_version || 1}</span><span>Proposal {proposal.approval_status}</span><span>{proposal.content_angle}</span></div>
          <p className="gallery-safety"><ShieldAlert size={14} /> Rendering is separate from approval. Publishing is disabled.</p>
        </div>
        <dl className="gallery-metadata">
          <div><dt>Dimensions</dt><dd>{creative.width} × {creative.height}px</dd></div>
          <div><dt>Source media ID</dt><dd>{pretty(image.shopify_media_id)}</dd></div>
          <div><dt>Source checksum</dt><dd>{pretty(image.checksum_sha256)}</dd></div>
          <div><dt>Creative fingerprint</dt><dd>{pretty(creative.creative_fingerprint)}</dd></div>
          <div><dt>Rendered SHA-256</dt><dd>{pretty(creative.sha256)}</dd></div>
          <div><dt>Source URL</dt><dd>{pretty(image.provenance_url)}</dd></div>
        </dl>
      </div>
      {warnings.length > 0 && <div className="gallery-warning"><ShieldAlert size={15} /><div><strong>Review warnings</strong>{warnings.map((warning) => <span key={warning}>{warning}</span>)}</div></div>}
      <footer className="gallery-modal-footer">
        <span>Use ← → to navigate · Esc to close</span>
        <a href={proposal.canonical_url} target="_blank" rel="noreferrer">Open product <ExternalLink size={14} /></a>
      </footer>
    </section>
  </div>
}

function GalleryCard({ proposal, onOpen }: { proposal: PinProposal; onOpen: () => void }) {
  const creative = proposal.creative!
  return <article className="gallery-card">
    <button className="gallery-card-preview" onClick={onOpen} aria-label={`Open full-size preview for ${proposal.product_title}`}>
      <img src={creative.image_url || ''} alt={`Rendered Pinterest creative for ${proposal.product_title}`} />
      <span className="gallery-expand"><Maximize2 size={15} /> Full-size</span>
      <span className={`badge ${proposal.approval_status.toLowerCase()}`}>{proposal.approval_status}</span>
    </button>
    <div className="gallery-card-content">
      <div className="gallery-card-title"><div><p className="eyebrow">{proposal.content_angle}</p><h3>{proposal.product_title}</h3><p>{proposal.vendor || 'Unknown brand'}</p></div><span className="creative-status creative-status-rendered"><span className="status-dot" />{creative.status}</span></div>
      <div className="gallery-card-meta"><span>{templateLabel(proposal)} v{creative.template_version || 1}</span><span>{fact(proposal, 'normalization_category') || 'Category not supplied'}</span></div>
      <div className="gallery-card-facts">
        {fact(proposal, 'audience') && <span>Audience: {fact(proposal, 'audience')}</span>}
        {fact(proposal, 'designer') && <span>Designer: {fact(proposal, 'designer')}</span>}
        {fact(proposal, 'arabian') && <span>Arabian: {fact(proposal, 'arabian')}</span>}
        {fact(proposal, 'niche') && <span>Niche: {fact(proposal, 'niche')}</span>}
      </div>
      <button className="gallery-review-button" onClick={onOpen}><ImageIcon size={14} /> Review creative</button>
    </div>
  </article>
}

export function CreativeGalleryPage({ onOpenQueue }: { onOpenQueue: () => void }) {
  const [proposals, setProposals] = useState<PinProposal[]>([])
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await getProposals('REVIEW')
      setProposals(response.items)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not load creative gallery.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const rendered = useMemo(() => proposals.filter((proposal) => proposal.creative?.status === 'RENDERED' && proposal.creative.image_url), [proposals])
  const options = useMemo(() => {
    const values: Record<FilterKey, string[]> = { template: [], category: [], audience: [], designer: [], arabian: [], niche: [] }
    rendered.forEach((proposal) => {
      const valuesForProposal: Record<FilterKey, string | null> = {
        template: proposal.creative_template_key,
        category: fact(proposal, 'normalization_category'),
        audience: fact(proposal, 'audience'),
        designer: fact(proposal, 'designer'),
        arabian: fact(proposal, 'arabian'),
        niche: fact(proposal, 'niche'),
      }
      Object.entries(valuesForProposal).forEach(([key, value]) => {
        if (value && !values[key as FilterKey].includes(value)) values[key as FilterKey].push(value)
      })
    })
    Object.values(values).forEach((list) => list.sort())
    return values
  }, [rendered])

  const visible = useMemo(() => rendered.filter((proposal) => FILTERS.every(({ key }) => {
    if (filters[key] === 'all') return true
    const value = key === 'template' ? proposal.creative_template_key : fact(proposal, key === 'category' ? 'normalization_category' : key)
    return value === filters[key]
  })), [filters, rendered])

  function setFilter(key: FilterKey, value: string) {
    setFilters((current) => ({ ...current, [key]: value }))
    setSelectedIndex(null)
  }

  function openPreview(index: number) {
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setSelectedIndex(index)
  }

  const heading = <header className="page-heading gallery-heading">
    <div><p className="eyebrow">CONTENT LIBRARY / CREATIVE REVIEW</p><h2>Content Library</h2><p>Inspect authentic catalog sources beside Pinterest internal-preview creatives prepared for human review.</p></div>
    <div className="gallery-heading-actions"><span className="proposal-safety"><ShieldAlert size={15} /> Publishing disabled</span><button className="secondary-action" onClick={onOpenQueue}>Open Approval Queue <ChevronRight size={15} /></button></div>
  </header>

  if (loading) return <div className="gallery-page">{heading}<div className="gallery-empty"><div className="gallery-loading-grid">{[1, 2, 3].map((value) => <span key={value} />)}</div><strong>Loading rendered creatives</strong><p>Fetching the existing review records…</p></div></div>

  if (error) return <div className="gallery-page">{heading}<div className="gallery-empty gallery-error" role="alert"><ShieldAlert size={32} /><strong>Content library could not load</strong><p>{error}</p><button className="secondary-action" onClick={() => void load()}>Try again</button></div></div>

  return <div className="gallery-page">
    {heading}
    <section className="gallery-summary">
      <div><span>Rendered creatives</span><strong>{rendered.length}</strong><small>of {proposals.length} REVIEW proposals</small></div>
      <div><span>Current view</span><strong>{visible.length}</strong><small>{visible.length === 1 ? 'creative matches' : 'creatives match filters'}</small></div>
      <div><span>Review state</span><strong>{rendered.filter((proposal) => proposal.approval_status === 'REVIEW').length}</strong><small>awaiting explicit approval</small></div>
      <div className="gallery-summary-note"><Images size={20} /><span>Read-only gallery. Reviewing never approves or publishes.</span></div>
    </section>
    <section className="gallery-filters" aria-label="Content library filters">
      <button className={Object.values(filters).every((value) => value === 'all') ? 'gallery-filter active' : 'gallery-filter'} onClick={() => setFilters(EMPTY_FILTERS)}>All <span>{rendered.length}</span></button>
      {FILTERS.map(({ key, label }) => <label key={key}>{label}<select value={filters[key]} disabled={options[key].length === 0} onChange={(event) => setFilter(key, event.target.value)}><option value="all">{options[key].length === 0 ? `No ${label.toLowerCase()} facts` : `All ${label.toLowerCase()}s`}</option>{options[key].map((value) => <option value={value} key={value}>{value.replaceAll('_', ' ')}</option>)}</select></label>)}
    </section>
    {visible.length === 0 ? <div className="gallery-empty"><Images size={32} /><strong>{rendered.length === 0 ? 'No rendered creatives yet' : 'No creatives match these filters'}</strong><p>{rendered.length === 0 ? 'Rendered previews will appear here when available.' : 'Try All or clear one of the fact filters.'}</p></div>
      : <section className="gallery-grid">{visible.map((proposal, index) => <GalleryCard key={proposal.id} proposal={proposal} onOpen={() => openPreview(index)} />)}</section>}
    {selectedIndex !== null && <CreativeModal proposals={visible} index={selectedIndex} onClose={() => setSelectedIndex(null)} onMove={setSelectedIndex} returnFocus={returnFocusRef.current} />}
  </div>
}