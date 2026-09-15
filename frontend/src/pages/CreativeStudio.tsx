import { ArrowRight, Images, Radio, ShieldCheck, Sparkles } from 'lucide-react'
import { SOCIAL_CHANNELS } from '../ui/channelModel'

export function CreativeStudioPage({
  creativeCount,
  reviewCount,
  onOpenLibrary,
  onOpenChannels,
  onOpenQueue,
}: {
  creativeCount: number | null
  reviewCount: number
  onOpenLibrary: () => void
  onOpenChannels: () => void
  onOpenQueue: () => void
}) {
  return <div className="studio-page">
    <header className="page-heading">
      <div><p className="eyebrow">SOCIAL STUDIO / CREATE</p><h2>Multi-channel Creative Studio</h2><p>Start from trusted catalog facts, create review-only variants, and adapt the same product story for each social channel.</p></div>
      <span className="proposal-safety"><ShieldCheck size={15} /> Review first · channel publishing stays explicit</span>
    </header>

    <section className="studio-hero">
      <div><p className="eyebrow">CREATE ONCE → ADAPT BY CHANNEL</p><h3>One content system. Six first-class social targets.</h3><p>Pinterest, Instagram, Facebook, LinkedIn, TikTok, and YouTube share the same catalog grounding and review controls. Only channels with an implemented connection and publishing path can distribute externally.</p></div>
      <div className="studio-hero-mark"><Sparkles size={24} /><span>6 content targets<br /><b>1 review workflow</b></span></div>
    </section>

    <section className="ds-channel-strip" aria-label="Supported content channels">
      {SOCIAL_CHANNELS.map((channel) => <span key={channel.key}><Radio size={13} />{channel.label}</span>)}
    </section>

    <section className="studio-actions">
      <button onClick={onOpenLibrary}><Images size={18} /><span><strong>Content workspace</strong><small>{creativeCount ?? '—'} rendered creatives available</small></span><ArrowRight size={16} /></button>
      <button onClick={onOpenQueue}><Images size={18} /><span><strong>Needs review</strong><small>{reviewCount} content items awaiting a decision</small></span><ArrowRight size={16} /></button>
      <button onClick={onOpenChannels}><Radio size={18} /><span><strong>Channels & services</strong><small>See which channels are content-ready, connected, and publish-capable</small></span><ArrowRight size={16} /></button>
    </section>

    <section className="panel studio-safety">
      <div><p className="eyebrow">OPERATING BOUNDARY</p><h3>Generation is not distribution.</h3><p>Channel variants remain immutable review records. Pinterest keeps its existing approval, snapshot, scheduling, dispatch-authorization, no-blind-retry, and reconciliation controls. Instagram, Facebook, LinkedIn, TikTok, and YouTube gain no OAuth or publishing capability in this phase.</p></div>
      <span className="studio-boundary">SHOPIFY → CREATE → CHANNEL VARIANT → REVIEW → APPROVAL</span>
    </section>
  </div>
}
