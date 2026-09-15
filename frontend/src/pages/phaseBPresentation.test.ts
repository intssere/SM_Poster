import assert from 'node:assert/strict'
import test from 'node:test'
import { buildHomeAttention, connectionStatus, formatOperationalCount } from '../ui/phaseBPresentation.ts'

test('formats live operational counts without inventing fallback numbers', () => {
  assert.equal(formatOperationalCount(2997), '2,997')
  assert.equal(formatOperationalCount(null), '—')
  assert.equal(formatOperationalCount(undefined), '—')
})

test('builds Home attention items only from real operational conditions', () => {
  assert.deepEqual(buildHomeAttention({
    backendStatus: 'connected',
    publishingEnabled: true,
    reviewCount: 2,
    qaWarnings: 3,
    shopifyConnected: false,
  }).map((item) => item.key), ['review', 'catalog', 'shopify'])

  assert.deepEqual(buildHomeAttention({
    backendStatus: 'connected',
    publishingEnabled: true,
    reviewCount: 0,
    qaWarnings: 0,
    shopifyConnected: true,
  }), [])
})

test('presents connection state with professional product language', () => {
  assert.deepEqual(connectionStatus(true), { status: 'CONNECTED', label: 'Connected' })
  assert.deepEqual(connectionStatus(false), { status: 'BLOCKED', label: 'Needs attention' })
  assert.deepEqual(connectionStatus(null), { status: 'CHECKING', label: 'Checking' })
})
