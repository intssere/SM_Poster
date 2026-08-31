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
  kind: 'ORIGINAL' | 'COPY' | 'CREATIVE' | 'CONTENT' | 'IMAGE_BACKGROUND' | 'VIDEO_SPEC'
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
  generation_type?: 'original' | 'copy' | 'content_variant' | 'image_background' | 'video_script' | 'storyboard'
  intended_channel?: 'pinterest' | 'instagram' | 'facebook' | 'tiktok' | 'youtube_shorts'
  content_payload?: Record<string, unknown> | null
  video_spec?: Record<string, unknown> | null
  background_asset_id?: string | null
  ai_telemetry_id?: string | null
  estimated_cost_usd?: number | null
  actual_cost_usd?: number | null
  telemetry?: {
    id: string
    provider: string
    model: string
    operation: string
    request_type: string
    generation_type: string
    prompt_tokens?: number | null
    completion_tokens?: number | null
    total_tokens?: number | null
    latency_ms: number
    success: boolean
    failure_code?: string | null
    fallback_used: boolean
    fallback_reason?: string | null
    validation_failure_reason?: string | null
    estimated_cost_usd?: number | null
    actual_cost_usd?: number | null
    created_at?: string | null
  } | null
  created_at?: string | null
  creative: CreativePreview | null
}

export function proposalVersionPreviewUrl(proposalId: string, versionId: string | null): string {
  return `/api/pins/proposals/${encodeURIComponent(proposalId)}/versions/${encodeURIComponent(versionId || 'original')}/preview`
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
    content_variants: boolean
    video_scripts: boolean
    storyboards: boolean
    production_video_rendering: boolean
    hosted_provider_configured: boolean
  }
  decorative_backgrounds_enabled: boolean
  credentials_configured: boolean
  local_base_url: string
  local_model: string
  hosted_model: string
  image_model: string
  video_model: string
  request_timeout_seconds: number
  daily_budget_usd: number
  monthly_budget_usd: number
  per_request_cost_usd: number
  pricing_metadata: Record<string, { input_per_1m: number; output_per_1m: number }>
}

export type AIProviderStatus = {
  provider: 'disabled' | 'ollama' | 'openai'
  configured: boolean
  reachable: boolean
  model?: string | null
  model_available: boolean
  message: string
  failure_code?: string
  effective_mode: AISettings['effective_mode']
  timeout_seconds: number
}

export type AIUsage = {
  daily: { spent_usd: number; limit_usd: number }
  monthly: { spent_usd: number; limit_usd: number }
  recent: Array<{
    id: string
    provider: string
    model: string
    operation: string
    prompt_tokens?: number | null
    completion_tokens?: number | null
    total_tokens?: number | null
    latency_ms: number
    success: boolean
    failure_code?: string | null
    estimated_cost_usd?: number | null
    actual_cost_usd?: number | null
    fallback_used: boolean
    created_at?: string | null
  }>
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

export async function getAIStatus(): Promise<AIProviderStatus> {
  return json(await fetch('/api/ai/status'))
}

export async function getAIUsage(): Promise<AIUsage> {
  return json(await fetch('/api/ai/usage'))
}

export async function updateAISettings(
  settings: Partial<Pick<AISettings,
    'enabled' | 'provider_mode' | 'local_base_url' | 'local_model' | 'hosted_model' |
    'image_model' | 'video_model' | 'decorative_backgrounds_enabled' |
    'request_timeout_seconds' | 'daily_budget_usd' | 'monthly_budget_usd' | 'per_request_cost_usd'
  >>,
): Promise<AISettings> {
  return json(await fetch('/api/ai/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  }))
}

export async function regenerateProposal(
  id: string,
  kind: 'copy' | 'creative' | 'content_variant' | 'image_background' | 'video_script' | 'storyboard',
  options: {
    templateKey?: string
    styleKey?: string
    channel?: 'pinterest' | 'instagram' | 'facebook' | 'tiktok' | 'youtube_shorts'
    count?: number
  } = {},
): Promise<ContentVersion | { variants: ContentVersion[] }> {
  return json(await fetch(`/api/pins/proposals/${id}/regenerate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      kind,
      template_key: options.templateKey,
      style_key: options.styleKey,
      channel: options.channel || 'pinterest',
      count: options.count || 1,
    }),
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