import { useEffect, useMemo, useState } from 'react'
import { Check, Clapperboard, GitCompare, ImagePlus, Layers3, RefreshCw, ShieldAlert, Sparkles } from 'lucide-react'

import {
  AISettings,
  ContentVersion,
  PinProposal,
  proposalVersionPreviewUrl,
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

function readable(value?: string | null) {
  return value ? value.replaceAll('_', ' ') : 'Not available'
}

function money(value?: number | null) {
  return value == null ? 'Not reported' : `$${value.toFixed(8)}`
}

function CopyFields({ version }: { version: ContentVersion }) {
  return <dl className="version-copy-fields">
    <div><dt>Headline</dt><dd>{version.headline}</dd></div>
    <div><dt>Pin title</dt><dd>{version.title}</dd></div>
    <div><dt>Description</dt><dd>{version.description}</dd></div>
    <div><dt>Alt text</dt><dd>{version.alt_text}</dd></div>
    <div><dt>CTA</dt><dd>{version.cta}</dd></div>
  </dl>
}

function VersionMetadata({ version }: { version: ContentVersion }) {
  const telemetry = version.telemetry
  const validation = version.unsupported_claims.length === 0
    ? 'Passed · no unsupported claims'
    : `Review required · ${version.unsupported_claims.length} unsupported claim(s)`
  return <div className="version-metadata">
    <div><span>Generation source</span><strong>{telemetry?.provider || readable(version.provider_mode)}</strong></div>
    <div><span>Model</span><strong>{telemetry?.model || 'No AI model'}</strong></div>
    <div><span>Result</span><strong>{readable(version.generation_mode)}</strong></div>
    <div><span>Validation</span><strong>{validation}</strong></div>
    <div><span>Warnings</span><strong>{version.warnings.length || 'None'}</strong></div>
    <div><span>Missing facts</span><strong>{version.missing_facts.length || 'None'}</strong></div>
    <div><span>Text fingerprint</span><strong className="break-anywhere">{version.text_fingerprint}</strong></div>
    <div><span>Created</span><strong>{version.created_at ? new Date(version.created_at).toLocaleString() : 'Original persisted copy'}</strong></div>
    <div><span>Estimated cost</span><strong>{money(telemetry?.estimated_cost_usd ?? version.estimated_cost_usd)}</strong></div>
    <div><span>Actual cost</span><strong>{money(telemetry?.actual_cost_usd ?? version.actual_cost_usd)}</strong></div>
    <div><span>Telemetry</span><strong>{telemetry?.id || 'None'}</strong></div>
    <div><span>Tokens</span><strong>{telemetry?.total_tokens == null ? 'Not reported' : `${telemetry.total_tokens} total (${telemetry.prompt_tokens || 0} prompt / ${telemetry.completion_tokens || 0} completion)`}</strong></div>
    <div><span>Latency</span><strong>{telemetry ? `${telemetry.latency_ms} ms` : 'Not reported'}</strong></div>
    <div><span>Provider outcome</span><strong>{telemetry ? `${telemetry.success ? 'Success' : `Failed · ${readable(telemetry.failure_code)}`}${telemetry.fallback_used ? ' · fallback used' : ''}` : 'Not applicable'}</strong></div>
  </div>
}


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
  const original = versions.find((version) => version.kind === 'ORIGINAL') || versions[0]

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
    {versions.length > 1 ? <>
    <div className="version-picker">
      <label><GitCompare size={14} />Compare version
        <select value={compareId} onChange={(event) => setCompareId(event.target.value)}>
          {versions.map((version) => <option key={version.id || 'original'} value={version.id || 'original'}>
            v{version.version} · {version.kind.toLowerCase()}{version.active ? ' · active' : ''}
          </option>)}
        </select>
      </label>
      <span>Choose a persisted version to inspect. Comparing does not select, approve, or publish it.</span>
    </div>
    {compared && original && <>
      <div className="version-preview-panel">
        <div className="version-preview-heading">
          <div><p className="eyebrow">SELECTED VERSION PREVIEW</p><strong>v{compared.version} · {compared.kind.toLowerCase()}</strong></div>
          <span>Read-only · deterministic renderer · authentic Shopify image</span>
        </div>
        <div className="version-preview-grid">
          <div className="version-source-proof">
            <div className="creative-label">Authentic source <span>Shopify catalog</span></div>
            <img src={proposal.image_url} alt={`Authentic Shopify source for ${proposal.product_title}`} />
          </div>
          <div className="version-render-proof">
            <div className="creative-label">Selected copy preview <span>not persisted</span></div>
            <img loading="lazy" key={compareId} src={proposalVersionPreviewUrl(proposal.id, compared.id)} alt={`Deterministic Pinterest preview for version ${compared.version}`} />
          </div>
        </div>
        <p className="revision-safety"><ShieldAlert size={13} />This preview changes with the comparison control. It creates no AI image, creative asset, selection, approval, or publication.</p>
      </div>
      <div className="version-copy-comparison">
        <section>
          <div className="version-column-heading"><span>Baseline</span><strong>v{original.version} · original{original.active ? ' · active' : ''}</strong></div>
          <CopyFields version={original} />
          <VersionMetadata version={original} />
        </section>
        <section className={compared.id === original.id ? 'same-version' : ''}>
          <div className="version-column-heading"><span>Selected for comparison</span><strong>v{compared.version} · {compared.kind.toLowerCase()}{compared.active ? ' · active' : ''}</strong></div>
          <CopyFields version={compared} />
          <VersionMetadata version={compared} />
          {compared.video_spec && <pre className="video-spec-preview">{JSON.stringify(compared.video_spec, null, 2)}</pre>}
          {compared.content_payload && <pre className="video-spec-preview">{JSON.stringify(compared.content_payload, null, 2)}</pre>}
        </section>
      </div>
      <div className="version-activation">
        <div><strong>Version activation is a separate review action</strong><span>Activating copy does not approve it and publishing remains disabled.</span></div>
        <button className="select-version" onClick={() => void selectVersion()} disabled={working !== null || compared.active}>
          <Check size={14} />{compared.active ? 'Active version' : 'Select this version'}
        </button>
      </div>
    </>} </>
    : <p className="revision-empty">No copy revisions yet. The persisted original remains active.</p>}
    {message && <p className={`revision-message ${message.kind}`} role="status">{message.text}</p>}
  </section>
}