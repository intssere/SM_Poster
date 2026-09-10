import assert from 'node:assert/strict'
import test from 'node:test'

import { getProducts, type CatalogProduct } from '../api/catalog.ts'
import {
  EXACT_PRODUCT_SEARCH_LIMIT,
  exactProductSearchQueryChanged,
  exactProductSearchFailed,
  exactProductSearchOptions,
  exactProductSearchStarted,
  exactProductSearchSucceeded,
  initialExactProductSearchState,
} from './exactProductSearchState.ts'

const selected = {
  id: 'product-selected',
  title: 'Selected fragrance',
  handle: 'selected-fragrance',
} as CatalogProduct
const other = {
  id: 'product-other',
  title: 'Other fragrance',
  handle: 'other-fragrance',
} as CatalogProduct

test('bounded product search sends one capped request with the requested term', async () => {
  const previousFetch = globalThis.fetch
  const requests: string[] = []
  globalThis.fetch = async (input) => {
    requests.push(String(input))
    return new Response(JSON.stringify({ items: [], total: 0, offset: 0, limit: 20 }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  try {
    await getProducts({
      search: 'amharaoge67s',
      vendor: '',
      productType: '',
      stockStatus: 'in_stock',
      eligibility: 'eligible',
      normalizationStatus: '',
      minPrice: '',
      maxPrice: '',
    }, 0, undefined, EXACT_PRODUCT_SEARCH_LIMIT)
  } finally {
    globalThis.fetch = previousFetch
  }

  assert.equal(requests.length, 1)
  const request = new URL(requests[0], 'https://example.test')
  assert.equal(request.searchParams.get('search'), 'amharaoge67s')
  assert.equal(request.searchParams.get('offset'), '0')
  assert.equal(request.searchParams.get('limit'), '20')
})

test('search result and error updates preserve the selected exact product', () => {
  const withSelection = { ...initialExactProductSearchState, selected }
  const changed = exactProductSearchQueryChanged(withSelection, 'other')
  const loading = exactProductSearchStarted(changed, changed.requestId)
  const succeeded = exactProductSearchSucceeded(loading, [other], 1)
  assert.equal(succeeded.selected?.id, selected.id)
  assert.deepEqual(exactProductSearchOptions(succeeded).map((item) => item.id), [selected.id, other.id])

  const failed = exactProductSearchFailed(loading, 'Catalog search failed', 1)
  assert.equal(failed.selected?.id, selected.id)
  assert.equal(failed.error, 'Catalog search failed')
  assert.deepEqual(exactProductSearchOptions(failed).map((item) => item.id), [selected.id])
})

test('stale success and error responses cannot replace the latest query state', () => {
  const firstChanged = exactProductSearchQueryChanged(initialExactProductSearchState, 'first')
  const first = exactProductSearchStarted(firstChanged, firstChanged.requestId)
  const secondChanged = exactProductSearchQueryChanged(first, 'second')

  assert.equal(exactProductSearchStarted(secondChanged, first.requestId), secondChanged)
  assert.equal(exactProductSearchSucceeded(secondChanged, [selected], first.requestId), secondChanged)
  assert.equal(exactProductSearchFailed(secondChanged, 'stale error', first.requestId), secondChanged)

  const second = exactProductSearchStarted(secondChanged, secondChanged.requestId)
  const latest = exactProductSearchSucceeded(second, [other], second.requestId)
  assert.equal(latest.query, 'second')
  assert.deepEqual(latest.results.map((item) => item.id), [other.id])
})

test('clearing a query invalidates pending responses while preserving selection', () => {
  const changed = exactProductSearchQueryChanged(
    { ...initialExactProductSearchState, selected },
    'amber oud',
  )
  const loading = exactProductSearchStarted(changed, changed.requestId)
  const cleared = exactProductSearchQueryChanged(loading, '')

  assert.equal(exactProductSearchStarted(cleared, loading.requestId), cleared)
  assert.equal(exactProductSearchSucceeded(cleared, [other], loading.requestId), cleared)
  assert.equal(cleared.loading, false)
  assert.equal(cleared.error, null)
  assert.equal(cleared.selected?.id, selected.id)
})