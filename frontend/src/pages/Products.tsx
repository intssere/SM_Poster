import { FormEvent, useCallback, useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2, PackageSearch, RefreshCw, Search } from 'lucide-react'

import {
  CatalogProduct,
  FilterOptions,
  getFilterOptions,
  getIntelligenceSummary,
  getProducts,
  IntelligenceSummary,
  getShopifyStatus,
  getSyncStatus,
  ProductFilters,
  ShopifyStatus,
  startCatalogSync,
  SyncStatus,
} from '../api/catalog'
import { Button, EmptyState, MetricCard, PageHeader, StatusBadge, Surface } from '../ui/primitives'
import { formatOperationalCount } from '../ui/phaseBPresentation'

const emptyFilters: ProductFilters = {
  search: '',
  vendor: '',
  productType: '',
  stockStatus: '',
  eligibility: '',
  normalizationStatus: '',
  minPrice: '',
  maxPrice: '',
}

function formatDate(value?: string | null) {
  if (!value) return 'Never'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function money(value?: number | null) {
  if (value == null) return 'Unknown'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)
}

function statusToken(value: string) {
  return value === 'eligible' || value === 'COMPLETE' || value === 'in_stock' ? 'READY'
    : value === 'ineligible' ? 'BLOCKED'
      : value === 'PARTIAL' ? 'REVIEW'
        : value === 'out_of_stock' ? 'UNAVAILABLE'
          : value
}

export function ProductsPage() {
  const [connection, setConnection] = useState<ShopifyStatus | null>(null)
  const [sync, setSync] = useState<SyncStatus | null>(null)
  const [products, setProducts] = useState<CatalogProduct[]>([])
  const [intelligence, setIntelligence] = useState<IntelligenceSummary | null>(null)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [filters, setFilters] = useState(emptyFilters)
  const [appliedFilters, setAppliedFilters] = useState(emptyFilters)
  const [loading, setLoading] = useState(true)
  const [message, setMessage] = useState<string | null>(null)
  const [options, setOptions] = useState<FilterOptions>({ vendors: [], product_types: [] })
  const [optionsError, setOptionsError] = useState<string | null>(null)
  const [filterErrors, setFilterErrors] = useState({ vendor: '', productType: '' })

  const loadFilterOptions = useCallback(async (signal?: AbortSignal) => {
    try {
      setOptions(await getFilterOptions(signal))
      setOptionsError(null)
    } catch (error) {
      if ((error as Error).name !== 'AbortError') setOptionsError('Filter options unavailable. Reload to try again.')
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void loadFilterOptions(controller.signal)
    return () => controller.abort()
  }, [loadFilterOptions])

  const loadProducts = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      const result = await getProducts(appliedFilters, offset, signal)
      setProducts(result.items)
      setTotal(result.total)
      setMessage(null)
    } catch (error) {
      if ((error as Error).name !== 'AbortError') setMessage((error as Error).message)
    } finally {
      setLoading(false)
    }
  }, [appliedFilters, offset])

  const loadStatus = useCallback(async (signal?: AbortSignal) => {
    const [shopify, summary] = await Promise.all([getShopifyStatus(signal), getIntelligenceSummary(signal)])
    setConnection(shopify)
    setSync(shopify.last_sync)
    setIntelligence(summary)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([loadStatus(controller.signal), loadProducts(controller.signal)]).catch((error) => {
      if ((error as Error).name !== 'AbortError') setMessage((error as Error).message)
    })
    return () => controller.abort()
  }, [loadProducts, loadStatus])

  useEffect(() => {
    if (!sync || !['QUEUED', 'RUNNING'].includes(sync.status)) return
    let completionRefreshed = false
    const interval = window.setInterval(async () => {
      try {
        const current = await getSyncStatus()
        setSync(current)
        if (!['QUEUED', 'RUNNING'].includes(current.status) && !completionRefreshed) {
          completionRefreshed = true
          window.clearInterval(interval)
          await Promise.all([loadStatus(), loadProducts(), loadFilterOptions()])
        }
      } catch (error) {
        setMessage((error as Error).message)
      }
    }, 2500)
    return () => window.clearInterval(interval)
  }, [loadProducts, loadStatus, loadFilterOptions, sync])

  function applyFilters(event: FormEvent) {
    event.preventDefault()
    const canonical = (value: string, available: string[]) => {
      const cleaned = value.trim()
      return cleaned ? available.find((option) => option.toLowerCase() === cleaned.toLowerCase()) : ''
    }
    const vendor = canonical(filters.vendor, options.vendors)
    const productType = canonical(filters.productType, options.product_types)
    setFilterErrors({ vendor: vendor === undefined ? 'Choose an available brand / vendor.' : '', productType: productType === undefined ? 'Choose an available product type.' : '' })
    if (vendor === undefined || productType === undefined) return
    const selected = { ...filters, vendor, productType }
    setFilters(selected)
    setOffset(0)
    setAppliedFilters(selected)
  }

  async function startSync() {
    setMessage(null)
    try {
      const result = await startCatalogSync()
      setMessage(result.message)
      await loadStatus()
    } catch (error) {
      setMessage((error as Error).message)
    }
  }

  const syncActive = !!sync && ['QUEUED', 'RUNNING'].includes(sync.status)
  const clearFilters = () => {
    setFilters(emptyFilters)
    setAppliedFilters(emptyFilters)
    setFilterErrors({ vendor: '', productType: '' })
    setOffset(0)
  }

  return <div className="ds-catalog-page">
    <PageHeader
      eyebrow="Catalog"
      title="Product workspace"
      description="Find products, understand readiness, and keep Shopify catalog facts current without leaving the content workflow."
      actions={<Button variant="primary" onClick={() => void startSync()} disabled={!connection?.connected || syncActive}><RefreshCw size={16} className={syncActive ? 'spin' : ''} />{syncActive ? 'Sync in progress' : 'Sync Shopify'}</Button>}
    />

    <section className="ds-metrics" aria-label="Catalog summary">
      <MetricCard label="Products" value={formatOperationalCount(intelligence?.total)} note="Catalog records available" icon={<PackageSearch size={19} />} />
      <MetricCard label="Complete" value={formatOperationalCount(intelligence?.normalization_status.COMPLETE)} note="Ready normalized records" icon={<CheckCircle2 size={19} />} />
      <MetricCard label="Partial" value={formatOperationalCount(intelligence?.normalization_status.PARTIAL)} note="Records missing some normalized facts" icon={<AlertCircle size={19} />} />
      <MetricCard label="QA warnings" value={formatOperationalCount(intelligence?.qa_warning_products)} note="Products that need catalog attention" icon={<AlertCircle size={19} />} />
    </section>

    <div className="ds-catalog-overview">
      <Surface>
        <div className="ds-section-heading"><div><p className="ds-eyebrow">Shopify</p><h2>Catalog source</h2><p>Connection and synchronization health for the product source.</p></div><StatusBadge status={connection?.connected ? 'CONNECTED' : connection ? 'BLOCKED' : 'CHECKING'} label={connection?.connected ? 'Connected' : connection ? 'Needs attention' : 'Checking'} /></div>
        <div className="ds-catalog-overview__status">
          <div className="ds-catalog-overview__row"><div><strong>{connection?.shop_domain || 'Shopify store'}</strong><small>{connection?.message || 'Checking Shopify connection'}</small></div><span>{connection?.api_version || '—'}</span></div>
          <div className="ds-catalog-overview__row"><div><strong>Last successful sync</strong><small>{formatDate(connection?.last_successful_sync_at)}</small></div><StatusBadge status={syncActive ? 'CHECKING' : sync?.status || 'UNKNOWN'} label={syncActive ? 'Syncing' : sync?.status?.replaceAll('_', ' ') || 'Not started'} /></div>
        </div>
      </Surface>
      <Surface>
        <div className="ds-section-heading"><div><p className="ds-eyebrow">Latest sync</p><h2>Import activity</h2><p>Raw synchronization counts from the existing catalog service.</p></div></div>
        <div className="ds-product-card__facts">
          <div className="ds-product-card__fact"><span>Seen</span><strong>{formatOperationalCount(sync?.total_seen)}</strong></div>
          <div className="ds-product-card__fact"><span>Imported</span><strong>{formatOperationalCount(sync?.products_imported)}</strong></div>
          <div className="ds-product-card__fact"><span>Updated</span><strong>{formatOperationalCount(sync?.products_updated)}</strong></div>
          <div className="ds-product-card__fact"><span>Failed</span><strong>{formatOperationalCount(sync?.products_failed)}</strong></div>
        </div>
        {sync?.last_error ? <p className="ds-product-card__note ds-product-card__note--warning">{sync.last_error}</p> : null}
      </Surface>
    </div>

    <Surface className="ds-catalog-filter-panel">
      <div className="ds-section-heading"><div><p className="ds-eyebrow">Find products</p><h2>Search and filter</h2><p>Filter by catalog facts already stored by the system.</p></div></div>
      <form onSubmit={applyFilters}>
        <div className="ds-catalog-filters-grid">
          <label className="ds-field ds-field--search">Search<Search size={16} /><input value={filters.search} onChange={(event) => setFilters({ ...filters, search: event.target.value })} placeholder="Title or product" /></label>
          <label className="ds-field">Brand / vendor<input list="catalog-vendors" autoComplete="off" value={filters.vendor} aria-invalid={!!filterErrors.vendor} onChange={(event) => { setFilters({ ...filters, vendor: event.target.value }); setFilterErrors({ ...filterErrors, vendor: '' }) }} placeholder="Available brands" /><datalist id="catalog-vendors">{options.vendors.map((value) => <option key={value} value={value} />)}</datalist>{filterErrors.vendor ? <small role="alert">{filterErrors.vendor}</small> : null}</label>
          <label className="ds-field">Product type<input list="catalog-product-types" autoComplete="off" value={filters.productType} aria-invalid={!!filterErrors.productType} onChange={(event) => { setFilters({ ...filters, productType: event.target.value }); setFilterErrors({ ...filterErrors, productType: '' }) }} placeholder="Available types" /><datalist id="catalog-product-types">{options.product_types.map((value) => <option key={value} value={value} />)}</datalist>{filterErrors.productType ? <small role="alert">{filterErrors.productType}</small> : null}</label>
          <label className="ds-field">Stock<select value={filters.stockStatus} onChange={(event) => setFilters({ ...filters, stockStatus: event.target.value })}><option value="">Any stock</option><option value="in_stock">In stock</option><option value="out_of_stock">Out of stock</option></select></label>
          <label className="ds-field">Eligibility<select value={filters.eligibility} onChange={(event) => setFilters({ ...filters, eligibility: event.target.value })}><option value="">Any eligibility</option><option value="eligible">Eligible</option><option value="ineligible">Ineligible</option></select></label>
          <label className="ds-field">Normalization<select value={filters.normalizationStatus} onChange={(event) => setFilters({ ...filters, normalizationStatus: event.target.value })}><option value="">Any normalization</option><option value="COMPLETE">Complete</option><option value="PARTIAL">Partial</option><option value="UNKNOWN">Unknown</option></select></label>
        </div>
        <div className="ds-price-range" style={{ maxWidth: 360, marginTop: 10 }}><label className="ds-field">Minimum price<input type="number" min="0" value={filters.minPrice} onChange={(event) => setFilters({ ...filters, minPrice: event.target.value })} placeholder="$0" /></label><label className="ds-field">Maximum price<input type="number" min="0" value={filters.maxPrice} onChange={(event) => setFilters({ ...filters, maxPrice: event.target.value })} placeholder="No maximum" /></label></div>
        <div className="ds-filter-actions" style={{ marginTop: 12 }}><Button type="submit" variant="primary">Apply filters</Button><Button type="button" variant="ghost" onClick={clearFilters}>Clear</Button></div>
      </form>
      {optionsError ? <p className="ds-catalog-message" role="alert">{optionsError}</p> : null}
      {message ? <p className="ds-catalog-message">{message}</p> : null}
    </Surface>

    <Surface>
      <div className="ds-product-toolbar"><div><p className="ds-eyebrow">Products</p><h2>{total.toLocaleString()} matching products</h2></div>{total > 0 ? <div className="ds-pagination"><Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</Button><span>{offset + 1}–{Math.min(offset + 50, total)}</span><Button variant="ghost" disabled={offset + 50 >= total} onClick={() => setOffset(offset + 50)}>Next</Button></div> : null}</div>
      {loading ? <EmptyState title="Loading catalog" description="Fetching the current product workspace." /> : products.length === 0 ? <EmptyState title="No products to show" description={connection?.connected ? 'Change the current filters or synchronize Shopify.' : 'Connect Shopify to import the Diamond Shelf catalog.'} /> : <div className="ds-product-grid">{products.map((product) => <ProductCard key={product.id} product={product} />)}</div>}
    </Surface>
  </div>
}

function ProductCard({ product }: { product: CatalogProduct }) {
  const eligibilityDetail = product.eligibility_blocking_reasons[0] || `${product.eligibility_score.toFixed(0)} / 100 · all gates passed`
  return <article className="ds-product-card">
    <a className="ds-product-card__media" href={product.product_url} target="_blank" rel="noreferrer" aria-label={`Open ${product.title} in storefront`}>{product.image_url ? <img src={product.image_url} alt="" /> : <PackageSearch size={28} />}</a>
    <div className="ds-product-card__body">
      <a className="ds-product-card__title" href={product.product_url} target="_blank" rel="noreferrer"><strong>{product.title}</strong><small>{product.vendor || 'Unknown brand'}</small></a>
      <div className="ds-product-card__facts">
        <div className="ds-product-card__fact"><span>Price</span><strong>{money(product.price)}</strong></div>
        <div className="ds-product-card__fact"><span>Inventory</span><strong>{product.inventory_total.toLocaleString()} units</strong></div>
        <div className="ds-product-card__fact"><span>Type</span><strong>{product.product_type || 'Unknown'}</strong></div>
        <div className="ds-product-card__fact"><span>Category</span><strong>{product.normalization_category.replaceAll('_', ' ')}</strong></div>
      </div>
      <div className="ds-product-card__statuses"><StatusBadge status={statusToken(product.inventory_status)} label={product.inventory_status.replaceAll('_', ' ')} /><StatusBadge status={statusToken(product.eligibility_status)} label={product.eligibility_status} /><StatusBadge status={statusToken(product.normalization_status)} label={product.normalization_status} /></div>
      <p className="ds-product-card__note">{eligibilityDetail}</p>
      {product.normalization_missing_fields.length ? <p className="ds-product-card__note">Missing normalized facts: {product.normalization_missing_fields.join(', ')}</p> : null}
      {product.qa_warnings[0] ? <p className="ds-product-card__note ds-product-card__note--warning">{product.qa_warnings[0]}</p> : null}
    </div>
  </article>
}
