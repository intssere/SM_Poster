import { useCallback, useEffect, useState } from 'react'
import { Check, Clipboard, ExternalLink, ImageOff, Images, RefreshCw, ShieldAlert, X, TriangleAlert, Ruler, Fingerprint, Clock3 } from 'lucide-react'

import {
  decideProposal,
  generateProposals,
  getCreativeQa,
  getProposalQa,
  getProposals,
  PinProposal,
  ProposalReport,
  renderCreatives,
} from '../api/proposals'

function formatDate(value?: string | null) {
  if (!value) return 'Just now'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function statusClass(value: string) {
  return value.toLowerCase().replaceAll('_', '-')
}

function factEntries(proposal: PinProposal) {
  return Object.entries(proposal.intelligence_facts_used).filter(([key]) => key !== 'angle_supporting_fields')
}

function pretty(value: unknown) {
  if (value === null || value === undefined || value === '') return 'Not supplied'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function CreativeStatus({ proposal }: { proposal: PinProposal }) {
  const creative = proposal.creative
  if (!creative) return <span className="creative-status creative-status-pending">Not rendered</span>
  const status = creative.status.toLowerCase()
  const failed = status === 'failed' || Boolean(creative.error)
  return <span className={`creative-status creative-status-${failed ? 'failed' : status}`}><span className="status-dot" />{failed ? 'Render failed' : creative.status}</span>
}

function CreativeComparison({ proposal }: { proposal: PinProposal }) {
  const creative = proposal.creative
  const specification = creative?.specification || {}
  const imageSpec = typeof specification.image === 'object' && specification.image
    ? specification.image as Record<string, unknown>
    : {}
  const provenanceKeys = ['id', 'shopify_media_id', 'provenance_url', 'checksum_sha256']
  const provenance = provenanceKeys.filter((key) => imageSpec[key] !== undefined)
  return <div className="creative-review">
    <div className="creative-review-heading">
      <div><p className="eyebrow">CREATIVE PROOF</p><h4>Source → Pinterest render</h4></div>
      <CreativeStatus proposal={proposal} />
    </div>
    <div className="creative-images">
      <div className="creative-frame">
        <div className="creative-label">Authentic source <span>catalog</span></div>
        <div className="creative-image"><img src={proposal.image_url} alt={`Authentic source for ${proposal.product_title}`} /></div>
      </div>
      <div className="creative-frame">
        <div className="creative-label">Rendered preview <span>2:3</span></div>
        <div className="creative-image">
          {creative?.image_url ? <img src={creative.image_url} alt={`Rendered Pinterest preview for ${proposal.product_title}`} /> : <div className="creative-empty"><ImageOff size={22} /><span>{creative?.error ? 'Preview unavailable' : 'Awaiting render'}</span></div>}
        </div>
      </div>
    </div>
    {creative?.error && <div className="creative-error"><TriangleAlert size={15} /><span>{creative.error}</span></div>}
    {creative && <div className="creative-metadata">
      <span><Ruler size={13} />{creative.width && creative.height ? `${creative.width} × ${creative.height}px` : 'Dimensions pending'}</span>
      <span>Template {pretty(creative.template_version || proposal.creative_template)}</span>
      {creative.duration_ms != null && <span><Clock3 size={13} />{creative.duration_ms}ms</span>}
      {creative.size_bytes != null && <span>{Math.round(creative.size_bytes / 1024)} KB</span>}
    </div>}
    {(creative?.creative_fingerprint || creative?.sha256) && <div className="creative-identifiers">
      <Fingerprint size={14} /><span>Creative fingerprint <b>{creative.creative_fingerprint || '—'}</b></span>
      {creative.sha256 && <span>SHA-256 <b>{creative.sha256}</b></span>}
    </div>}
    {provenance.length > 0 && <div className="creative-provenance"><strong>Source provenance</strong>{provenance.map((key) => <span key={key}><b>{key.replaceAll('_', ' ')}</b> {pretty(imageSpec[key])}</span>)}</div>}
  </div>
}

export function ProposalsPage({ onOpenGallery }: { onOpenGallery?: () => void }) {
  const [proposals, setProposals] = useState<PinProposal[]>([])
  const [qa, setQa] = useState<ProposalReport | null>(null)
  const [creativeQa, setCreativeQa] = useState<Record<string, unknown> | null>(null)
  const [status, setStatus] = useState('REVIEW')
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [rendering, setRendering] = useState(false)
  const [workingId, setWorkingId] = useState<string | null>(null)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [queue, report, creativeReport] = await Promise.all([getProposals(status), getProposalQa(), getCreativeQa()])
      setProposals([...queue.items].sort((a, b) => Number(Boolean(b.creative)) - Number(Boolean(a.creative))))
      setQa(report)
      setCreativeQa(creativeReport)
      setMessage(null)
    } catch (error) {
      setMessage({ type: 'error', text: (error as Error).message })
    } finally { setLoading(false) }
  }, [status])

  useEffect(() => { void load() }, [load])

  async function generate() {
    setGenerating(true); setMessage(null)
    try {
      const report = await generateProposals({ dry_run: true })
      setQa(report)
      setMessage({ type: 'success', text: `${report.proposals_generated} proposals evaluated for ${report.products_selected} products. Dry run made ${report.mutations_performed} database changes.` })
    } catch (error) { setMessage({ type: 'error', text: (error as Error).message }) }
    finally { setGenerating(false) }
  }

  async function render() {
    setRendering(true); setMessage(null)
    try {
      await renderCreatives(12)
      await load()
      setMessage({ type: 'success', text: 'Render batch complete. Creative previews refreshed; no approval state was changed.' })
    } catch (error) { setMessage({ type: 'error', text: `Creative render failed: ${(error as Error).message}` }) }
    finally { setRendering(false) }
  }

  async function decide(id: string, decision: 'approve' | 'reject') {
    setWorkingId(id); setMessage(null)
    try { await decideProposal(id, decision); await load() }
    catch (error) { setMessage({ type: 'error', text: (error as Error).message }) }
    finally { setWorkingId(null) }
  }

  return <div className="proposals-page">
    <header className="page-heading">
      <div><p className="eyebrow">PIN PROPOSALS / REVIEW</p><h2>Approval Queue</h2><p>Compare authentic catalog sources with rendered Pinterest output before making an explicit decision.</p></div>
      <div className="queue-actions">
        {onOpenGallery && <button className="secondary-action" onClick={onOpenGallery}><Images size={16} />View Content Library</button>}
        <button className="secondary-action" onClick={render} disabled={rendering}><RefreshCw size={16} className={rendering ? 'spin' : ''} />{rendering ? 'Rendering 12 previews' : 'Render 12 previews'}</button>
        <button className="primary-action" onClick={generate} disabled={generating}><RefreshCw size={16} className={generating ? 'spin' : ''} />{generating ? 'Evaluating ranked sample' : 'Run ranking dry run'}</button>
      </div>
    </header>
    <section className="proposal-toolbar"><div className="proposal-status-tabs">{['REVIEW', 'APPROVED', 'REJECTED'].map((value) => <button key={value} className={status === value ? 'active' : ''} onClick={() => setStatus(value)}>{value}</button>)}</div><span className="proposal-safety"><ShieldAlert size={15} /> Publishing disabled — approval never publishes</span></section>
    {message && <p className={message.type === 'error' ? 'error-message' : 'catalog-message'} role="status">{message.text}</p>}
    {creativeQa && <div className="creative-qa-strip"><span className="eyebrow">RENDER QA</span>{Object.entries(creativeQa).slice(0, 4).map(([key, value]) => <span key={key}><b>{key.replaceAll('_', ' ')}</b> {pretty(value)}</span>)}</div>}
    {qa && <section className="proposal-summary"><div><span>Products selected</span><strong>{qa.products_selected}</strong></div><div><span>Proposals</span><strong>{qa.proposals_generated}</strong></div><div><span>Partial records</span><strong>{qa.proposals_using_partial_records}</strong></div><div><span>Duplicates blocked</span><strong>{qa.duplicate_attempts_prevented}</strong></div></section>}
    {qa?.dry_run && <section className="panel diversity-diagnostics"><div><p className="eyebrow">DRY-RUN DIVERSITY QA</p><h3>Angle ranking diagnostics</h3><p>No concepts, drafts, approvals, fingerprints, or publication records were created.</p></div><div className="diversity-metrics"><span>Maximum angle share <strong>{Math.round(qa.maximum_angle_share.share * 100)}%</strong> {qa.maximum_angle_share.angle || 'None'}</span>{Object.entries(qa.classification_angle_coverage).map(([label, values]) => <span key={label}>{label} <strong>{values.selected}/{values.available_candidates}</strong> selected</span>)}</div><div className="diversity-distribution">{Object.entries(qa.content_angle_distribution).map(([angle, count]) => <span key={angle}>{angle}: <strong>{count}</strong></span>)}</div></section>}
    {loading ? <div className="empty-catalog loading-state"><span className="skeleton-line" /><span className="skeleton-line short" /><span className="skeleton-block" /></div> : proposals.length === 0 ? <div className="empty-catalog"><ImageOff size={32} /><strong>No {status.toLowerCase()} proposals</strong><p>Generate the controlled batch to create fact-safe proposals for human review.</p></div> : <section className="proposal-list">{proposals.map((proposal) => <article className="proposal-card" key={proposal.id}>
      <div className="proposal-image-wrap">{proposal.image_url ? <img src={proposal.image_url} alt={proposal.product_title} /> : <ImageOff size={28} />}<span className={`badge ${statusClass(proposal.approval_status)}`}>{proposal.approval_status}</span></div>
      <div className="proposal-content"><div className="proposal-card-heading"><div><p className="eyebrow">{proposal.content_angle}</p><h3>{proposal.product_title}</h3><small>{proposal.vendor || 'Unknown brand'} · {proposal.creative_template}</small></div><a href={proposal.canonical_url} target="_blank" rel="noreferrer" aria-label="Open canonical product page"><ExternalLink size={17} /></a></div>
        <CreativeComparison proposal={proposal} /><p className="proposal-headline">{proposal.headline}</p>
        <dl className="proposal-details"><div><dt>Pin title</dt><dd>{proposal.title}</dd></div><div><dt>Description</dt><dd>{proposal.description}</dd></div><div><dt>Keywords</dt><dd>{proposal.keywords.join(' · ')}</dd></div><div><dt>Board</dt><dd>{proposal.intended_board.name}</dd></div><div><dt>Tracked URL</dt><dd className="break-anywhere">{proposal.utm_url}</dd></div><div><dt>Created</dt><dd>{formatDate(proposal.created_at)}</dd></div></dl>
        <div className="proposal-facts"><strong>Facts used</strong>{factEntries(proposal).map(([key, value]) => <span key={key}>{key.replaceAll('_', ' ')}: {String(value)}</span>)}</div>
        {(proposal.warnings.length > 0 || proposal.missing_facts.length > 0 || proposal.unsupported_claims.length > 0) && <div className="proposal-warning"><ShieldAlert size={15} /><span>{proposal.warnings.join(' ')}{proposal.missing_facts.length > 0 && ` Missing/unknown: ${proposal.missing_facts.join(', ')}.`}{proposal.unsupported_claims.length > 0 && ` Unsupported claims detected: ${proposal.unsupported_claims.join(', ')}.`}</span></div>}
        <div className="proposal-fingerprint"><Clipboard size={14} /><span>Text SHA-256 {proposal.text_fingerprint}</span></div>
        {proposal.approval_status === 'REVIEW' && <div className="proposal-actions"><button className="approve-action" onClick={() => decide(proposal.id, 'approve')} disabled={workingId === proposal.id}><Check size={15} /> Approve</button><button className="reject-action" onClick={() => decide(proposal.id, 'reject')} disabled={workingId === proposal.id}><X size={15} /> Reject</button></div>}
      </div>
    </article>)}</section>}
  </div>
}