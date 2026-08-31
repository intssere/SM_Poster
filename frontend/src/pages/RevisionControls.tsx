import { useEffect, useMemo, useState } from 'react'
import { Check, Clapperboard, GitCompare, ImagePlus, Layers3, RefreshCw, ShieldAlert, Sparkles } from 'lucide-react'

import {
  AISettings,
  ContentVersion,
  PinProposal,
  regenerateProposal,
  selectProposalVersion,
} from '../api/proposals'


const TEMPLATES = [
  ['luxury_product_spotlight', 'Luxury Product Spotlight'],
  ['product_classification', 'Product + Classification'],
  ['gift_guide_gift_set', 'Gift Guide / Gift Set'],
  ['editorial_product_pick', 'Editorial Product Pick'],
] as const

const BACKGROUND_STYLES = [
  ['quiet_luxury', 'Quiet luxury'],
  ['modern_gradient', 'Modern gradient'],
  ['botanical_editorial', 'Botanical editorial'],
  ['bold_color_block', 'Bold color block'],
] as const

const CHANNELS = [
  ['pinterest', 'Pinterest'],
  ['instagram', 'Instagram'],
  ['facebook', 'Facebook'],
  ['tiktok', 'TikTok'],
  ['youtube_shorts', 'YouTube Shorts'],
] as const


export function RevisionControls({
  proposal,
  settings,
  onChanged,
  compact = false,
}: {
  proposal: PinProposal
  settings: AISettings | null
  onChanged: () => Promise<void>
  compact?: boolean
}) {
  const versions = proposal.versions || []
  const active = versions.find((version) => version.active) || versions[0]
  const alternativeTemplate = useMemo(
    () => TEMPLATES.find(([key]) => key !== (active?.creative_template_key || proposal.creative_template_key))?.[0] || TEMPLATES[0][0],
    [active?.creative_template_key, proposal.creative_template_key],
  )
  const [template, setTemplate] = useState<string>(alternativeTemplate)
  const [backgroundStyle, setBackgroundStyle] = useState<string>(BACKGROUND_STYLES[0][0])
  const [channel, setChannel] = useState<(typeof CHANNELS)[number][0]>('pinterest')
  const [variantCount, setVariantCount] = useState(2)
  const [compareId, setCompareId] = useState<string>('original')
  const [working, setWorking] = useState<'copy' | 'creative' | 'content_variant' | 'image_background' | 'video_script' | 'storyboard' | 'select' | null>(null)
  const [message, setMessage] = useState<{ kind: 'error' | 'success'; text: string } | null>(null)

  useEffect(() => {
    setTemplate(alternativeTemplate)
    setCompareId(active?.id || 'original')
  }, [active?.id, alternativeTemplate, proposal.id])

  const compared = versions.find((version) => (version.id || 'original') === compareId) || active

  async function regenerate(kind: 'copy' | 'creative' | 'content_variant' | 'image_background' | 'video_script' | 'storyboard') {
    setWorking(kind)
    setMessage(null)
    try {
      const result = await regenerateProposal(proposal.id, kind, {
        templateKey: kind === 'creative' ? template : undefined,
        styleKey: kind === 'image_background' ? backgroundStyle : undefined,
        channel,
        count: kind === 'creative' ? 1 : variantCount,
      })
      const revisions = 'variants' in result ? result.variants : [result]
      const revision = revisions[revisions.length - 1]
      setMessage({
        kind: 'success',
        text: `${revisions.length} immutable ${revisions.length === 1 ? 'variant' : 'variants'} created in REVIEW through version ${revision.version}. Select one explicitly to make it active.${revision.actual_cost_usd != null ? ` Latest provider cost $${revision.actual_cost_usd.toFixed(6)}.` : revision.estimated_cost_usd != null ? ` Latest estimated provider cost $${revision.estimated_cost_usd.toFixed(6)}.` : ' No paid provider cost.'}`,
      })
      await onChanged()
      setCompareId(revision.id || 'original')
    } catch (error) {
      setMessage({ kind: 'error', text: (error as Error).message })
    } finally {
      setWorking(null)
    }
  }

  async function selectVersion() {
    if (!compared) return
    setWorking('select')
    setMessage(null)
    try {
      await selectProposalVersion(proposal.id, compared.id || 'original')
      await onChanged()
      setMessage({ kind: 'success', text: `Version ${compared.version} is now active. Approval and publishing were not changed.` })
    } catch (error) {
      setMessage({ kind: 'error', text: (error as Error).message })
    } finally {
      setWorking(null)
    }
  }

  return <section className={compact ? 'revision-controls compact' : 'revision-controls'}>
    <div className="revision-control-heading">
      <div><p className="eyebrow">VERSION WORKSPACE</p><strong>Active version {proposal.active_version || 1}</strong></div>
      <span className="revision-provider"><Sparkles size={13} />{settings?.provider_label || 'Loading provider status'}</span>
    </div>
    <p className="revision-safety"><ShieldAlert size={13} />AI is optional. Disabled mode uses a deterministic fact-safe fallback; every result stays in review.</p>
    <div className="revision-actions">
      <label>Variants
        <select value={variantCount} onChange={(event) => setVariantCount(Number(event.target.value))} disabled={working !== null}>
          {[1, 2, 3, 4].map((count) => <option key={count} value={count}>{count}</option>)}
        </select>
      </label>
      <label>Channel
        <select value={channel} onChange={(event) => setChannel(event.target.value as typeof channel)} disabled={working !== null}>
          {CHANNELS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
      </label>
      <button onClick={() => void regenerate('copy')} disabled={working !== null}>
        <RefreshCw size={14} className={working === 'copy' ? 'spin' : ''} />
        {working === 'copy' ? 'Creating copy' : 'Regenerate copy'}
      </button>
      <label>Template
        <select value={template} onChange={(event) => setTemplate(event.target.value)} disabled={working !== null}>
          {TEMPLATES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
      </label>
      <button onClick={() => void regenerate('creative')} disabled={working !== null || template === (active?.creative_template_key || proposal.creative_template_key)}>
        <ImagePlus size={14} className={working === 'creative' ? 'spin' : ''} />
        {working === 'creative' ? 'Rendering variant' : 'Try creative variant'}
      </button>
      <button onClick={() => void regenerate('content_variant')} disabled={working !== null}>
        <Layers3 size={14} className={working === 'content_variant' ? 'spin' : ''} />
        {working === 'content_variant' ? 'Creating content' : 'Content bundle'}
      </button>
      <label>Background
        <select value={backgroundStyle} onChange={(event) => setBackgroundStyle(event.target.value)} disabled={working !== null}>
          {BACKGROUND_STYLES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
      </label>
      <button onClick={() => void regenerate('image_background')} disabled={working !== null || !settings?.decorative_backgrounds_enabled || settings?.effective_mode !== 'hosted_paid'}>
        <ImagePlus size={14} className={working === 'image_background' ? 'spin' : ''} />
        {working === 'image_background' ? 'Generating background' : 'Background variant'}
      </button>
      <button onClick={() => void regenerate('video_script')} disabled={working !== null}>
        <Clapperboard size={14} className={working === 'video_script' ? 'spin' : ''} />
        {working === 'video_script' ? 'Writing script' : 'Video script'}
      </button>
      <button onClick={() => void regenerate('storyboard')} disabled={working !== null}>
        <Clapperboard size={14} className={working === 'storyboard' ? 'spin' : ''} />
        {working === 'storyboard' ? 'Planning scenes' : 'Storyboard'}
      </button>
    </div>
    <div className="version-compare">
      <label><GitCompare size={14} />Compare version
        <select value={compareId} onChange={(event) => setCompareId(event.target.value)}>
          {versions.map((version) => <option key={version.id || 'original'} value={version.id || 'original'}>
            v{version.version} · {version.kind.toLowerCase()}{version.active ? ' · active' : ''}
          </option>)}
        </select>
      </label>
      {compared && <div className="version-snapshot">
        <div><span>Title</span><strong>{compared.title}</strong></div>
        {!compact && <div><span>Description</span><p>{compared.description}</p></div>}
        <small>{compared.generation_mode.replaceAll('_', ' ')} · {compared.creative_template}{compared.actual_cost_usd != null ? ` · $${compared.actual_cost_usd.toFixed(6)}` : compared.estimated_cost_usd != null ? ` · estimated $${compared.estimated_cost_usd.toFixed(6)}` : ''}</small>
        {compared.intended_channel && <small>{compared.generation_type?.replaceAll('_', ' ')} · intended for {compared.intended_channel.replaceAll('_', ' ')}</small>}
        {compared.creative?.image_url && compared.generation_type === 'image_background' && <img className="version-creative-preview" src={compared.creative.image_url} alt="Generated background variant with authentic Shopify product image" />}
        {compared.video_spec && <pre className="video-spec-preview">{JSON.stringify(compared.video_spec, null, 2)}</pre>}
        {compared.content_payload && <pre className="video-spec-preview">{JSON.stringify(compared.content_payload, null, 2)}</pre>}
      </div>}
      <button className="select-version" onClick={() => void selectVersion()} disabled={working !== null || !compared || compared.active}>
        <Check size={14} />{compared?.active ? 'Active version' : 'Select this version'}
      </button>
    </div>
    {message && <p className={`revision-message ${message.kind}`} role="status">{message.text}</p>}
  </section>
}