export type SyncStatus = {
  id?: string
  status: string
  started_at?: string | null
  completed_at?: string | null
  total_seen: number
  products_imported: number
  products_updated: number
  products_failed: number
  last_error?: string | null
}

export type ShopifyStatus = {
  status: 'CONNECTED' | 'NOT_CONFIGURED' | 'AUTHENTICATION_FAILED' | 'INSUFFICIENT_SCOPES'
  connected: boolean
  authentication_method?: 'CLIENT_CREDENTIALS' | 'ACCESS_TOKEN' | null
  provider: string
  shop_domain?: string | null
  api_version: string
  missing: string[]
  scopes: string[]
  missing_scopes: string[]
  required_scopes: string[]
  guidance: string
  message: string
  last_sync: SyncStatus
  last_successful_sync_at?: string | null
}

export type CatalogProduct = {
  id: string
  shopify_product_id: string
  title: string
  handle: string
  product_url: string
  image_url?: string | null
  vendor?: string | null
  product_type?: string | null
  status: string
  price?: number | null
  compare_at_price?: number | null
  inventory_total: number
  inventory_status: string
  eligibility_score: number
  eligibility_status: string
  eligibility_reasons: string[]
  eligibility_positive_reasons: string[]
  eligibility_blocking_reasons: string[]
  normalization_status: string
  normalization_category: string
  normalization_required_fields: string[]
  normalization_missing_fields: string[]
  qa_warnings: string[]
  normalized: Record<string, unknown>
  synced_at?: string | null
}

export type IntelligenceSummary = {
  total: number
  normalization_status: {
    COMPLETE: number
    PARTIAL: number
    UNKNOWN: number
  }
  field_populated: Record<string, number>
  categories: Record<string, {
    total: number
    COMPLETE: number
    PARTIAL: number
    UNKNOWN: number
  }>
  qa_warning_products: number
}

export type ProductFilters = {
  search: string
  vendor: string
  productType: string
  stockStatus: string
  eligibility: string
  normalizationStatus: string
  minPrice: string
  maxPrice: string
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.detail || payload?.message || `Request failed (${response.status})`)
  }
  return response.json()
}

export async function getShopifyStatus(signal?: AbortSignal): Promise<ShopifyStatus> {
  return json(await fetch('/api/catalog/shopify/status', { signal }))
}

export async function getSyncStatus(signal?: AbortSignal): Promise<SyncStatus> {
  return json(await fetch('/api/catalog/sync/status', { signal }))
}

export async function getIntelligenceSummary(signal?: AbortSignal): Promise<IntelligenceSummary> {
  return json(await fetch('/api/catalog/intelligence/summary', { signal }))
}

export async function startCatalogSync(): Promise<{ accepted: boolean; message: string }> {
  return json(await fetch('/api/catalog/sync', { method: 'POST' }))
}

export async function getProducts(
  filters: ProductFilters,
  offset: number,
  signal?: AbortSignal,
): Promise<{ items: CatalogProduct[]; total: number; offset: number; limit: number }> {
  const params = new URLSearchParams()
  if (filters.search) params.set('search', filters.search)
  if (filters.vendor) params.set('vendor', filters.vendor)
  if (filters.productType) params.set('product_type', filters.productType)
  if (filters.stockStatus) params.set('stock_status', filters.stockStatus)
  if (filters.eligibility) params.set('eligibility', filters.eligibility)
  if (filters.normalizationStatus) params.set('normalization_status', filters.normalizationStatus)
  if (filters.minPrice) params.set('min_price', filters.minPrice)
  if (filters.maxPrice) params.set('max_price', filters.maxPrice)
  params.set('offset', String(offset))
  params.set('limit', '50')
  return json(await fetch(`/api/catalog/products?${params}`, { signal }))
}