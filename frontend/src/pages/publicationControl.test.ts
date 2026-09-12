import assert from 'node:assert/strict'
import test from 'node:test'
import { activationRequest, canArmActivation, canCreatePublication, canDispatchBuffer, canRevokeActivation, isCurrentResponse, publicationRequest, revokeActivationRequest, submitSelectedPublication } from './publicationControl.ts'

test('publication request contains only sanitized local board record fields', () => {
  const request = publicationRequest('approval-7', 'board-record-3', '2026-04-01T10:00:00.000Z')
  assert.deepEqual(JSON.parse(request.body), { approval_id: 'approval-7', pinterest_board_record_id: 'board-record-3', scheduled_for: '2026-04-01T10:00:00.000Z' })
  for (const forbidden of ['external_board_id', 'credentials', 'actor', 'fingerprint', 'authorization_id', 'activation_id']) assert.equal(request.body.includes(forbidden), false)
})

test('activation and dispatch routes remain separate and revoke has no body', () => {
  assert.equal(activationRequest('pub/1').url, '/api/publications/pub%2F1/buffer-pilot-activation')
  assert.equal(revokeActivationRequest('pub-1').url, '/api/publications/pub-1/buffer-pilot-activation/revoke')
  assert.equal('body' in revokeActivationRequest('pub-1'), false)
  assert.equal(activationRequest('pub-1').body.includes('publish'), false)
})

test('stale responses are ignored', () => {
  assert.equal(isCurrentResponse(1, 2, 'old'), undefined)
  assert.equal(isCurrentResponse(2, 2, 'new'), 'new')
})

test('publication creation requires approval_id, never proposal id', () => {
  assert.equal(canCreatePublication(undefined, 'board-1'), false)
  assert.equal(canCreatePublication('approval-1', 'board-1'), true)
})

test('publication submission uses approval-keyed destination and fails closed when identity is missing', async () => {
  const calls: Array<[string, string]> = []
  const submit = async (approvalId: string, boardRecordId: string) => { calls.push([approvalId, boardRecordId]) }
  const destinationChoice = { 'approval-1': 'board-approved', 'proposal-1': 'board-decoy' }

  assert.equal(await submitSelectedPublication('approval-1', destinationChoice, submit), true)
  assert.deepEqual(calls, [['approval-1', 'board-approved']])
  assert.equal(await submitSelectedPublication(undefined, destinationChoice, submit), false)
  assert.equal(await submitSelectedPublication('approval-missing', destinationChoice, submit), false)
  assert.equal(calls.length, 1)
})

test('unknown and loading activation states fail closed', () => {
  assert.equal(canArmActivation('UNKNOWN'), false)
  assert.equal(canArmActivation('DISARMED', true), false)
  assert.equal(canRevokeActivation('UNKNOWN'), false)
  assert.equal(canRevokeActivation('ARMED', true), false)
  assert.equal(canDispatchBuffer('buffer', true, true, 'UNKNOWN'), false)
  assert.equal(canDispatchBuffer('buffer', true, true, 'ARMED', true), false)
  assert.equal(canDispatchBuffer('buffer', true, true, 'ARMED'), true)
})