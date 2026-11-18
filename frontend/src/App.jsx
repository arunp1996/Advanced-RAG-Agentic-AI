import React, {useEffect, useState} from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import AdminConsole from './AdminConsole.jsx'
import {Brain, MessageSquare, History, Plus, Upload, ShieldCheck, Settings, LogOut, Send, FileText, ThumbsUp, ThumbsDown, Sparkles, Database, Activity, Trash2, RefreshCw, Copy} from 'lucide-react'


async function copyText(text) {
  const value = String(text ?? '')
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return true
    } catch (_) {}
  }

  const area = document.createElement('textarea')
  area.value = value
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  const copied = document.execCommand('copy')
  area.remove()
  return copied
}

function CopyableCodeBlock({children}) {
  const codeText = React.Children.toArray(children).map(child => {
    if (typeof child === 'string') return child
    return child?.props?.children ?? ''
  }).join('')

  return (
    <div className="chat-code-card">
      <div className="chat-code-toolbar">
        <span>CODE</span>
        <button type="button" onClick={() => copyText(codeText)} title="Copy code">
          <Copy size={13}/> Copy code
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  )
}

const API = import.meta.env.VITE_API_URL || ''
function App(){
  const [token,setToken] = useState(localStorage.getItem('rag_token')||'')
  const [me,setMe] = useState(null)
  const [email,setEmail] = useState('')
  const [password,setPassword] = useState('')
  const [mode,setMode] = useState('login')
  const [tab,setTab] = useState('chat')
  const [question,setQuestion] = useState('')
  const [messages,setMessages] = useState([])
  const [conversations,setConversations] = useState([])
  const [historySearch,setHistorySearch] = useState('')
  const [historyPage,setHistoryPage] = useState(1)
  const HISTORY_PAGE_SIZE = 15
  const [conversationId,setConversationId] = useState(null)
  const [documents,setDocuments] = useState([])
  const [metrics,setMetrics] = useState(null)
  const [memory,setMemory] = useState('')
  const [pipeline,setPipeline] = useState(null)
  const [debugQuery,setDebugQuery] = useState('')
  const [debugResults,setDebugResults] = useState(null)
  const [busy,setBusy] = useState(false)
  const [notice,setNotice] = useState('')
  const headers = {'Authorization':`Bearer ${token}`}
  async function request(path, opts={}) {
    const res = await fetch(`${API}${path}`, {...opts, headers:{...headers,...(opts.headers||{})}})
    const data = await res.json().catch(()=>({detail:'Unexpected server response'}))
    if(!res.ok) throw new Error(data.detail || 'Request failed')
    return data
  }
  async function refresh(){
    try { const u=await request('/api/me'); setMe(u); if(u.role==='admin'){ const d=await request('/api/documents'); setDocuments(d) } else { setDocuments([]) } }
    catch(e){setNotice(e.message)}
  }
  useEffect(()=>{if(token) refresh()},[token])
  async function auth(e){
    e.preventDefault(); setNotice('')
    try{
      if(mode==='register'){const r=await fetch(`${API}/api/auth/register`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})}); const d=await r.json(); if(!r.ok) throw new Error(d.detail); setNotice('Account created. Sign in to continue.');setMode('login');return}
      const d=await fetch(`${API}/api/auth/login`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})}).then(async r=>{const x=await r.json();if(!r.ok)throw new Error(x.detail);return x})
      localStorage.setItem('rag_token',d.access_token);setToken(d.access_token);setMe(d.user);setNotice('')
    }catch(e){setNotice(e.message)}
  }
  function logout(){localStorage.removeItem('rag_token');setToken('');setMe(null);setMessages([]);setConversationId(null)}
  const filteredHistory = conversations.filter(c =>
    `${c.title || ''} ${c.id || ''}`.toLowerCase().includes(historySearch.toLowerCase())
  )
  const historyPages = Math.max(1, Math.ceil(filteredHistory.length / HISTORY_PAGE_SIZE))
  const visibleHistory = filteredHistory.slice(
    (historyPage - 1) * HISTORY_PAGE_SIZE,
    historyPage * HISTORY_PAGE_SIZE
  )

  function downloadHistoryCsv() {
    const columns = ['Conversation ID', 'Title', 'Created']
    const rows = filteredHistory.map(c => [c.id, c.title || 'New chat', c.created_at || ''])
    const csvCell = value => `"${String(value ?? '').replace(/"/g, '""')}"`
    const csv = [columns, ...rows].map(row => row.map(csvCell).join(',')).join('\\r\\n')
    const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8;'}))
    const link = document.createElement('a')
    link.href = url
    link.download = 'user-chat-history.csv'
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    setNotice(`Exported ${filteredHistory.length} loaded conversations to CSV.`)
  }

  async function openHistory(){
    setHistoryPage(1)
    setHistorySearch('')
    setTab('history')
    try {
      setConversations(await request('/api/conversations'))
    } catch(e) {
      setNotice(e.message)
    }
  }

  async function openConversation(id){
    try {
      const rows = await request(`/api/conversations/${id}`)
      setConversationId(id)
      setMessages(rows.map(m => ({
        role: m.role,
        content: m.content,
        sources: m.sources || [],
        message_id: m.id
      })))
      setTab('chat')
      setNotice('')
    } catch(e) {
      setNotice(e.message)
    }
  }

  function newChat(){
    setConversationId(null)
    setMessages([])
    setQuestion('')
    setTab('chat')
    setNotice('')
  }

  async function send(){
    if(!question.trim()||busy)return
    const q=question;setQuestion('');setMessages(m=>[...m,{role:'user',content:q}]);setBusy(true)
    try{const d=await request('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,conversation_id:conversationId})});setConversationId(d.conversation_id);setMessages(m=>[...m,{role:'assistant',content:d.answer,sources:d.sources,message_id:d.message_id,latency_ms:d.latency_ms}])}
    catch(e){setMessages(m=>[...m,{role:'assistant',content:`Error: ${e.message}`}])}finally{setBusy(false)}
  }
  async function upload(e){
    const file=e.target.files?.[0];if(!file)return
    const form=new FormData();form.append('file',file)
    try{const d=await request('/api/documents',{method:'POST',body:form});setNotice(`${d.filename}: ${d.status}, ${d.chunk_count} chunks`);setDocuments(await request('/api/documents'))}
    catch(err){setNotice(err.message)} e.target.value=''
  }
  async function openMemory(){setTab('memory');try{const d=await request('/api/memory');setMemory(d.content||'')}catch(e){setNotice(e.message)}}
  async function saveMemory(){try{await request('/api/memory',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:memory})});setNotice('Personal memory saved securely for this account.')}catch(e){setNotice(e.message)}}
  async function openPipeline(){setTab('pipeline');try{setPipeline(await request('/api/pipeline/metrics'))}catch(e){setNotice(e.message)}}
  async function runDebug(){try{setDebugResults(await request('/api/retrieval/debug',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:debugQuery})}))}catch(e){setNotice(e.message)}}
  async function openAdmin(){
    setTab('admin')
    try{
      const results = await Promise.all([
        request('/api/admin/metrics'),
        request('/api/admin/documents')
      ])
      setMetrics(results[0])
      setDocuments(results[1])
      setNotice('')
    }catch(e){setNotice(e.message)}
  }

  async function deleteAdminDocument(document){
    const confirmed = window.confirm(
      `Delete "${document.filename}"?\n\nThis permanently removes this document, its indexed chunks, and its sharing records. This action cannot be undone.`
    )
    if(!confirmed) return

    try{
      await request(`/api/admin/documents/${encodeURIComponent(document.id)}`, {
        method: 'DELETE'
      })
      setDocuments(current => current.filter(item => item.id !== document.id))
      setNotice(`Deleted "${document.filename}" successfully.`)
    }catch(e){
      setNotice(`Could not delete "${document.filename}": ${e.message}`)
    }
  }
  async function rate(message_id,rating){
    try{await request('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id,rating})});setNotice('Feedback saved')}
    catch(e){setNotice(e.message)}
  }
  if(!token||!me) return <main className="auth-wrap"><div className="auth-card"><div className="brand"><div className="brand-icon"><Brain/></div><div><h1>Advanced RAG AI</h1><p>Your knowledge, connected.</p></div></div><h2>{mode==='login'?'Welcome back':'Create your account'}</h2><form onSubmit={auth}><label>Email</label><input type="email" value={email} onChange={e=>setEmail(e.target.value)} required/><label>Password</label><input type="password" minLength="10" value={password} onChange={e=>setPassword(e.target.value)} required/><button className="primary full">{mode==='login'?'Sign in':'Register'}</button></form>{notice&&<p className="notice">{notice}</p>}<p className="switch">{mode==='login'?'New here?':'Already registered?'} <button onClick={()=>setMode(mode==='login'?'register':'login')}>{mode==='login'?'Create account':'Sign in'}</button></p><small>Starter build: email verification and password reset must be configured before production.</small></div></main>
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><div className="brand-icon"><Brain/></div><div><h1>RAG AI</h1><p>Knowledge workspace</p></div></div><button className={`nav ${tab==='chat'?'active':''}`} onClick={()=>setTab('chat')}><MessageSquare size={18}/> Chat</button><button className={`nav ${tab==='history'?'active':''}`} onClick={openHistory}><History size={18}/> Chat History</button>{me.role==='admin'&&<>
<button className={`nav ${tab==='admin'?'active':''}`} onClick={openAdmin}><ShieldCheck size={18}/> Admin workspace</button>
<button className={`nav ${tab==='memory'?'active':''}`} onClick={openMemory}><Brain size={18}/> Personal memory</button>
</>}<div className="side-bottom"><div className="user-chip"><div className="avatar">{me.email[0].toUpperCase()}</div><div className="user-text"><b>{me.email}</b><small>{me.role}</small></div><button title="Sign out" className="icon-btn" onClick={logout}><LogOut size={17}/></button></div><small className="muted">Advanced RAG · Starter</small></div></aside><main className="main-panel"><header className="topbar"><div><span className="status-dot"></span> Workspace ready</div><div className="top-actions"><span className="pill"><Sparkles size={14}/> AI assistant</span></div></header>
  {notice&&<div className="notice-banner">{notice}<button onClick={()=>setNotice('')}>Dismiss</button></div>}
  {tab==='history'&&<section className="page">
    <div className="page-title">
      <div>
        <div className="eyebrow">YOUR CONVERSATIONS</div>
        <h2>Chat History</h2>
        <p>Reopen a previous conversation or start a new one.</p>
      </div>
      <button className="primary" onClick={newChat}><Plus size={17}/> New chat</button>
    </div>
    <div className="history-toolbar">
      <input
        type="search"
        value={historySearch}
        onChange={e => {setHistorySearch(e.target.value); setHistoryPage(1)}}
        placeholder="Search your conversations..."
        aria-label="Search chat history"
      />
      <button className="secondary" onClick={downloadHistoryCsv} disabled={!filteredHistory.length}>
        Download CSV
      </button>
      <span>{filteredHistory.length} conversations</span>
    </div>
    <div className="doc-list">
      {visibleHistory.map(c=><button
        className="doc-card"
        key={c.id}
        onClick={()=>openConversation(c.id)}
        style={{width:'100%',textAlign:'left',cursor:'pointer'}}
      >
        <MessageSquare/>
        <div className="doc-info">
          <b>{c.title || 'New chat'}</b>
          <small>{c.created_at ? new Date(c.created_at).toLocaleString() : 'Saved conversation'}</small>
        </div>
      </button>)}
      {filteredHistory.length===0&&<div className="empty">No matching conversations found.</div>}
    </div>
    {filteredHistory.length > HISTORY_PAGE_SIZE && <div className="history-pagination">
      <button className="secondary" disabled={historyPage===1} onClick={()=>setHistoryPage(p=>Math.max(1,p-1))}>Previous</button>
      <span>Page {historyPage} of {historyPages}</span>
      <button className="secondary" disabled={historyPage===historyPages} onClick={()=>setHistoryPage(p=>Math.min(historyPages,p+1))}>Next</button>
    </div>}
  </section>}
  {tab==='chat'&&<section className="chat-layout"><div className="chat-heading"><div className="eyebrow">PERSONAL KNOWLEDGE ASSISTANT</div><h2>What can I help you with?</h2><p>Ask about your uploaded documents. Answers may include retrieved source references.</p></div><div className="messages">{messages.length===0&&<div className="welcome"><div className="welcome-icon"><Sparkles/></div><h3>Start with a question</h3><p>Ask a question and get help from your AI assistant.</p><div className="suggestions"><button onClick={()=>setQuestion('Summarize my uploaded documents')}>Summarize my documents <span>↗</span></button><button onClick={()=>setQuestion('What are the key points in my documents?')}>Find key points <span>↗</span></button></div></div>}{messages.map((m,i)=><div className={`message-row ${m.role}`} key={i}><div className="message-avatar">{m.role==='assistant'?<Brain size={17}/>:<span>{me.email[0].toUpperCase()}</span>}</div><div className="message-content"><div className="message-role">{m.role==='assistant'?'RAG Assistant':'You'}</div><div className={`bubble ${m.role === 'assistant' ? 'assistant-markdown' : 'user-text'}`}>{m.role === 'assistant' ? <ReactMarkdown remarkPlugins={[remarkGfm]} components={{pre: CopyableCodeBlock}}>
  {(m.content || '').replace(/<br\s*\/?>/gi, '  \n')}
</ReactMarkdown> : m.content}</div>{m.role === 'assistant' && <div className="chat-message-actions"><button type="button" onClick={() => copyText(m.content)} title="Copy assistant response"><Copy size={13}/> Copy response</button></div>}{m.sources?.length>0&&<div className="sources"><b>Retrieved sources</b>{m.sources.map((s,j)=><div key={j}>{s.source_type==='web'&&s.url&&/^https?:\/\//i.test(s.url)?<>🌐 <a href={s.url} target="_blank" rel="noopener noreferrer">{s.title||s.filename}</a></>:<>📄 {s.filename}{s.chunk_index!=null&&s.chunk_index!=='?'?` · chunk ${s.chunk_index}`:''}</>}</div>)}</div>}{m.message_id&&<div className="feedback"><button onClick={()=>rate(m.message_id,5)} title="Helpful"><ThumbsUp size={14}/></button><button onClick={()=>rate(m.message_id,1)} title="Not helpful"><ThumbsDown size={14}/></button><small>{m.latency_ms} ms</small></div>}</div></div>)}{busy&&<div className="message-row assistant"><div className="message-avatar"><Brain size={17}/></div><div className="bubble typing">Thinking through your question…</div></div>}</div><div className="composer"><textarea value={question} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}} placeholder="Message Advanced RAG AI..." rows="1"/><button className="send-btn" onClick={send} disabled={busy||!question.trim()}><Send size={17}/></button><small>AI can make mistakes. Verify important information.</small></div></section>}
  {tab==='documents'&&<section className="page"><div className="page-title"><div><div className="eyebrow">KNOWLEDGE BASE</div><h2>My documents</h2><p>Upload files to index them for private retrieval.</p></div><label className="primary upload-btn"><Upload size={17}/> Upload file<input type="file" accept=".pdf,.txt,.docx" onChange={upload}/></label></div><div className="info-card"><Database/><div><b>Private document index</b><p>Retrieval is scoped to your account. Supported formats: PDF, TXT and DOCX.</p></div></div><div className="doc-list">{documents.map(d=><div className="doc-card" key={d.id}><FileText/><div className="doc-info"><b>{d.filename}</b><small>{d.chunk_count} chunks · {d.status}</small></div><span className={`state ${d.status}`}>{d.status}</span></div>)}{documents.length===0&&<div className="empty">No documents yet. Upload a file to get started.</div>}</div></section>}
  {tab==='memory'&&<section className="page"><div className="eyebrow">PERSONALIZATION</div><h2>Personal memory</h2><p>Save preferences or context you want the assistant to use in future chats. This note is private to your account.</p><div className="info-card"><Brain/><div><b>Persistent, user-isolated memory</b><p>Your saved note is stored in PostgreSQL and is passed to the assistant as a personalization hint. Do not store passwords, secrets, or sensitive information.</p></div></div><label className="field-label">What should the assistant remember?</label><textarea className="memory-editor" maxLength={5000} value={memory} onChange={e=>setMemory(e.target.value)} placeholder="Example: I prefer explanations in simple English with step-by-step Ubuntu commands. I am learning Docker, Kubernetes and RAG."/><div className="memory-footer"><small>{memory.length}/5000 characters</small><button className="primary" onClick={saveMemory}>Save memory</button></div></section>}
  {tab==='pipeline'&&<section className="page"><div className="eyebrow">OBSERVABILITY</div><h2>Visual RAG Pipeline</h2><p>Live metrics derived from your document records. A stage is marked complete only when the current backend records support it.</p><div className="metric-grid">{[['Documents',pipeline?.documents_total??'—'],['Ready',pipeline?.documents_ready??'—'],['Chunks',pipeline?.chunks_indexed??'—'],['Failed',pipeline?.documents_failed??'—']].map(([k,v])=><div className="metric-card" key={k}><span>{k}</span><b>{v}</b></div>)}</div><div className="pipeline-flow">{(pipeline?.stages||[]).map((stage,i)=><React.Fragment key={stage.name}><div className={`pipeline-stage ${stage.status}`}><div className="pipeline-number">{i+1}</div><div><b>{stage.name}</b><small>{stage.detail}</small></div><span className="state">{stage.status}</span></div>{i<(pipeline?.stages?.length||0)-1&&<div className="pipeline-connector"/>}</React.Fragment>)}</div><div className="info-card"><Activity/><div><b>Retrieval debugger</b><p>Enter a question to inspect the top chunks returned by your actual user-scoped ChromaDB retrieval.</p></div></div><div className="debug-row"><input value={debugQuery} onChange={e=>setDebugQuery(e.target.value)} placeholder="Example: What is the project architecture?"/><button className="primary" disabled={!debugQuery.trim()} onClick={runDebug}>Inspect retrieval</button></div>{debugResults&&<div className="debug-results"><div className="debug-summary">{debugResults.result_count} chunk(s) retrieved for “{debugResults.query}”</div>{debugResults.results.map((r,i)=><div className="debug-card" key={`${r.document_id}-${r.chunk_index}-${i}`}><div className="debug-card-head"><b>{r.filename} · chunk {r.chunk_index}</b><span>Similarity estimate: {r.similarity_estimate}</span></div><div className="score-track"><div style={{width:`${Math.round(r.similarity_estimate*100)}%`}}/></div><p>{r.text_preview}</p></div>)}</div>}</section>}
  {tab==='admin'&&<AdminConsole token={token} onNotice={setNotice}/>}
  </main></div>
}
export default App
