import assert from 'node:assert/strict'
import test from 'node:test'

import {
  canGenerateReviewCreative,
  canRejectReviewCreative,
  canSelectReviewCreative,
  reviewCreativeButtonState,
} from './reviewCreativeState.ts'

const settings = {
  effective_mode: 'hosted_paid',
  decorative_backgrounds_enabled: true,
  capabilities: { decorative_backgrounds: true },
}
const generated = { id: 'revision', kind: 'IMAGE_BACKGROUND', active: false, status: 'REVIEW' as const }
const copyRevision = { id: 'copy-revision', kind: 'COPY', active: false, status: 'REVIEW' as const }
const originalRevision = { id: null, kind: 'ORIGINAL', active: false, status: 'REVIEW' as const }

test('generation is available only for eligible review-only hosted background proposals', () => {
  assert.equal(canGenerateReviewCreative('REVIEW', settings), true)
  assert.equal(canGenerateReviewCreative('APPROVED', settings), false)
  assert.equal(canGenerateReviewCreative('REVIEW', { ...settings, decorative_backgrounds_enabled: false }), false)
  assert.equal(canGenerateReviewCreative('REVIEW', { ...settings, effective_mode: 'disabled' }), false)
})

test('generation button exposes progress and becomes regenerate after a background exists', () => {
  assert.deepEqual(reviewCreativeButtonState('image_background', [], true, 47), {
    disabled: true,
    loading: true,
    label: 'Generating 47%',
  })
  assert.equal(reviewCreativeButtonState(null, [generated], true, 0).label, 'Regenerate')
})

test('selection accepts every inactive review version while rejection is limited to review image backgrounds', () => {
  assert.equal(canSelectReviewCreative(generated, null), true)
  assert.equal(canSelectReviewCreative(copyRevision, null), true)
  assert.equal(canSelectReviewCreative(originalRevision, null), true)
  assert.equal(canSelectReviewCreative({ ...generated, active: true }, null), false)
  assert.equal(canRejectReviewCreative(generated, null), true)
  assert.equal(canRejectReviewCreative({ ...generated, active: true }, null), true)
  assert.equal(canRejectReviewCreative({ ...generated, kind: 'ORIGINAL' }, null), false)
})

test('rejected versions cannot be selected or rejected again', () => {
  const rejected = { ...generated, status: 'REJECTED' as const }
  assert.equal(canSelectReviewCreative(rejected, null), false)
  assert.equal(canRejectReviewCreative(rejected, null), false)
})