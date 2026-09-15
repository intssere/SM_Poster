import { useCallback, useEffect, useMemo, useState } from 'react'
import { Check, ImageOff, Layers3, RefreshCw, ShieldCheck, Sparkles, X } from 'lucide-react'
import { decideProposal, getProposals, PinProposal, regenerateProposal } from '../api/proposals'
import { listPublications, PublicationListItem } from '../api/publications'
import { Alert, Button, EmptyState, PageHeader, StatusBadge, Surface } from '../ui/primitives'
import {
  SOCIAL_CHANNELS,
  SocialChannel,
  SocialChannelFilter,
  channelLabel,
  contentChannel,
  contentChannels,
  generationChannel,
  matchesChannel,
} from '../ui/channelModel'

type LegacyContentView = 'library' | 'review'
type Lifecycle = 'all' | 'drafts' | 'review' | 'approved' | 'published'

const LIFECYCLES: ReadonlyArray<{ key: Lifecycle; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'drafts', label: 'Drafts' },
  { key: 'review', label: 'Needs review' },
  { key: 'approved', label: 'Approved' },
  { key: 'published', label: 'Published' },
]

function proposalStatus(lifecycle: Lifecycle): string | undefined {
  if (lifecycle === 'drafts') return 'GENERATED'
  if (lifecycle === 'review') return 'REVIEW'
  if (lifecycle === 'approved') return 'APPROVED'
  return undefined
}

function lifecycleDescription(lifecycle: Lifecycle): string {
  if (lifecycle === 'drafts') return 'Generated content that has not yet entered the approval path.'
  if (lifecycle === 'review') return 'Review channel variants before any approval or distribution decision.'
  if (lifecycle === 'approved') return 'Approved content records. Publishing remains channel-specific and separately controlled.'
  if (lifecycle === 'published') return 'Confirmed publication records. Pinterest is currently the only implemented publishing channel.'
  return 'Browse content across the lifecycle and filter independently by intended social channel.'
}

function ProposalCard({
  proposal,
  lifecycle,
  variantTarget,
  working,
  onGenerateVariant,
  onGenerateVideo,
  onApprove,
  onReject,
}: {
  proposal: PinProposal
  lifecycle: Lifecycle
  variantTarget: SocialChannel
  working: boolean
  onGenerateVariant: () => void
  onGenerateVideo: () => void
  onApprove: () => void
  onReject: () => void
}) {
  const channels = contentChannels(proposal.versions)
  const activeChannel = contentChannel(proposal.versions)
  const media = proposal.creative?.image_url || proposal.image_url
  const canApproveForPinterest = activeChannel === 'pinterest'
  const isVideoTarget = variantTarget === 'tiktok' || variantTarget === 'youtube'

  return <article className="ds-content-card">
    <div className="ds-content-card__media">
      {media ? <img src={media} alt={proposal.product_title} /> : <ImageOff size={28} />}
      <StatusBadge status={proposal.approval_status} />
    </div>
    <div className="ds-content-card__body">
      <div className="ds-content-card__heading">
        <div><p className="ds-eyebrow">{proposal.content_angle}</p><h3>{proposal.product_title}</h3><small>{proposal.vendor || 'Unknown brand'} · active: {channelLabel(activeChannel)}</small></div>
      </div>
      <div className="ds-channel-tags" aria-label="Persisted channel variants">
        {channels.map((channel) => <span key={channel}>{channelLabel(channel)}</span>)}
      </div>
      <p className="ds-content-card__copy">{proposal.headline || proposal.description}</p>
      <div className="ds-content-card__meta"><span>{proposal.versions?.length || 1} version{(proposal.versions?.length || 1) === 1 ? '' : 's'}</span><span>{proposal.creative?.status || 'Creative pending'}</span><span>{proposal.approval_status.replaceAll('_', ' ')}</span></div>

      {lifecycle === 'review' ? <div className="ds-review-actions">
        <Button variant="ghost" onClick={onGenerateVariant} disabled={working}><Layers3 size={14} />{working ? 'Working…' : `Create ${channelLabel(variantTarget)} variant`}</Button>
        {isVideoTarget ? <Button variant="ghost" onClick={onGenerateVideo} disabled={working}><Sparkles size={14} />Video script</Button> : null}
        <div className="ds-review-decision">
          <Button variant="primary" onClick={onApprove} disabled={working || !proposal.creative?.id || !canApproveForPinterest}><Check size={14} />Approve</Button>
          <Button variant="ghost" onClick={onReject} disabled={working}><X size={14} />Reject</Button>
        </div>
        {!canApproveForPinterest ? <small className="ds-review-boundary"><ShieldCheck size={13} />This active {channelLabel(activeChannel)} variant remains review-only because no {channelLabel(activeChannel)} publishing adapter exists.</small> : null}
      </div> : null}
    </div>
  </article>
}

function PublicationCard({ publication }: { publication: PublicationListItem }) {
  return <article className="ds-content-card ds-content-card--publication">
    <div className="ds-content-card__media">
      {publication.media_url ? <img src={publication.media_url} alt={publication.alt_text || publication.title || 'Published Pinterest content'} /> : <ImageOff size={28} />}
      <StatusBadge status={publication.status} />
    </div>
    <div className="ds-content-card__body">
      <div><p className="ds-eyebrow">Published · Pinterest</p><h3>{publication.title || 'Published content'}</h3></div>
      <div className="ds-channel-tags"><span>Pinterest</span></div>
      <p className="ds-content-card__copy">{publication.description || 'No description snapshot available.'}</p>
      <div className="ds-content-card__meta"><span>{publication.published_at ? new Date(publication.published_at).toLocaleString() : 'Publication time unavailable'}</span>{publication.pinterest_pin_id ? <span>Provider confirmed</span> : null}</div>
    </div>
  </article>
}

export function UnifiedContentWorkspace({ initialView }: { initialView: LegacyContentView }) {
  const [lifecycle, setLifecycle] = useState<Lifecycle>(initialView === 'review' ? 'review' : 'all')
  const [channel, setChannel] = useState<SocialChannelFilter>('all')
  const [variantTarget, setVariantTarget] = useState<SocialChannel>('pinterest')
  const [proposals, setProposals] = useState<PinProposal[]>([])
  const [publications, setPublications] = useState<PublicationListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [workingId, setWorkingId] = useState<string | null>(null)

  useEffect(() => {
    setLifecycle(initialView === 'review' ? 'review' : 'all')
  }, [initialView])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const status = proposalStatus(lifecycle)
      const [proposalResult, publicationResult] = await Promise.all([
        lifecycle === 'published' ? Promise.resolve({ items: [] as PinProposal[] }) : getProposals(status),
        lifecycle === 'published' || lifecycle === 'all' ? listPublications() : Promise.resolve([] as PublicationListItem[]),
      ])
      setProposals(proposalResult.items)
      setPublications(publicationResult.filter((publication) => publication.status === 'PUBLISHED'))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not load content workspace.')
    } finally {
      setLoading(false)
    }
  }, [lifecycle])

  useEffect(() => { void load() }, [load])

  const visibleProposals = useMemo(
    () => proposals.filter((proposal) => matchesChannel(proposal.versions, channel)),
    [channel, proposals],
  )
  const visiblePublications = useMemo(
    () => publications.filter(() => channel === 'all' || channel === 'pinterest'),
    [channel, publications],
  )

  async function generate(proposal: PinProposal, kind: 'content_variant' | 'video_script') {
    setWorkingId(proposal.id)
    setMessage(null)
    try {
      await regenerateProposal(proposal.id, kind, { channel: generationChannel(variantTarget), count: 1 })
      setMessage(`${channelLabel(variantTarget)} ${kind === 'video_script' ? 'video script' : 'content variant'} created as an immutable review version. Nothing was published.`)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not create channel variant.')
    } finally {
      setWorkingId(null)
    }
  }

  async function decide(proposal: PinProposal, decision: 'approve' | 'reject') {
    setWorkingId(proposal.id)
    setMessage(null)
    try {
      await decideProposal(proposal.id, decision, decision === 'approve' ? proposal.creative?.id || undefined : undefined)
      setMessage(decision === 'approve' ? 'Pinterest-ready content approved. Approval did not publish it.' : 'Content rejected. Nothing was published.')
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not update review decision.')
    } finally {
      setWorkingId(null)
    }
  }

  const totalVisible = visibleProposals.length + (lifecycle === 'all' || lifecycle === 'published' ? visiblePublications.length : 0)

  return <div className="ds-content-workspace">
    <PageHeader eyebrow="Content" title="Multi-channel content" description={lifecycleDescription(lifecycle)} actions={<StatusBadge status="READY" label="6 channel targets" />} />

    {error ? <Alert tone="danger" title="Content workspace needs attention">{error}</Alert> : null}
    {message ? <Alert tone="success" title="Review workflow updated">{message}</Alert> : null}

    <div className="ds-content-controls">
      <div className="ds-content-tabs" role="tablist" aria-label="Content lifecycle">
        {LIFECYCLES.map((item) => <button key={item.key} type="button" role="tab" aria-selected={lifecycle === item.key} className={lifecycle === item.key ? 'is-active' : ''} onClick={() => setLifecycle(item.key)}>{item.label}</button>)}
      </div>
      <div className="ds-channel-filter" aria-label="Filter by intended channel">
        <button type="button" className={channel === 'all' ? 'is-active' : ''} onClick={() => setChannel('all')}>All channels</button>
        {SOCIAL_CHANNELS.map((item) => <button type="button" key={item.key} className={channel === item.key ? 'is-active' : ''} onClick={() => setChannel(item.key)}>{item.label}</button>)}
      </div>
    </div>

    {lifecycle === 'review' ? <Surface className="ds-variant-target">
      <div><p className="ds-eyebrow">Channel adaptation</p><strong>Create a review-only variant for</strong><small>Generation creates a persisted review version; it does not connect or publish to the selected network.</small></div>
      <select aria-label="Channel variant target" value={variantTarget} onChange={(event) => setVariantTarget(event.target.value as SocialChannel)}>{SOCIAL_CHANNELS.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select>
    </Surface> : null}

    <div className="ds-content-summary"><strong>{loading ? '—' : totalVisible.toLocaleString()}</strong><span>records in this view</span><small>{channel === 'all' ? 'Across all channel identities' : `Filtered to ${channelLabel(channel)}`}</small></div>

    {loading ? <EmptyState title="Loading content" description="Reading existing proposals, versions, and publication snapshots." /> : totalVisible === 0 ? <EmptyState title="No matching content" description="Try another lifecycle or channel filter. No provider action was performed." /> : <div className="ds-content-grid">
      {visibleProposals.map((proposal) => <ProposalCard
        key={proposal.id}
        proposal={proposal}
        lifecycle={lifecycle}
        variantTarget={variantTarget}
        working={workingId === proposal.id}
        onGenerateVariant={() => void generate(proposal, 'content_variant')}
        onGenerateVideo={() => void generate(proposal, 'video_script')}
        onApprove={() => void decide(proposal, 'approve')}
        onReject={() => void decide(proposal, 'reject')}
      />)}
      {(lifecycle === 'all' || lifecycle === 'published') ? visiblePublications.map((publication) => <PublicationCard key={publication.id} publication={publication} />) : null}
    </div>}

    <Surface className="ds-content-safety">
      <ShieldCheck size={18} />
      <p><strong>Distribution boundaries are unchanged.</strong> Pinterest is the only channel with implemented connection, scheduling, and publishing infrastructure. Instagram, Facebook, LinkedIn, TikTok, and YouTube variants stay inside generation/review until separately implemented provider adapters are authorized.</p>
    </Surface>
  </div>
}
