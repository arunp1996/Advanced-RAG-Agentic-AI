import React, {useCallback, useEffect, useState} from 'react'
import {Activity, BarChart3, CheckCircle2, FileText, RefreshCw, Search, Settings, Trash2, Users, Workflow, ExternalLink, MessageSquare, Eye, X} from 'lucide-react'
import './admin-console.css'

const sections = [
  {id:'agents', label:'Agent Monitoring', icon:Workflow},
  {id:'live', label:'Live View', icon:Workflow},
  {id:'traces', label:'Traces', icon:Activity},
  {id:'conversations', label:'Conversations', icon:MessageSquare},
  {id:'documents', label:'Documents', icon:FileText},
  {id:'users', label:'Users', icon:Users},
  {id:'analytics', label:'Analytics', icon:BarChart3},
  {id:'settings', label:'Settings', icon:Settings},
]

export default function AdminConsole({token, onNotice}) {
  const [section, setSection] = useState('live')
  const [metrics, setMetrics] = useState(null)
  const [pipeline, setPipeline] = useState(null)
  const [documents, setDocuments] = useState([])
  const [users, setUsers] = useState([])
  const [traces, setTraces] = useState([])
  const [langfuseResult, setLangfuseResult] = useState({available:false,count:0,observations:[]})
  const [exportStartDate, setExportStartDate] = useState(() => {
    const d = new Date()
    d.setDate(d.getDate() - 7)
    return d.toISOString().slice(0, 10)
  })
  const [exportEndDate, setExportEndDate] = useState(
    () => new Date().toISOString().slice(0, 10)
  )
  const [exportingCsv, setExportingCsv] = useState(false)

  const [conversations, setConversations] = useState([])
  const [conversationDetail, setConversationDetail] = useState(null)
  const [settings, setSettings] = useState(null)
  const [search, setSearch] = useState('')
  const [conversationPage, setConversationPage] = useState(1)
  const [langfusePage, setLangfusePage] = useState(1)
  const [tracePage, setTracePage] = useState(1)
  const [latencyPage, setLatencyPage] = useState(1)
  const ADMIN_TRACE_PAGE_SIZE = 15
  const CONVERSATION_PAGE_SIZE = 15
  const [loading, setLoading] = useState(false)
  const headers = {Authorization:`Bearer ${token}`}
  const request = useCallback(async (path, opts={}) => {
    const response = await fetch(`${import.meta.env.VITE_API_URL || ''}${path}`, {...opts, headers:{...headers, ...(opts.headers||{})}})
    const body = await response.json().catch(()=>({detail:'Unexpected server response'}))
    if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`)
    return body
  }, [token])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [m, d, t, u, p, s, c, lf] = await Promise.all([
        request('/api/admin/metrics'),
        request('/api/admin/documents'),
        request('/api/admin/traces?limit=100'),
        request('/api/admin/users'),
        request('/api/admin/pipeline/metrics'),
        request('/api/admin/settings'),
        request('/api/admin/conversations?limit=200'),
        request('/api/admin/langfuse/observations?limit=100')
          .catch(e => ({available:false,count:0,observations:[],error:e.message}))
      ])
      setMetrics(m); setDocuments(d); setTraces(t); setUsers(u)
      setPipeline(p); setSettings(s); setConversations(c)
      setLangfuseResult(lf)
    } catch (e) { onNotice?.(e.message) } finally { setLoading(false) }
  }, [request, onNotice])
  useEffect(()=>{load()},[load])
  
  async function exportLangfuseCsv() {
    if (!exportStartDate || !exportEndDate) {
      onNotice?.('Please select both export dates.')
      return
    }
    if (exportStartDate > exportEndDate) {
      onNotice?.('Start date must not be after end date.')
      return
    }

    setExportingCsv(true)
    try {
      const params = new URLSearchParams({
        start_date: exportStartDate,
        end_date: exportEndDate,
      })
      const response = await fetch(
        `${import.meta.env.VITE_API_URL || ''}/api/admin/langfuse/observations/export.csv?${params}`,
        { headers: { Authorization: `Bearer ${token}` } }
      )

      if (!response.ok) {
        const body = await response.json().catch(
          () => ({ detail: 'Unexpected server response' })
        )
        throw new Error(body.detail || `Export failed (${response.status})`)
      }

      const blob = await response.blob()
      const objectUrl = URL.createObjectURL(blob)
      const link = document.createElement('a')
      const disposition = response.headers.get('Content-Disposition') || ''
      const filenameMatch = disposition.match(/filename="?([^";]+)"?/i)

      link.href = objectUrl
      link.download = filenameMatch?.[1] ||
        `langfuse-observations-${exportStartDate}-to-${exportEndDate}.csv`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(objectUrl)

      onNotice?.('Langfuse CSV export downloaded.')
    } catch (e) {
      onNotice?.(`CSV export failed: ${e.message}`)
    } finally {
      setExportingCsv(false)
    }
  }


  async function deleteDocument(doc) {
    if (!window.confirm(`Permanently delete "${doc.filename}" and its indexed chunks? This cannot be undone.`)) return
    try { await request(`/api/admin/documents/${encodeURIComponent(doc.id)}`, {method:'DELETE'}); setDocuments(v=>v.filter(x=>x.id!==doc.id)); onNotice?.(`Deleted ${doc.filename}`); await load() }
    catch(e) { onNotice?.(`Delete failed: ${e.message}`) }
  }
  async function changeRole(user) {
    const next = user.role === 'admin' ? 'user' : 'admin'
    if (!window.confirm(`Change ${user.email} role from ${user.role} to ${next}?`)) return
    try { await request(`/api/admin/users/${user.id}/role`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({role:next})}); onNotice?.(`Updated ${user.email} role to ${next}`); await load() }
    catch(e) { onNotice?.(e.message) }
  }
  async function viewConversation(id) {
    try { setConversationDetail(await request(`/api/admin/conversations/${encodeURIComponent(id)}`)) }
    catch(e) { onNotice?.(e.message) }
  }
  function downloadAdminCsv(filename, headers, rows) {
    const csvRows = [headers, ...rows]
    const csv = csvRows.map(row => row.map(value =>
      `"${String(value ?? '').replace(/"/g, '""')}"`
    ).join(',')).join('\r\n')
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'})
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  function exportLoadedLangfuseCsv() {
    downloadAdminCsv(
      'langfuse-observations-loaded.csv',
      ['Observation ID', 'Name', 'Type', 'Trace ID', 'Duration ms', 'Started'],
      filteredLangfuseObservations.map(o => [
        o.id, o.name, o.type, o.trace_id, o.duration_ms, o.start_time
      ])
    )
  }

  function exportApplicationTracesCsv() {
    downloadAdminCsv(
      'application-traces-loaded.csv',
      ['Event ID', 'Query', 'Action', 'Status', 'Latency ms', 'Created'],
      filteredTraces.map(t => [
        t.id, t.query, t.action, t.status, t.latency_ms, t.created_at
      ])
    )
  }

  function exportAdminConversationsCsv() {
    const rows = [
      ['Conversation ID', 'Title', 'User', 'Message Count', 'Created'],
      ...filteredConversations.map(c => [
        c.id, c.title || 'New chat', c.user_email || '',
        c.message_count ?? '', c.created_at || ''
      ])
    ]
    const csv = rows.map(row => row.map(value =>
      `"${String(value ?? '').replace(/"/g, '""')}"`
    ).join(',')).join('\r\n')
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'})
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'admin-conversations.csv'
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  async function deleteConversation(conversation) {
    if (!window.confirm(`Permanently delete the conversation "${conversation.title}" belonging to ${conversation.user_email}?`)) return
    try { await request(`/api/admin/conversations/${encodeURIComponent(conversation.id)}`, {method:'DELETE'}); setConversationDetail(null); onNotice?.('Conversation deleted and audit event recorded.'); await load() }
    catch(e) { onNotice?.(e.message) }
  }
  const filteredLangfuseObservations = (langfuseResult.observations || []).filter(o => `${o.name || ''} ${o.trace_id || ''} ${o.type || ''} ${o.id || ''}`.toLowerCase().includes(search.toLowerCase()))
  const filteredTraces = traces.filter(t => `${t.id} ${t.query||''} ${t.action||''} ${t.status||''}`.toLowerCase().includes(search.toLowerCase()))
  const filteredDocs = documents.filter(d => `${d.filename} ${d.owner_email||''} ${d.status}`.toLowerCase().includes(search.toLowerCase()))
  const filteredUsers = users.filter(u => `${u.email} ${u.role} ${u.id}`.toLowerCase().includes(search.toLowerCase()))
  const filteredConversations = conversations.filter(c => `${c.title||''} ${c.user_email||''} ${c.id}`.toLowerCase().includes(search.toLowerCase()))
  const conversationPageCount = Math.max(1, Math.ceil(filteredConversations.length / CONVERSATION_PAGE_SIZE))
  const safeConversationPage = Math.min(conversationPage, conversationPageCount)
  const langfusePageCount = Math.max(1, Math.ceil(filteredLangfuseObservations.length / ADMIN_TRACE_PAGE_SIZE))
  const safeLangfusePage = Math.min(langfusePage, langfusePageCount)
  const visibleLangfuseObservations = filteredLangfuseObservations.slice(
    (safeLangfusePage - 1) * ADMIN_TRACE_PAGE_SIZE,
    safeLangfusePage * ADMIN_TRACE_PAGE_SIZE
  )
  const tracePageCount = Math.max(1, Math.ceil(filteredTraces.length / ADMIN_TRACE_PAGE_SIZE))
  const safeTracePage = Math.min(tracePage, tracePageCount)
  const latencyPageCount = Math.max(1, Math.ceil(filteredTraces.length / ADMIN_TRACE_PAGE_SIZE))
  const safeLatencyPage = Math.min(latencyPage, latencyPageCount)
  const visibleLatency = filteredTraces.slice(
    (safeLatencyPage - 1) * ADMIN_TRACE_PAGE_SIZE,
    safeLatencyPage * ADMIN_TRACE_PAGE_SIZE
  )
  const visibleTraces = filteredTraces.slice(
    (safeTracePage - 1) * ADMIN_TRACE_PAGE_SIZE,
    safeTracePage * ADMIN_TRACE_PAGE_SIZE
  )
  const visibleConversations = filteredConversations.slice(
    (safeConversationPage - 1) * CONVERSATION_PAGE_SIZE,
    safeConversationPage * CONVERSATION_PAGE_SIZE
  )
  const maxDuration = Math.max(1,...filteredTraces.map(t=>Number(t.latency_ms||0)))
  const nav = <div className="ac-tabs">{sections.map(s=>{const Icon=s.icon;return <button key={s.id} className={section===s.id?'selected':''} onClick={()=>{setSection(s.id);setSearch('')}}><Icon size={16}/>{s.label}</button>})}</div>
  return <section className="ac-wrap">
    <header className="ac-header"><div><div className="ac-eyebrow">ADMINISTRATOR WORKSPACE</div><h2>Advanced RAG Agentic AI</h2><p>Monitor and administer the existing application. Metrics below are sourced from current backend records.</p></div><button className="ac-refresh" onClick={load} disabled={loading}><RefreshCw size={15} className={loading?'ac-spin':''}/>{loading?'Refreshing':'Refresh data'}</button></header>
    {nav}
    {section!=='live'&&section!=='settings'&&<div className="ac-toolbar"><div className="ac-search"><Search size={16}/><input value={search} onChange={e=>{setSearch(e.target.value);setConversationPage(1);setLangfusePage(1);setTracePage(1);setLatencyPage(1)}} placeholder={`Search ${section}...`}/></div><span>{section==='traces'?filteredTraces.length:section==='documents'?filteredDocs.length:section==='users'?filteredUsers.length:section==='conversations'?filteredConversations.length:section==='analytics'?(traces.length+' recent events'):''} records</span></div>}
    {section==='live'&&<>
      <div className="ac-health"><span><i/>API / FastAPI</span><span><i/>PostgreSQL records</span><span><i/>ChromaDB via RAG service</span><span className="ac-hint">Live health probes require explicit integration; these labels indicate configured components, not independent uptime checks.</span></div>
      <div className="ac-stats">{[['Users',metrics?.users],['Documents',metrics?.documents],['Indexed chunks',metrics?.chunks_indexed],['Messages',metrics?.messages],['Failed documents',metrics?.failed_documents],['Feedback',metrics?.feedback_items]].map(([label,value])=><div className="ac-stat" key={label}><span>{label}</span><b>{value ?? '—'}</b><small>From PostgreSQL</small></div>)}</div>
      <div className="ac-section-title"><div><h3>Document ingestion pipeline</h3><p>Current stored document state, not a fabricated per-request animation.</p></div><span className="ac-chip">{pipeline?.documents_ready??0} ready</span></div>
      <div className="ac-pipeline">{(pipeline?.stages||[]).map((stage,i)=><React.Fragment key={stage.name}><div className={`ac-node ${stage.status}`}><div className="ac-node-num">{i+1}</div><div><b>{stage.name}</b><small>{stage.detail}</small></div><span>{stage.status}</span></div>{i<(pipeline?.stages?.length||0)-1&&<div className="ac-connector"><span/></div>}</React.Fragment>)}</div>
      <div className="ac-section-title"><div><h3>Recent application traces</h3><p>Recent chat completion audit events. These are not Langfuse trace records unless a Langfuse integration is added.</p></div><button className="ac-link" onClick={()=>setSection('traces')}>View traces <ExternalLink size={14}/></button></div>
      <TraceTable rows={traces.slice(0,5)}/>
    </>}
    {section==='traces'&&<>
      <div className="ac-section-title">
        <div>
          <h3>Langfuse observations</h3>
          <p>Real observations retrieved from Langfuse using the protected backend endpoint.</p>
        </div>
        <span className="ac-chip">
          {langfuseResult.count ?? 0} observations
        </span>
      </div>
      <div style={{display:'flex', gap:12, alignItems:'end', flexWrap:'wrap', margin:'12px 0 16px'}}>
        <label style={{display:'grid', gap:5}}>
          <span>Start date</span>
          <input
            type="date"
            value={exportStartDate}
            max={exportEndDate}
            onChange={e => setExportStartDate(e.target.value)}
          />
        </label>
        <label style={{display:'grid', gap:5}}>
          <span>End date</span>
          <input
            type="date"
            value={exportEndDate}
            min={exportStartDate}
            max={new Date().toISOString().slice(0,10)}
            onChange={e => setExportEndDate(e.target.value)}
          />
        </label>
        <button
          className="ac-secondary"
          onClick={exportLangfuseCsv}
          disabled={exportingCsv || !exportStartDate || !exportEndDate || exportStartDate > exportEndDate}
        >
          {exportingCsv ? 'Exporting CSV...' : 'Download CSV'}
        </button>
      </div>
      {langfuseResult.error && <div className="ac-note">
        Langfuse observations could not be loaded: {langfuseResult.error}
      </div>}
      {!langfuseResult.error && !langfuseResult.available &&
        <div className="ac-note">
          Langfuse data is not available yet. Refresh the Admin Console
          after checking the backend connection and credentials.
        </div>}
      <div className="ac-table-wrap">
        <table className="ac-table">
          <thead><tr>
            <th>Observation</th><th>Type</th><th>Trace ID</th>
            <th>Duration</th><th>RAG metadata</th><th>Started</th>
          </tr></thead>
          <tbody>
            {visibleLangfuseObservations.map(o => (
              <tr key={o.id}>
                <td>
                  <strong>{o.name || 'Unnamed observation'}</strong>
                  <br/><code>{o.id || '—'}</code>
                  {o.parent_observation_id &&
                    <small>Parent: {o.parent_observation_id}</small>}
                </td>
                <td>{o.type || '—'}</td>
                <td><code>{o.trace_id || '—'}</code></td>
                <td>{o.duration_ms == null ? '—' : `${o.duration_ms} ms`}</td>
                <td>
                  {o.metadata?.retrieval_count != null &&
                    <div>Retrievals: {o.metadata.retrieval_count}</div>}
                  {o.metadata?.evidence_found != null &&
                    <div>Evidence: {String(o.metadata.evidence_found)}</div>}
                  {o.metadata?.attempts != null &&
                    <div>Attempts: {o.metadata.attempts}</div>}
                  {o.metadata?.retrieval_error != null &&
                    <div>Retrieval error: {String(o.metadata.retrieval_error)}</div>}
                  {o.metadata?.timings_ms &&
                    Object.entries(o.metadata.timings_ms).map(([key, value]) => (
                      <div key={key}>{key}: {value} ms</div>
                    ))}
                  {!o.metadata?.retrieval_count &&
                    o.metadata?.evidence_found == null &&
                    o.metadata?.attempts == null &&
                    !o.metadata?.timings_ms && '—'}
                </td>
                <td>{o.start_time ? new Date(o.start_time).toLocaleString() : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {filteredLangfuseObservations.length === 0 &&
          <div className="ac-empty">
            No matching Langfuse observations found.
          </div>}
      </div>
      <div className="ac-pagination">
        <span>Showing {filteredLangfuseObservations.length ? (safeLangfusePage - 1) * ADMIN_TRACE_PAGE_SIZE + 1 : 0}–{Math.min(safeLangfusePage * ADMIN_TRACE_PAGE_SIZE, filteredLangfuseObservations.length)} of {filteredLangfuseObservations.length} filtered observations</span>
        <div>
          <button className="ac-secondary" disabled={safeLangfusePage <= 1} onClick={() => setLangfusePage(p => Math.max(1, p - 1))}>Previous</button>
          <span className="ac-page-number">Page {safeLangfusePage} of {langfusePageCount}</span>
          <button className="ac-secondary" disabled={safeLangfusePage >= langfusePageCount} onClick={() => setLangfusePage(p => Math.min(langfusePageCount, p + 1))}>Next</button>
          <button className="ac-secondary" onClick={exportLoadedLangfuseCsv} disabled={filteredLangfuseObservations.length === 0}>Export loaded CSV</button>
        </div>
      </div>
      <div className="ac-note">
        Observation metadata is shown only when available. Prompt contents,
        generated answers, secrets, token costs, and other unreturned fields
        are not fabricated.
      </div>
<div className="ac-section-title"><div><h3>Application traces</h3><p>Trace-like records derived from persisted chat audit events.</p></div><span className="ac-chip">{traces.length} events</span></div><TraceTable rows={visibleTraces}/>
<div className="ac-pagination">
  <span>Showing {filteredTraces.length ? (safeTracePage - 1) * ADMIN_TRACE_PAGE_SIZE + 1 : 0}–{Math.min(safeTracePage * ADMIN_TRACE_PAGE_SIZE, filteredTraces.length)} of {filteredTraces.length} filtered application traces</span>
  <div>
    <button className="ac-secondary" disabled={safeTracePage <= 1} onClick={() => setTracePage(p => Math.max(1, p - 1))}>Previous</button>
    <span className="ac-page-number">Page {safeTracePage} of {tracePageCount}</span>
    <button className="ac-secondary" disabled={safeTracePage >= tracePageCount} onClick={() => setTracePage(p => Math.min(tracePageCount, p + 1))}>Next</button>
    <button className="ac-secondary" onClick={exportApplicationTracesCsv} disabled={filteredTraces.length === 0}>Download CSV</button>
  </div>
</div>
<div className="ac-note">Langfuse spans, nested tool calls, token counts and model cost will appear here only when the backend sends and stores that data. The current implementation does not invent those fields.</div></>}
    {section==='conversations'&&<><div className="ac-section-title"><div><h3>All conversations</h3><p>Administrators can inspect conversation history and delete records when required. Every deletion is audited.</p></div><span className="ac-chip">{conversations.length} conversations</span></div><div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>Conversation</th><th>User</th><th>Messages</th><th>Created</th><th>Actions</th></tr></thead><tbody>{visibleConversations.map(c=><tr key={c.id}><td>{c.title||'New chat'}</td><td>{c.user_email}</td><td>{c.message_count}</td><td>{c.created_at?new Date(c.created_at).toLocaleString():'—'}</td><td><div className="ac-row-actions"><button className="ac-secondary" onClick={()=>viewConversation(c.id)}><Eye size={14}/> Read</button><button className="ac-danger" onClick={()=>deleteConversation(c)}><Trash2 size={14}/> Delete</button></div></td></tr>)}</tbody></table>{filteredConversations.length===0&&<div className="ac-empty">No matching conversations.</div>}</div>
    <div className="ac-pagination">
      <span>Showing {filteredConversations.length ? (safeConversationPage - 1) * CONVERSATION_PAGE_SIZE + 1 : 0}–{Math.min(safeConversationPage * CONVERSATION_PAGE_SIZE, filteredConversations.length)} of {filteredConversations.length} filtered conversations</span>
      <div>
        <button className="ac-secondary" disabled={safeConversationPage <= 1} onClick={() => setConversationPage(p => Math.max(1, p - 1))}>Previous</button>
        <span className="ac-page-number">Page {safeConversationPage} of {conversationPageCount}</span>
        <button className="ac-secondary" disabled={safeConversationPage >= conversationPageCount} onClick={() => setConversationPage(p => Math.min(conversationPageCount, p + 1))}>Next</button>
        <button className="ac-secondary" onClick={exportAdminConversationsCsv} disabled={filteredConversations.length === 0}>Download CSV</button>
      </div>
    </div>
    {conversationDetail&&<div className="ac-modal-backdrop" onClick={()=>setConversationDetail(null)}><div className="ac-modal" onClick={e=>e.stopPropagation()}><div className="ac-modal-head"><div><h3>{conversationDetail.title}</h3><p>{conversationDetail.user_email} · read-only history view</p></div><button className="ac-secondary" onClick={()=>setConversationDetail(null)} aria-label="Close"><X size={15}/></button></div><div className="ac-modal-messages">{conversationDetail.messages.map(m=><article className={`ac-chat-message ${m.role}`} key={m.id}><div><b>{m.role}</b><small>{m.created_at?new Date(m.created_at).toLocaleString():''}</small></div><p>{m.content}</p>{m.sources?.length>0&&<small>Sources: {m.sources.map(s=>s.filename||s.document_id||'source').join(', ')}</small>}</article>)}</div></div></div>}</>}
    {section==='documents'&&<><div className="ac-section-title"><div><h3>All documents</h3><p>Upload and delete actions are protected by admin-only backend routes.</p></div><label className="ac-upload">Upload PDF / TXT / DOCX<input type="file" accept=".pdf,.txt,.docx" onChange={async e=>{const f=e.target.files?.[0];if(!f)return;const form=new FormData();form.append('file',f);try{const result=await request('/api/documents',{method:'POST',body:form});onNotice?.(`${result.filename}: ${result.status}, ${result.chunk_count} chunks`);await load()}catch(err){onNotice?.(err.message)}e.target.value=''}}/></label></div><div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>Document</th><th>Owner</th><th>Status</th><th>Chunks</th><th>Created</th><th>Action</th></tr></thead><tbody>{filteredDocs.map(d=><tr key={d.id}><td><FileText size={15}/><span>{d.filename}</span></td><td>{d.owner_email||d.owner_user_id||'—'}</td><td><span className={`ac-status ${d.status}`}>{d.status}</span></td><td>{d.chunk_count}</td><td>{d.created_at?new Date(d.created_at).toLocaleString():'—'}</td><td><button className="ac-danger" onClick={()=>deleteDocument(d)} title="Delete document"><Trash2 size={15}/> Delete</button></td></tr>)}</tbody></table>{filteredDocs.length===0&&<div className="ac-empty">No matching documents.</div>}</div></>}
    {section==='users'&&<><div className="ac-section-title"><div><h3>User administration</h3><p>Manage application roles. Changes are audited and enforced by FastAPI.</p></div><span className="ac-chip">{users.length} accounts</span></div><div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>User</th><th>Role</th><th>Verified</th><th>Created</th><th>Action</th></tr></thead><tbody>{filteredUsers.map(u=><tr key={u.id}><td><Users size={15}/><span>{u.email}</span></td><td><span className={`ac-status ${u.role}`}>{u.role}</span></td><td>{u.is_verified?'Yes':'No'}</td><td>{u.created_at?new Date(u.created_at).toLocaleDateString():'—'}</td><td><button className="ac-secondary" onClick={()=>changeRole(u)}>{u.role==='admin'?'Make user':'Make admin'}</button></td></tr>)}</tbody></table>{filteredUsers.length===0&&<div className="ac-empty">No matching users.</div>}</div><div className="ac-note">You cannot change your own role through this screen. Keep at least one trusted administrator account.</div></>}
    {section==='analytics'&&<><div className="ac-stats">{[['Total messages',metrics?.messages],['Total users',metrics?.users],['Total documents',metrics?.documents],['Ready documents',metrics?.ready_documents],['Indexed chunks',metrics?.chunks_indexed],['Failed documents',metrics?.failed_documents]].map(([label,value])=><div className="ac-stat" key={label}><span>{label}</span><b>{value??'—'}</b><small>Current database aggregate</small></div>)}</div><div className="ac-section-title"><div><h3>Recent request latency</h3><p>Latency recorded in chat.completed audit events; only available when present in the event.</p></div></div><div className="ac-bars">{visibleLatency.map((t,i)=><div className="ac-bar-row" key={t.id}><span title={t.query||t.id}>{(t.query||t.id).slice(0,48)}</span><div className="ac-bar-track"><div style={{width:`${Math.max(2,Number(t.latency_ms||0)/maxDuration*100)}%`}}/></div><b>{t.latency_ms==null?'—':`${t.latency_ms} ms`}</b></div>)}</div>
<div className="ac-pagination">
  <span>Showing {filteredTraces.length ? (safeLatencyPage - 1) * ADMIN_TRACE_PAGE_SIZE + 1 : 0}–{Math.min(safeLatencyPage * ADMIN_TRACE_PAGE_SIZE, filteredTraces.length)} of {filteredTraces.length} filtered latency events</span>
  <div>
    <button className="ac-secondary" disabled={safeLatencyPage <= 1} onClick={() => setLatencyPage(p => Math.max(1, p - 1))}>Previous</button>
    <span className="ac-page-number">Page {safeLatencyPage} of {latencyPageCount}</span>
    <button className="ac-secondary" disabled={safeLatencyPage >= latencyPageCount} onClick={() => setLatencyPage(p => Math.min(latencyPageCount, p + 1))}>Next</button>
    <button className="ac-secondary" onClick={() => downloadAdminCsv('request-latency-loaded.csv', ['Event ID','Query','Action','Status','Latency ms','Created'], filteredTraces.map(t => [t.id,t.query,t.action,t.status,t.latency_ms,t.created_at]))} disabled={!filteredTraces.length}>Download CSV</button>
  </div>
</div>
<div className="ac-note">Historical token/cost analytics and retrieval-quality evaluation are not present in current persisted event data, so they are not displayed as if measured.</div></>}
    {section==='agents'&&<AgentMonitoring request={request} token={token} onNotice={onNotice}/>}
    {section==='settings'&&<><div className="ac-section-title"><div><h3>Runtime configuration</h3><p>Read-only, safe configuration summary. Secrets are never sent to this page.</p></div><span className="ac-chip">Protected</span></div><div className="ac-settings-grid">{Object.entries(settings||{}).map(([key,value])=><div className="ac-setting" key={key}><span>{key.replaceAll('_',' ')}</span><b>{String(value)}</b></div>)}</div><div className="ac-note">Model credentials, JWT secrets, database passwords and provider API keys are intentionally excluded. Runtime-changing controls should be added only alongside validated backend configuration and audit handling.</div></>}
  </section>
}

function TraceTable({rows}) {
  return <div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>Event ID</th><th>User query</th><th>Status</th><th>Latency</th><th>Timestamp</th></tr></thead><tbody>{rows.map(t=><tr key={t.id}><td><code>{t.id}</code></td><td>{t.query||t.action||'Chat event'}</td><td><span className={`ac-status ${t.status||'completed'}`}>{t.status||'completed'}</span></td><td>{t.latency_ms==null?'—':`${t.latency_ms} ms`}</td><td>{t.created_at?new Date(t.created_at).toLocaleString():'—'}</td></tr>)}</tbody></table>{rows.length===0&&<div className="ac-empty"><CheckCircle2 size={18}/> No trace events found in the selected records.</div>}</div>
}

export function AgentMonitoring({request, token, onNotice}) {
  const [tasks, setTasks] = useState([])
  const [reports, setReports] = useState([])
  const [cycles, setCycles] = useState([])
  const [cyclePage, setCyclePage] = useState(1)
  const REPORTING_CYCLE_PAGE_SIZE = 15
  const [emailStatuses, setEmailStatuses] = useState({})
  const [agentType, setAgentType] = useState('daily_report')
  const [busy, setBusy] = useState(false)
  const [loadingReporting, setLoadingReporting] = useState(false)
  const [executionMode, setExecutionMode] = useState('')
  const [scheduleEnabled, setScheduleEnabled] = useState(false)

  const refreshTasks = useCallback(async () => {
    try {
      const [data, reportData, cycleData, dailyData] = await Promise.all([
        request('/api/admin/agent-tasks'),
        request('/api/admin/agent-tasks/reports'),
        request('/api/admin/agent-tasks/reporting/cycles'),
        request('/api/admin/agent-tasks/reporting/reports')
      ])

      setTasks(data.tasks || [])
      setExecutionMode(data.execution_mode || '')
      setScheduleEnabled(Boolean(data.schedule_enabled))

      // Keep the existing legacy task report history.
      setReports(reportData.reports || [])

      // New consolidated reporting data.
      setCycles(cycleData.cycles || [])
      const dailyReports = dailyData.reports || []

      const statuses = {}
      await Promise.all(dailyReports.map(async report => {
        try {
          const result = await request(
            `/api/admin/agent-tasks/reporting/email/${encodeURIComponent(report.report_date)}`
          )
          statuses[report.report_date] = result.deliveries || []
        } catch (err) {
          statuses[report.report_date] = [{status: 'unavailable', error: err.message}]
        }
      }))

      setEmailStatuses(statuses)
      setDailyReportsState(dailyReports)
    } catch (err) {
      onNotice?.(`Reporting refresh: ${err.message}`)
    }
  }, [request, onNotice])

  const [dailyReports, setDailyReportsState] = useState([])

  useEffect(() => {
    refreshTasks()
    const timer = setInterval(refreshTasks, 15000)
    return () => clearInterval(timer)
  }, [refreshTasks])

  const startTask = async () => {
    setBusy(true)
    try {
      await request('/api/admin/agent-tasks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({agent_type: agentType, params: {}})
      })
      onNotice?.(`Started ${agentType.replaceAll('_', ' ')} task`)
      await refreshTasks()
    } catch (err) {
      onNotice?.(err.message)
    } finally {
      setBusy(false)
    }
  }

  const downloadConsolidatedReport = async (report, format) => {
    try {
      const url =
        `${import.meta.env.VITE_API_URL || ''}` +
        `/api/admin/agent-tasks/reporting/reports/` +
        `${encodeURIComponent(report.report_date)}/download/${format}`

      const response = await fetch(url, {
        headers: {Authorization: `Bearer ${token}`}
      })

      if (!response.ok) {
        const body = await response.json().catch(() => ({detail: 'Download failed'}))
        throw new Error(body.detail || `Download failed (${response.status})`)
      }

      const blob = await response.blob()
      const objectUrl = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = objectUrl
      link.download = `daily-report-${report.report_date}.${format}`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(objectUrl)
      onNotice?.(`${format.toUpperCase()} report downloaded.`)
    } catch (err) {
      onNotice?.(`Report download failed: ${err.message}`)
    }
  }

  const downloadLegacyReport = async report => {
    try {
      const response = await fetch(
        `${import.meta.env.VITE_API_URL || ''}/api/admin/agent-tasks/reports/${encodeURIComponent(report.id)}/download`,
        {headers: {Authorization: `Bearer ${token}`}}
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({detail: 'Download failed'}))
        throw new Error(body.detail || `Download failed (${response.status})`)
      }
      const blob = await response.blob()
      const objectUrl = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = objectUrl
      link.download = `advanced-rag-${report.agent_type}-${report.id}.json`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(objectUrl)
      onNotice?.('Legacy report downloaded.')
    } catch (err) {
      onNotice?.(`Report download failed: ${err.message}`)
    }
  }

  const act = async (task, action) => {
    const wording = action === 'cancel' ? 'Cancel' : action === 'delete' ? 'Delete' : action === 'pause' ? 'Pause' : 'Resume'
    if (!window.confirm(`${wording} task ${task.id}?`)) return
    try {
      if (action === 'delete') {
        await request(`/api/admin/agent-tasks/${task.id}`, {method: 'DELETE'})
      } else {
        await request(`/api/admin/agent-tasks/${task.id}/${action}`, {method: 'POST'})
      }
      onNotice?.(`${wording} request submitted`)
      await refreshTasks()
    } catch (err) {
      onNotice?.(err.message)
    }
  }

  const agentNames = [
    'api_monitor',
    'infrastructure_agent',
    'log_agent',
    'langfuse_agent'
  ]

  const cyclePageCount = Math.max(1, Math.ceil(cycles.length / REPORTING_CYCLE_PAGE_SIZE))
  const safeCyclePage = Math.min(cyclePage, cyclePageCount)
  const visibleCycles = cycles.slice(
    (safeCyclePage - 1) * REPORTING_CYCLE_PAGE_SIZE,
    safeCyclePage * REPORTING_CYCLE_PAGE_SIZE
  )

  const exportReportingCyclesCsv = () => {
    const headers = [
      'Cycle ID', 'Cycle Key', 'Cycle Status',
      'Window Start', 'Window End', 'Agent',
      'Agent Status', 'Attempts', 'Error',
      'Same Reporting Cycle'
    ]
    const rows = cycles.flatMap(cycle => {
      const agents = cycle.agents || []
      const cycleAgents = agents.length ? agents : agentNames.map(name => ({
        agent_type: name, status: 'missing', attempts: 0
      }))
      return cycleAgents.map(agent => [
        cycle.id, cycle.cycle_key, cycle.status,
        cycle.window_start, cycle.window_end,
        agent.agent_type, agent.status, agent.attempts ?? 0,
        agent.error_summary,
        cycle.validation?.same_reporting_cycle
      ])
    })
    const csvCell = value => {
      const text = value == null ? '' : String(value)
      return '"' + text.replaceAll('"', '""') + '"'
    }
    const csv = [headers, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n')
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'})
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'reporting-cycles-loaded.csv'
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    onNotice?.(`Exported ${cycles.length} loaded reporting cycles.`)
  }

  const completedCycles = dailyReports.reduce(
    (sum, report) => sum + Number(report.validation?.completed_cycles || 0), 0
  )
  const missingCycles = dailyReports.reduce(
    (sum, report) => sum + Number(report.validation?.missing_cycles || 0), 0
  )

  return <div className="ac-agent-monitoring">
    <div className="ac-section-title">
      <div>
        <h3>Agent Monitoring</h3>
        <p>Four-agent monitoring, consolidated reporting and execution controls.</p>
      </div>
      <button className="ac-secondary" onClick={refreshTasks}>
        <RefreshCw size={14}/> Refresh
      </button>
    </div>

    <div className="ac-note">
      Execution mode: {executionMode || 'loading'} · Existing daily schedule: {scheduleEnabled ? 'enabled' : 'not configured'}.
      The separate 15-minute reporting Beat is configured but intentionally stopped during testing.
    </div>

    <div className="ac-row-actions" style={{margin: '12px 0'}}>
      <select value={agentType} onChange={e => setAgentType(e.target.value)}>
        <option value="daily_report">Daily report (run now)</option>
        <option value="api_monitor">API health monitor</option>
        <option value="log_analysis">Monitoring log summary</option>
        <option value="langfuse_report">Langfuse observations report</option>
      </select>
      <button className="ac-primary" disabled={busy} onClick={startTask}>
        {busy ? 'Starting…' : 'Run agent'}
      </button>
    </div>

    <div className="ac-stats">
      <div className="ac-stat">
        <span>Reporting cycles loaded</span>
        <b>{cycles.length}</b>
        <small>Persisted 15-minute cycles</small>
      </div>
      <div className="ac-stat">
        <span>Daily cycles completed</span>
        <b>{completedCycles}</b>
        <small>Across displayed daily reports</small>
      </div>
      <div className="ac-stat">
        <span>Daily cycles missing</span>
        <b>{missingCycles}</b>
        <small>Across displayed daily reports</small>
      </div>
    </div>

    <div className="ac-section-title">
      <div>
        <h3>Consolidated reporting cycles</h3>
        <p>Cycle validation and execution status for all four agents.</p>
      </div>
      <span className="ac-chip">{cycles.length} cycles</span>
    </div>

    <div className="ac-table-wrap">
      <table className="ac-table">
        <thead>
          <tr>
            <th>Reporting window</th>
            <th>Cycle status</th>
            <th>Agent</th>
            <th>Agent status</th>
            <th>Attempts</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {visibleCycles.flatMap(cycle => {
            const agents = cycle.agents || []
            return (agents.length ? agents : agentNames.map(name => ({
              agent_type: name, status: 'missing', attempts: 0
            }))).map(agent => (
              <tr key={`${cycle.id}-${agent.agent_type}`}>
                <td>
                  <strong>{cycle.cycle_key}</strong>
                  <br/>
                  <small>
                    {cycle.window_start ? new Date(cycle.window_start).toLocaleString() : '—'}
                    {' – '}
                    {cycle.window_end ? new Date(cycle.window_end).toLocaleString() : '—'}
                  </small>
                </td>
                <td>
                  <span className={`ac-status ${cycle.status}`}>
                    {cycle.status}
                  </span>
                  {cycle.validation?.same_reporting_cycle === false &&
                    <small style={{display:'block'}}>Cycle mismatch</small>}
                </td>
                <td>{agent.agent_type.replaceAll('_', ' ')}</td>
                <td>
                  <span className={`ac-status ${agent.status}`}>
                    {agent.status}
                  </span>
                </td>
                <td>{agent.attempts ?? 0}</td>
                <td>{agent.error_summary || '—'}</td>
              </tr>
            ))
          })}
        </tbody>
      </table>
      <div className="ac-pagination">
        <span>
          Showing {cycles.length ? (safeCyclePage - 1) * REPORTING_CYCLE_PAGE_SIZE + 1 : 0}–{Math.min(safeCyclePage * REPORTING_CYCLE_PAGE_SIZE, cycles.length)} of {cycles.length} loaded reporting cycles
        </span>
        <div>
          <button
            className="ac-secondary"
            disabled={safeCyclePage <= 1}
            onClick={() => setCyclePage(page => Math.max(1, page - 1))}
          >Previous</button>
          <span className="ac-page-number">Page {safeCyclePage} of {cyclePageCount}</span>
          <button
            className="ac-secondary"
            disabled={safeCyclePage >= cyclePageCount}
            onClick={() => setCyclePage(page => Math.min(cyclePageCount, page + 1))}
          >Next</button>
          <button
            className="ac-secondary"
            onClick={exportReportingCyclesCsv}
            disabled={!cycles.length}
          >Download CSV</button>
        </div>
      </div>
      {cycles.length === 0 &&
        <div className="ac-empty">No consolidated reporting cycles returned by the API.</div>}
    </div>

    <div className="ac-section-title" style={{marginTop:24}}>
      <div>
        <h3>Daily consolidated reports</h3>
        <p>HTML, Excel and JSON files generated from the same reporting dataset.</p>
      </div>
      <span className="ac-chip">{dailyReports.length} reports</span>
    </div>

    <div className="ac-table-wrap">
      <table className="ac-table">
        <thead>
          <tr>
            <th>Report date</th>
            <th>Status</th>
            <th>Expected</th>
            <th>Observed</th>
            <th>Missing</th>
            <th>Validation</th>
            <th>Downloads</th>
          </tr>
        </thead>
        <tbody>
          {dailyReports.map(report => {
            const validation = report.validation || {}
            return <tr key={report.id}>
              <td>
                <strong>{report.report_date}</strong>
                <br/>
                <small>{report.generated_at ? new Date(report.generated_at).toLocaleString() : '—'}</small>
              </td>
              <td>
                <span className={`ac-status ${report.status}`}>{report.status}</span>
              </td>
              <td>{validation.expected_cycles ?? '—'}</td>
              <td>{validation.observed_cycles ?? '—'}</td>
              <td>{validation.missing_cycles ?? '—'}</td>
              <td>
                <span className={`ac-status ${validation.all_expected_cycles_present && validation.all_observed_cycles_completed ? 'completed' : 'warning'}`}>
                  {validation.all_expected_cycles_present && validation.all_observed_cycles_completed ? 'complete' : 'incomplete'}
                </span>
              </td>
              <td>
                <div className="ac-row-actions">
                  {['html', 'xlsx', 'json'].map(format =>
                    <button
                      key={format}
                      className="ac-secondary"
                      onClick={() => downloadConsolidatedReport(report, format)}
                    >
                      {format.toUpperCase()}
                    </button>
                  )}
                </div>
              </td>
            </tr>
          })}
        </tbody>
      </table>
      {dailyReports.length === 0 &&
        <div className="ac-empty">No consolidated daily reports found.</div>}
    </div>

    <div className="ac-section-title" style={{marginTop:24}}>
      <div>
        <h3>Email delivery history</h3>
        <p>Delivery records are read-only. No email is sent from this dashboard.</p>
      </div>
    </div>

    <div className="ac-table-wrap">
      <table className="ac-table">
        <thead>
          <tr>
            <th>Report date</th>
            <th>Recipient</th>
            <th>Delivery status</th>
            <th>Attempts</th>
            <th>Last error</th>
          </tr>
        </thead>
        <tbody>
          {dailyReports.flatMap(report => {
            const deliveries = emailStatuses[report.report_date] || []
            return (deliveries.length ? deliveries : [{
              status: 'not_created',
              recipient: '—',
              attempts: 0
            }]).map((delivery, index) => (
              <tr key={`${report.report_date}-${index}`}>
                <td>{report.report_date}</td>
                <td>{delivery.recipient || '—'}</td>
                <td>
                  <span className={`ac-status ${delivery.status}`}>
                    {delivery.status}
                  </span>
                </td>
                <td>{delivery.attempts ?? 0}</td>
                <td>{delivery.last_error || delivery.error || '—'}</td>
              </tr>
            ))
          })}
        </tbody>
      </table>
      {dailyReports.length === 0 &&
        <div className="ac-empty">Email delivery history will appear when daily reports are available.</div>}
    </div>

    <div className="ac-section-title" style={{marginTop:24}}>
      <div>
        <h3>Individual agent tasks</h3>
        <p>Existing task controls, execution logs and results are preserved.</p>
      </div>
      <span className="ac-chip">{tasks.length} tasks</span>
    </div>

    <div className="ac-table-wrap">
      <table className="ac-table">
        <thead>
          <tr>
            <th>Agent / task</th><th>Status</th><th>Progress</th>
            <th>Created</th><th>Finished</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map(task => <tr key={task.id}>
            <td>
              <strong>{task.agent_type.replaceAll('_', ' ')}</strong>
              <br/><code>{task.id}</code>
              {task.error_summary &&
                <small style={{display:'block', color:'var(--danger, #b91c1c)'}}>
                  {task.error_summary}
                </small>}
            </td>
            <td><span className={`ac-status ${task.status}`}>{task.status}</span></td>
            <td>{task.progress ?? 0}%</td>
            <td>{task.created_at ? new Date(task.created_at).toLocaleString() : '—'}</td>
            <td>{task.finished_at ? new Date(task.finished_at).toLocaleString() : '—'}</td>
            <td>
              <div className="ac-row-actions">
                {task.status === 'running' &&
                  <button className="ac-secondary" onClick={() => act(task, 'pause')}>Pause</button>}
                {task.status === 'paused' &&
                  <button className="ac-secondary" onClick={() => act(task, 'resume')}>Resume</button>}
                {['pending','running','paused'].includes(task.status) &&
                  <button className="ac-danger" onClick={() => act(task, 'cancel')}>Cancel</button>}
                {['completed','failed','cancelled'].includes(task.status) &&
                  <button className="ac-danger" onClick={() => act(task, 'delete')}>Delete</button>}
              </div>
            </td>
          </tr>)}
        </tbody>
      </table>
      {tasks.length === 0 &&
        <div className="ac-empty">No individual agent tasks yet.</div>}
    </div>

    {tasks.filter(t => t.result || (t.logs || []).length).slice(0, 10).map(task =>
      <details key={`${task.id}-details`} style={{marginTop:12}}>
        <summary>{task.agent_type.replaceAll('_',' ')} · execution details [{task.id}]</summary>
        <h4>Logs</h4>
        <pre style={{whiteSpace:'pre-wrap', overflowWrap:'anywhere'}}>
          {(task.logs || []).map(l =>
            `[${l.at}] ${String(l.level || 'info').toUpperCase()}: ${l.message}`
          ).join('\n') || 'No logs yet.'}
        </pre>
        {task.result && <>
          <h4>Result</h4>
          <pre style={{whiteSpace:'pre-wrap', overflowWrap:'anywhere'}}>
            {JSON.stringify(task.result, null, 2)}
          </pre>
        </>}
      </details>
    )}

    <div className="ac-section-title" style={{marginTop:24}}>
      <div>
        <h3>Legacy report history</h3>
        <p>Previously saved individual agent reports.</p>
      </div>
      <span className="ac-chip">{reports.length} reports</span>
    </div>

    <div className="ac-table-wrap">
      <table className="ac-table">
        <thead>
          <tr><th>Report</th><th>Status</th><th>Generated</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {reports.map(report => <tr key={`report-${report.id}`}>
            <td>
              <strong>{report.agent_type?.replaceAll('_', ' ') || 'Agent report'}</strong>
              <br/><code>{report.id}</code>
            </td>
            <td><span className={`ac-status ${report.status}`}>{report.status}</span></td>
            <td>
              {report.finished_at ? new Date(report.finished_at).toLocaleString() :
                report.created_at ? new Date(report.created_at).toLocaleString() : '—'}
            </td>
            <td>
              <button className="ac-secondary" disabled={!report.result}
                onClick={() => downloadLegacyReport(report)}>
                Download JSON
              </button>
            </td>
          </tr>)}
        </tbody>
      </table>
      {reports.length === 0 &&
        <div className="ac-empty">No legacy report runs found.</div>}
    </div>
  </div>
}
