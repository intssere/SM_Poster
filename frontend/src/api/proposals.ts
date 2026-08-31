export type PinProposal = {
  id: string
  concept_id: string
  product_id: string
  product_title: string
  vendor?: string | null
  image_url: string
  headline: string
  title: string
  description: string
  alt_text?: string
  cta: string
  canonical_url: string
  utm_url: string
  keywords: string[]
  content_angle: string
  content_angle_key: string
  creative_template: string
  creative_template_key: string
  intended_board: {
    key: string
    name: string
    pinterest_board_id?: string | null
  }
  intelligence_facts_used: Record<string, unknown>
  warnings: string[]
  missing_facts: string[]
  unsupported_claims: string[]
  duplicate_fingerprint: string
  text_fingerprint: string
  normalization_status: string
  approval_status: 'GENERATED' | 'REVIEW' | 'APPROVED' | 'REJECTED'
  variation_reason?: string | null
  created_at?: string | null
  creative: CreativePreview | null
  active_revision_id?: string | null
  active_version?: number
  versions?: ContentVersion[]
}

export type CreativePreview = {
  id: string
  status: string
  image_url?: string | null
  error?: string | null
  width?: number | null
  height?: number | null
  size_bytes?: number | null
  duration_ms?: number | null
  creative_fingerprint?: string | null
  sha256?: string | null
  template_version?: number | null
  specification?: Record<string, unknown> | null
}

export type ContentVersion = {
  id: string | null
  version: number
  kind: 'ORIGINAL' | 'COPY' | 'CREATIVE'
  status: 'REVIEW'
  parent_revision_id?: string | null
  active: boolean
  headline: string
  title: string
  description: string
  alt_text: string
  cta: string
  creative_template: string
  creative_template_key: string
  text_fingerprint: string
  creative_fingerprint?: string | null
  facts_used: Record<string, unknown>
  warnings: string[]
  missing_facts: string[]
  unsupported_claims: string[]
  provenance: Record<string, unknown>
  provider_mode: string
  generation_mode: string
  reason: string
  created_at?: string | null
  creative: CreativePreview | null
}

export type AISettings = {
  enabled: boolean
  provider_mode: 'disabled' | 'local_free' | 'hosted_paid'
  effective_mode: 'disabled' | 'local_free' | 'hosted_paid'
  provider_label: string
  available_provider_modes: Array<{ id: string; label: string; available: boolean }>
  capabilities: {
    copy_regeneration: boolean
    creative_template_variants: boolean
    decorative_backgrounds: boolean
    hosted_provider_configured: boolean
  }
  decorative_backgrounds_enabled: boolean
  credentials_configured: boolean
}

export type CreativeQa = Record<string, unknown>

export type ProposalReport = {
  products_selected: number
  eligible_products_considered: number
  not_selected_due_to_batch_limit: number
  proposals_generated: number
  duplicate_attempts_prevented: number
  content_angle_distribution: Record<string, number>
  template_distribution: Record<string, number>
  board_mapping_distribution: Record<string, number>
  normalization_distribution: Record<string, number>
  proposals_using_partial_records: number
  unsupported_claims_detected: string[]
  skipped_products: Array<{ product_id: string; title: string; reason: string }>
  representative_proposals: PinProposal[]
  candidate_angle_diagnostics: Array<{
    product_id: string
    product_title: string
    candidate_angles: Array<{
      angle_key: string
      angle: string
      score: number
      factors: Record<string, number>
      intent_group: string
      supported: boolean
      selected: boolean
      reason: string
    }>
    selected_angles: string[]
    rejected_angles: string[]
  }>
  selected_angle_details: Array<Record<string, unknown>>
  rejected_candidate_angles: Array<Record<string, unknown>>
  maximum_angle_share: { angle: string | null; count: number; share: number }
  classification_angle_coverage: Record<string, {
    available_candidates: number
    selected: number
    coverage: number | null
  }>
  sample_source: string
  dry_run: boolean
  mutations_performed: number
  publishing_enabled: boolean
}

export type ProposalSummary = {
  total: number
  review: number
  approved: number
  rejected: number
  generated: number
  scheduled: number
  publishing_enabled: boolean
}

export type ProposalFilters = {
  category?: string
  vendor?: string
  audience?: string
  designer?: string
  niche?: string
  arabian?: string
  price_band?: string
  concentration?: string
  fragrance_family?: string
  inventory_min?: number
  normalization_status?: string
  product_limit?: number
  max_proposals_per_product?: number
  dry_run?: boolean
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.detail || payload?.message || `Request failed (${response.status})`)
  }
  return response.json()
}

export async function getProposals(status?: string): Promise<{ items: PinProposal[] }> {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  return json(await fetch(`/api/pins/proposals?${params}`))
}

export async function getProposalSummary(): Promise<ProposalSummary> {
  return json(await fetch('/api/pins/summary'))
}

export async function generateProposals(filters: ProposalFilters = {}): Promise<ProposalReport> {
  return json(await fetch('/api/pins/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(filters),
  }))
}

export async function decideProposal(id: string, decision: 'approve' | 'reject', note?: string) {
  return json<{ id: string; approval_status: string; publishing_enabled: boolean }>(
    await fetch(`/api/pins/proposals/${id}/${decision}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(note ? { note } : {}),
    }),
  )
}

export async function getProposalQa(): Promise<ProposalReport> {
  return json(await fetch('/api/pins/proposals/qa'))
}

export async function renderCreatives(limit = 12): Promise<CreativeQa> {
  return json(await fetch('/api/pins/creatives/render', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ limit }),
  }))
}

export async function getCreativeQa(): Promise<CreativeQa> {
  return json(await fetch('/api/pins/creatives/qa'))
}

export async function getAISettings(): Promise<AISettings> {
  return json(await fetch('/api/ai/settings'))
}

export async function updateAISettings(
  providerMode: AISettings['provider_mode'],
): Promise<AISettings> {
  return json(await fetch('/api/ai/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      enabled: providerMode !== 'disabled',
      provider_mode: providerMode,
      decorative_backgrounds_enabled: false,
    }),
  }))
}

export async function regenerateProposal(
  id: string,
  kind: 'copy' | 'creative',
  templateKey?: string,
): Promise<ContentVersion> {
  return json(await fetch(`/api/pins/proposals/${id}/regenerate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, template_key: templateKey }),
  }))
}

export async function selectProposalVersion(id: string, versionId: string): Promise<{
  active_version_number: number
  publishing_enabled: false
}> {
  return json(await fetch(`/api/pins/proposals/${id}/active-version`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version_id: versionId }),
  }))
}