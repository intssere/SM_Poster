export type ReviewCreativeVersion = {
  id: string | null
  kind: string
  active: boolean
  status: 'REVIEW' | 'REJECTED'
}

export type ReviewCreativeSettings = {
  effective_mode: string
  decorative_backgrounds_enabled: boolean
  capabilities: { decorative_backgrounds: boolean }
}

export function canGenerateReviewCreative(
  proposalStatus: string,
  settings: ReviewCreativeSettings | null,
): boolean {
  return proposalStatus === 'REVIEW'
    && settings?.effective_mode === 'hosted_paid'
    && settings.decorative_backgrounds_enabled
    && settings.capabilities.decorative_backgrounds
}

export function hasGeneratedBackground(versions: ReviewCreativeVersion[]): boolean {
  return versions.some((version) => version.kind === 'IMAGE_BACKGROUND')
}

export function reviewCreativeButtonState(
  working: string | null,
  versions: ReviewCreativeVersion[],
  eligible: boolean,
  progress: number,
) {
  const loading = working === 'image_background'
  return {
    disabled: working !== null || !eligible,
    loading,
    label: loading
      ? `Generating ${progress}%`
      : hasGeneratedBackground(versions) ? 'Regenerate' : 'Generate AI Creative',
  }
}

export function canSelectReviewCreative(version: ReviewCreativeVersion | undefined, working: string | null): boolean {
  return Boolean(version && version.status === 'REVIEW' && !version.active && working === null)
}

export function canRejectReviewCreative(version: ReviewCreativeVersion | undefined, working: string | null): boolean {
  return Boolean(version?.id && version.kind === 'IMAGE_BACKGROUND' && version.status === 'REVIEW' && working === null)
}