import { useEffect, useMemo, useState } from 'react'
import { Check, GitCompare, ImagePlus, RefreshCw, ShieldAlert, Sparkles } from 'lucide-react'

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
  const [compareId, setCompareId] = useState<string>('original')
  const [working, setWorking] = useState<'copy' | 'creative' | 'select' | null>(null)
  const [message, setMessage] = useState<{ kind: 'error' | 'success'; text: string } | null>(null)

  useEffect(() => {
    setTemplate(alternativeTemplate)
    setCompareId(active?.id || 'original')
  }, [active?.id, alternativeTemplate, proposal.id])

  const compared = versions.find((version) => (version.id || 'original') === compareId) || active

  async function regenerate(kind: 'copy' | 'creative') {
    setWorking(kind)
    setMessage(null)
    try {
      const revision = await regenerateProposal(proposal.id, kind, kind === 'creative' ? template : undefined)
      setMessage({
        kind: 'success',
        text: `Version ${revision.version} created in REVIEW. Select it explicitly to make it active.`,
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
        <small>{compared.generation_mode.replaceAll('_', ' ')} · {compared.creative_template}</small>
      </div>}
      <button className="select-version" onClick={() => void selectVersion()} disabled={working !== null || !compared || compared.active}>
        <Check size={14} />{compared?.active ? 'Active version' : 'Select this version'}
      </button>
    </div>
    {message && <p className={`revision-message ${message.kind}`} role="status">{message.text}</p>}
  </section>
}