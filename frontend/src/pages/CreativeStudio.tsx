import { ArrowRight, Images, Radio, ShieldAlert } from 'lucide-react'

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
      <div><p className="eyebrow">SOCIAL STUDIO / WORKSPACE</p><h2>Creative Studio</h2><p>Prepare channel-ready content from trusted catalog facts, then review it before any future distribution.</p></div>
      <span className="proposal-safety"><ShieldAlert size={15} /> Publishing disabled</span>
    </header>
    <section className="studio-hero"><div><p className="eyebrow">PLATFORM-NEUTRAL FOUNDATION</p><h3>Content stays portable until a channel is ready.</h3><p>The current Pinterest preview flow remains intact while channel definitions keep content, account, and media requirements separate.</p></div><div className="studio-hero-mark"><Radio size={24} /><span>1 active preview<br /><b>5 future channels</b></span></div></section>
    <section className="studio-actions">
      <button onClick={onOpenLibrary}><Images size={18} /><span><strong>Content Library</strong><small>{creativeCount ?? '—'} rendered creatives available</small></span><ArrowRight size={16} /></button>
      <button onClick={onOpenQueue}><Images size={18} /><span><strong>Approval Queue</strong><small>{reviewCount} proposals awaiting review</small></span><ArrowRight size={16} /></button>
      <button onClick={onOpenChannels}><Radio size={18} /><span><strong>Channels</strong><small>Pinterest internal preview; future channels not connected</small></span><ArrowRight size={16} /></button>
    </section>
    <section className="panel studio-safety"><div><p className="eyebrow">CURRENT OPERATING BOUNDARY</p><h3>Review first. Connect later.</h3><p>This foundation adds no OAuth, scheduler, analytics, publishing, background workers, or AI generation. Existing Pinterest records and fingerprints remain the source of truth.</p></div><span className="studio-boundary">SHOPIFY → CONTENT → REVIEW → APPROVAL</span></section>
  </div>
}