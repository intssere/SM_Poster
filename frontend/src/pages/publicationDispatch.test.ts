import assert from 'node:assert/strict'
import test from 'node:test'
import { authorizationPath, authorizationPayload, performOneShotDispatch, publishRequest, publishPath, readinessKey, readinessPath } from './publicationDispatch.ts'

test('provider routes stay isolated', () => {
  assert.equal(authorizationPath('pub-1', 'direct'), '/api/publications/pub-1/dispatch-authorization')
  assert.equal(authorizationPath('pub-1', 'buffer'), '/api/publications/pub-1/buffer-dispatch-authorization')
  assert.equal(publishPath('pub-1', 'direct'), '/api/publications/pub-1/publish')
  assert.equal(publishPath('pub-1', 'buffer'), '/api/publications/pub-1/publish-buffer')
  assert.equal(readinessPath('pub-1', 'direct'), '/api/publications/pub-1/dispatch-readiness?dispatch_provider=pinterest_direct')
  assert.equal(readinessPath('pub-1', 'buffer'), '/api/publications/pub-1/dispatch-readiness?dispatch_provider=buffer')
  assert.notEqual(readinessKey('pub-1', 'direct'), readinessKey('pub-1', 'buffer'))
})

test('publish is a one-shot allowlisted request with no client evidence', () => {
  const request = publishRequest('pub-1', 'buffer')
  assert.deepEqual(request, { url: '/api/publications/pub-1/publish-buffer', method: 'POST', credentials: 'include' })
  assert.equal('body' in request, false)
  assert.deepEqual(authorizationPayload('v3'), { confirmed: true, confirmation_text_version: 'v3' })
})

test('dispatch sends once and refreshes after non-success or lost responses', async () => {
  for (const send of [
    async () => new Response(null, { status: 409 }),
    async () => { throw new TypeError('connection lost') },
  ]) {
    let sends = 0
    let refreshes = 0
    const result = await performOneShotDispatch(
      async () => { sends += 1; return send() },
      async () => { refreshes += 1 },
    )
    assert.equal(sends, 1)
    assert.equal(refreshes, 1)
    assert.equal(Boolean(result.response) || Boolean(result.sendError), true)
  }
})