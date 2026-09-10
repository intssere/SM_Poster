import type { CatalogProduct } from '../api/catalog'

export const EXACT_PRODUCT_SEARCH_LIMIT = 20

export type ExactProductSearchState = {
  requestId: number
  query: string
  results: CatalogProduct[]
  selected: CatalogProduct | null
  loading: boolean
  error: string | null
}

export const initialExactProductSearchState: ExactProductSearchState = {
  requestId: 0,
  query: '',
  results: [],
  selected: null,
  loading: false,
  error: null,
}

export function exactProductSearchOptions(state: ExactProductSearchState) {
  if (!state.selected || state.results.some((product) => product.id === state.selected?.id)) {
    return state.results
  }
  return [state.selected, ...state.results]
}

export function exactProductSearchStarted(
  state: ExactProductSearchState,
  requestId: number,
): ExactProductSearchState {
  if (state.requestId !== requestId) return state
  return { ...state, loading: true, error: null }
}

export function exactProductSearchSucceeded(
  state: ExactProductSearchState,
  results: CatalogProduct[],
  requestId: number,
): ExactProductSearchState {
  if (state.requestId !== requestId) return state
  return { ...state, results, loading: false, error: null }
}

export function exactProductSearchFailed(
  state: ExactProductSearchState,
  error: string,
  requestId: number,
): ExactProductSearchState {
  if (state.requestId !== requestId) return state
  return { ...state, results: [], loading: false, error }
}

export function exactProductSearchQueryChanged(
  state: ExactProductSearchState,
  query: string,
): ExactProductSearchState {
  return {
    ...state,
    requestId: state.requestId + 1,
    query,
    results: query.trim() ? state.results : [],
    loading: false,
    error: null,
  }
}