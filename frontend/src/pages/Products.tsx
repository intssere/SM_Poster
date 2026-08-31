import { FormEvent, useCallback, useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2, PackageSearch, RefreshCw, Search } from 'lucide-react'

import {
  CatalogProduct,
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
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

function money(value?: number | null) {
  if (value == null) return 'Unknown'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)
}

function statusClass(value: string) {
  return value.toLowerCase().replaceAll('_', '-')
}

function authenticationLabel(value?: string | null) {
  if (value === 'CLIENT_CREDENTIALS') return 'Client Credentials'
  if (value === 'ACCESS_TOKEN') return 'Access Token'
  return 'Not configured'
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
    const [shopify, summary] = await Promise.all([
      getShopifyStatus(signal),
      getIntelligenceSummary(signal),
    ])
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
    const interval = window.setInterval(async () => {
      try {
        const current = await getSyncStatus()
        setSync(current)
        if (!['QUEUED', 'RUNNING'].includes(current.status)) {
          await Promise.all([loadStatus(), loadProducts()])
        }
      } catch (error) {
        setMessage((error as Error).message)
      }
    }, 2500)
    return () => window.clearInterval(interval)
  }, [loadProducts, loadStatus, sync])

  function applyFilters(event: FormEvent) {
    event.preventDefault()
    setOffset(0)
    setAppliedFilters(filters)
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

  const syncActive = sync && ['QUEUED', 'RUNNING'].includes(sync.status)

  return <div className="products-page">
    <header className="page-heading">
      <div>
        <p className="eyebrow">SHOPIFY CATALOG</p>
        <h2>Products</h2>
        <p>Raw Shopify catalog facts with separate, deterministic editorial normalization.</p>
      </div>
      <button className="primary-action" onClick={startSync} disabled={!connection?.connected || !!syncActive}>
        <RefreshCw size={16} className={syncActive ? 'spin' : ''} />
        {syncActive ? 'Sync in progress' : 'Start Catalog Sync'}
      </button>
    </header>

    <section className="sync-grid">
      <article className="sync-card">
        <div className="sync-title">
          {connection?.connected ? <CheckCircle2 size={20} /> : <AlertCircle size={20} />}
          <div><strong>{connection?.message || 'Checking Shopify connection'}</strong><small>{connection?.shop_domain || 'No shop configured'}</small></div>
        </div>
        <dl>
          <div><dt>API version</dt><dd>{connection?.api_version || '—'}</dd></div>
          <div><dt>Authentication</dt><dd>{authenticationLabel(connection?.authentication_method)}</dd></div>
          <div><dt>Last successful sync</dt><dd>{formatDate(connection?.last_successful_sync_at)}</dd></div>
          <div><dt>Required scopes</dt><dd>{connection?.required_scopes?.join(', ') || '—'}</dd></div>
        </dl>
        {!connection?.connected && connection && <p className={connection.status === 'AUTHENTICATION_FAILED' ? 'error-message' : 'notice'}>{connection.status === 'INSUFFICIENT_SCOPES' && connection.missing_scopes.length ? `Missing scopes: ${connection.missing_scopes.join(', ')}.` : connection.guidance}</p>}
      </article>
      <article className="sync-card">
        <div className="sync-title"><PackageSearch size={20}/><div><strong>Catalog sync</strong><small>{sync?.status || 'NOT_STARTED'}</small></div></div>
        <dl className="sync-counts">
          <div><dt>Seen</dt><dd>{sync?.total_seen || 0}</dd></div>
          <div><dt>Imported</dt><dd>{sync?.products_imported || 0}</dd></div>
          <div><dt>Updated</dt><dd>{sync?.products_updated || 0}</dd></div>
          <div><dt>Failed</dt><dd>{sync?.products_failed || 0}</dd></div>
        </dl>
        {sync?.last_error && <p className="error-message">{sync.last_error}</p>}
      </article>
      <article className="sync-card">
        <div className="sync-title"><PackageSearch size={20}/><div><strong>Normalization v2</strong><small>Category-aware, catalog-fact only</small></div></div>
        <dl className="sync-counts">
          <div><dt>Complete</dt><dd>{intelligence?.normalization_status.COMPLETE.toLocaleString() || 0}</dd></div>
          <div><dt>Partial</dt><dd>{intelligence?.normalization_status.PARTIAL.toLocaleString() || 0}</dd></div>
          <div><dt>Unknown</dt><dd>{intelligence?.normalization_status.UNKNOWN.toLocaleString() || 0}</dd></div>
          <div><dt>QA warnings</dt><dd>{intelligence?.qa_warning_products.toLocaleString() || 0}</dd></div>
        </dl>
      </article>
    </section>

    <form className="catalog-filters" onSubmit={applyFilters}>
      <label className="search-field"><Search size={16}/><input value={filters.search} onChange={(event) => setFilters({...filters, search: event.target.value})} placeholder="Search products"/></label>
      <input value={filters.vendor} onChange={(event) => setFilters({...filters, vendor: event.target.value})} placeholder="Brand / vendor"/>
      <input value={filters.productType} onChange={(event) => setFilters({...filters, productType: event.target.value})} placeholder="Product type"/>
      <select value={filters.stockStatus} onChange={(event) => setFilters({...filters, stockStatus: event.target.value})}><option value="">Any stock</option><option value="in_stock">In stock</option><option value="out_of_stock">Out of stock</option></select>
      <select value={filters.eligibility} onChange={(event) => setFilters({...filters, eligibility: event.target.value})}><option value="">Any eligibility</option><option value="eligible">Eligible</option><option value="ineligible">Ineligible</option></select>
      <select value={filters.normalizationStatus} onChange={(event) => setFilters({...filters, normalizationStatus: event.target.value})}><option value="">Any normalization</option><option value="COMPLETE">Complete</option><option value="PARTIAL">Partial</option><option value="UNKNOWN">Unknown</option></select>
      <input type="number" min="0" value={filters.minPrice} onChange={(event) => setFilters({...filters, minPrice: event.target.value})} placeholder="Min price"/>
      <input type="number" min="0" value={filters.maxPrice} onChange={(event) => setFilters({...filters, maxPrice: event.target.value})} placeholder="Max price"/>
      <button type="submit">Apply filters</button>
      <button type="button" className="secondary-action" onClick={() => { setFilters(emptyFilters); setAppliedFilters(emptyFilters) }}>Clear</button>
    </form>

    {message && <p className="catalog-message">{message}</p>}

    <section className="catalog-panel">
      <div className="catalog-panel-heading"><div><p className="eyebrow">CATALOG</p><h3>{total.toLocaleString()} products</h3></div>{total > 0 && <div className="pagination"><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button><span>{offset + 1}–{Math.min(offset + 50, total)}</span><button disabled={offset + 50 >= total} onClick={() => setOffset(offset + 50)}>Next</button></div>}</div>
      {loading ? <div className="empty-catalog">Loading catalog…</div> : products.length === 0 ? <div className="empty-catalog"><PackageSearch size={32}/><strong>No products to show</strong><p>{connection?.connected ? 'Start a catalog sync or change the current filters.' : 'Connect Shopify to import the Diamond Shelf catalog.'}</p></div> :
        <div className="product-table-wrap"><table className="product-table"><thead><tr><th>Product</th><th>Brand</th><th>Price</th><th>Inventory</th><th>Category</th><th>Eligibility</th><th>Normalization</th></tr></thead><tbody>{products.map((product) => <tr key={product.id}><td><a href={product.product_url} target="_blank" rel="noreferrer" className="product-cell">{product.image_url ? <img src={product.image_url} alt=""/> : <span className="image-placeholder"><PackageSearch size={18}/></span>}<span><strong>{product.title}</strong><small>{product.handle}</small></span></a></td><td>{product.vendor || 'Unknown'}</td><td>{money(product.price)}</td><td><span className={`badge ${statusClass(product.inventory_status)}`}>{product.inventory_status.replaceAll('_', ' ')}</span><small>{product.inventory_total} units</small></td><td>{product.product_type || 'Unknown'}<small>{product.normalization_category.replaceAll('_', ' ')}</small></td><td><span className={`badge ${statusClass(product.eligibility_status)}`}>{product.eligibility_status}</span><small>{product.eligibility_blocking_reasons[0] || `${product.eligibility_score.toFixed(0)} / 100 · all gates passed`}</small></td><td><span className={`badge ${statusClass(product.normalization_status)}`}>{product.normalization_status}</span><small>{product.normalization_missing_fields.length ? `Missing: ${product.normalization_missing_fields.join(', ')}` : 'All required fields present'}</small>{product.qa_warnings[0] && <small className="warning-text">{product.qa_warnings[0]}</small>}</td></tr>)}</tbody></table></div>}
    </section>
  </div>
}