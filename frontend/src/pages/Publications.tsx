import { useEffect, useState } from 'react'
type Publication = { id:string; status:string; revision_id?:string; creative_id?:string; pinterest_board_id?:string; scheduled_for?:string; published_at?:string; pinterest_pin_id?:string; error_code?:string }
export function PublicationsPage() {
  const [rows,setRows]=useState<Publication[]>([])
  useEffect(()=>{fetch('/api/publications',{credentials:'include'}).then(r=>r.json()).then(setRows).catch(()=>setRows([]))},[])
  return <section><header><div><p className="eyebrow">PUBLICATIONS / SCHEDULER</p><h2>Publication queue</h2><p>Scheduler foundation: available · Live Pinterest publishing: disabled</p></div></header><section className="panel"><strong>Publishing disabled</strong><p>Provider writes require explicit enablement and approved readiness.</p></section>{rows.map(row=><article className="panel" key={row.id}><h3>{row.id}</h3><p>Status: {row.status}</p><p>Revision: {row.revision_id || 'original'} · Creative: {row.creative_id || '—'}</p><p>Board: {row.pinterest_board_id || '—'}</p><p>Scheduled: {row.scheduled_for || '—'} · Published: {row.published_at || '—'}</p>{row.error_code&&<p>Readiness: {row.error_code}</p>}</article>)}</section>
}
