import assert from 'node:assert/strict'
import test from 'node:test'
import { presentStatus } from '../ui/status.ts'

test('maps publishing state machine values to professional product language', () => {
  assert.deepEqual(presentStatus('PUBLISH_UNKNOWN'), { label: 'Needs verification', tone: 'warning' })
  assert.deepEqual(presentStatus('PUBLISHED'), { label: 'Published', tone: 'success' })
  assert.deepEqual(presentStatus('PUBLISHING'), { label: 'Publishing', tone: 'info' })
  assert.deepEqual(presentStatus('PUBLISH_FAILED'), { label: 'Failed', tone: 'danger' })
})

test('maps review and scheduling states without exposing raw enum formatting', () => {
  assert.deepEqual(presentStatus('READY_FOR_REVIEW'), { label: 'Needs review', tone: 'warning' })
  assert.deepEqual(presentStatus('APPROVED'), { label: 'Approved', tone: 'success' })
  assert.deepEqual(presentStatus('SCHEDULED'), { label: 'Scheduled', tone: 'info' })
  assert.deepEqual(presentStatus('CANCELLED'), { label: 'Cancelled', tone: 'neutral' })
})

test('falls back safely for unknown and missing statuses', () => {
  assert.deepEqual(presentStatus('awaiting_operator'), { label: 'Awaiting Operator', tone: 'neutral' })
  assert.deepEqual(presentStatus(undefined), { label: 'Unavailable', tone: 'neutral' })
})
