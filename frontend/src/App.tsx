import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'
import BrowserWorkspace from './BrowserWorkspace'

type FieldValue = string | number | boolean | null | string[] | { [key: string]: FieldValue }
type ExtractedField = {
  original: string | null
  value: FieldValue
  status: 'extracted' | 'missing' | 'needs_review'
  source_page: number | null
}
export type ContractExtraction = {
  extraction_id: string | null
  page_count: number
  original: Record<string, Record<string, ExtractedField>>
  warnings: { code: string; message: string }[]
}
const reviewSections = [
  ['identity', 'Child, family & center'],
  ['authorization', 'Contract authorization'],
  ['schedule', 'Attendance & tuition'],
  ['part_time', 'Part-time rates'],
  ['full_time', 'Full-time rates'],
  ['billing', 'Reimbursement & billing'],
  ['existing_pra', 'Existing PRA'],
  ['document', 'Document notes'],
]

function fieldLabel(key: string): string {
  return key.replaceAll('_', ' ').replace(/\b(lcw|pra|id)\b/g, (word) => word.toUpperCase()).replace(/^./, (letter) => letter.toUpperCase())
}

function displayValue(value: FieldValue): string {
  if (value === null) return 'Not provided'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'object') {
    return Object.entries(value).map(([key, item]) => `${fieldLabel(key)}: ${key === 'maximum_hours' && item === null ? 'No upper limit' : displayValue(item)}`).join(' · ')
  }
  return String(value)
}

type Conversation = { id: string; title: string; created_at: string }
type Message = { id: string; role: string; content: string; created_at: string }
type DocumentRecord = { id: string; conversation_id: string; extraction: ContractExtraction; metadata: { filename: string }; created_at: string }
const api = 'http://127.0.0.1:8000/api'
const ignoreBrowserRestore = () => {} // Chat selection owns its document, not the last global browser job.
const timestamp = (value: string) => new Date(value).toLocaleString()

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${api}${path}`, {
    ...options, cache: 'no-store', signal: options.signal ?? AbortSignal.timeout(30000),
    headers: { 'X-LCG-Local': '1', ...(typeof options.body === 'string' ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  })
  if (!response.ok) {
    const data = await response.json().catch(() => null)
    throw new Error(typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status}).`)
  }
  return response.status === 204 ? undefined as T : response.json()
}
async function all<T>(path: string, signal?: AbortSignal): Promise<T[]> {
  const result: T[] = []
  for (let offset = 0; ; offset += 100) {
    const page = await request<T[]>(`${path}?limit=100&offset=${offset}`, { signal })
    result.push(...page)
    if (page.length < 100) return result
  }
}
function failure(error: unknown): string {
  return error instanceof Error && error.name !== 'TypeError' && error.name !== 'TimeoutError'
    ? error.message : 'Connection interrupted. Refresh the chat to check what was saved before retrying.'
}

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [active, setActive] = useState<string | null>(() => {
    try { return localStorage.getItem('lcg-active-chat') } catch { return null }
  })
  const [messages, setMessages] = useState<Message[]>([])
  const [documents, setDocuments] = useState<DocumentRecord[]>([])
  const [extraction, setExtraction] = useState<ContractExtraction | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [file, setFile] = useState<File | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(true)
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState('')
  const [deleting, setDeleting] = useState(false)
  const mutation = useRef(false)
  const fileInput = useRef<HTMLInputElement | null>(null)
  const end = useRef<HTMLDivElement | null>(null)
  const current = conversations.find(chat => chat.id === active)
  const draft = active ? drafts[active] ?? '' : ''

  useEffect(() => {
    const controller = new AbortController()
    setLoadingHistory(true)
    let saved: string | null = null
    try { saved = localStorage.getItem('lcg-active-chat') } catch { /* Storage can be disabled. */ }
    void all<Conversation>('/persistence/conversations', controller.signal).then(chats => {
      if (controller.signal.aborted) return
      setConversations(chats.reverse())
      setActive(previous => chats.some(chat => chat.id === previous) ? previous : chats.find(chat => chat.id === saved)?.id ?? chats[0]?.id ?? null)
    }).catch(cause => { if (!controller.signal.aborted) setError(failure(cause)) })
      .finally(() => { if (!controller.signal.aborted) setLoadingHistory(false) })
    return () => controller.abort()
  }, [revision])

  useEffect(() => {
    const controller = new AbortController()
    setMessages([]); setDocuments([]); setExtraction(null); setFile(null); setLoaded(false)
    setRenaming(false); setDeleting(false)
    if (fileInput.current) fileInput.current.value = ''
    try {
      if (active) localStorage.setItem('lcg-active-chat', active)
      else localStorage.removeItem('lcg-active-chat')
    } catch { /* Only the selected ID is cached; content always comes from PostgreSQL. */ }
    if (!active) { setLoading(false); return () => controller.abort() }
    setLoading(true)
    void Promise.all([
      all<Message>(`/persistence/conversations/${active}/messages`, controller.signal),
      all<DocumentRecord>(`/persistence/conversations/${active}/documents`, controller.signal),
    ]).then(([savedMessages, savedDocuments]) => {
      if (!controller.signal.aborted) { setMessages(savedMessages); setDocuments(savedDocuments); setLoaded(true) }
    }).catch(cause => { if (!controller.signal.aborted) setError(failure(cause)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [active, revision])

  useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }) }, [messages.length, documents.length, active])

  async function mutate(label: string, action: () => Promise<void>) {
    if (mutation.current) return
    mutation.current = true; setBusy(label); setError(null)
    try { await action() } catch (cause) { setError(failure(cause)) }
    finally { mutation.current = false; setBusy('') }
  }
  function switchChat(id: string) {
    if (mutation.current) return
    setError(null); setActive(id)
  }
  function newChat() {
    void mutate('Creating chat…', async () => {
      const chat = await request<Conversation>('/persistence/conversations', { method: 'POST', body: JSON.stringify({ title: 'New chat' }) })
      setConversations(previous => [chat, ...previous]); setActive(chat.id)
    })
  }
  function rename(event: FormEvent) {
    event.preventDefault()
    if (!active || !title.trim()) return
    void mutate('Renaming…', async () => {
      const chat = await request<Conversation>(`/persistence/conversations/${active}`, { method: 'PATCH', body: JSON.stringify({ title: title.trim() }) })
      setConversations(previous => previous.map(item => item.id === chat.id ? chat : item)); setRenaming(false)
    })
  }
  function deleteChat() {
    if (!active) return
    void mutate('Deleting chat…', async () => {
      await request(`/persistence/conversations/${active}`, { method: 'DELETE' })
      const remaining = conversations.filter(chat => chat.id !== active)
      setConversations(remaining); setActive(remaining[0]?.id ?? null); setDeleting(false)
      setDrafts(previous => { const next = { ...previous }; delete next[active]; return next })
    })
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!active || !loaded || (!draft.trim() && !file)) return
    const chatId = active
    void mutate(file ? 'Saving and extracting PDF…' : 'Saving message…', async () => {
      if (draft.trim()) {
        const message = await request<Message>(`/persistence/conversations/${chatId}/messages`, { method: 'POST', body: JSON.stringify({ role: 'user', content: draft.trim() }) })
        setMessages(previous => [...previous, message]); setDrafts(previous => ({ ...previous, [chatId]: '' }))
      }
      if (file) {
        const body = new FormData(); body.append('file', file)
        const saved = await request<ContractExtraction>(`/contracts/extract?conversation_id=${chatId}`, { method: 'POST', body })
        setFile(null); if (fileInput.current) fileInput.current.value = ''
        setExtraction(saved)
        setDocuments(await all<DocumentRecord>(`/persistence/conversations/${chatId}/documents`))
      }
    })
  }
  const timeline = [
    ...messages.map(message => ({ kind: 'message' as const, value: message })),
    ...documents.map(document => ({ kind: 'document' as const, value: document })),
  ].sort((a, b) => a.value.created_at.localeCompare(b.value.created_at) || a.value.id.localeCompare(b.value.id))

  return <div className="app-shell">
    <a className="skip-link" href="#workspace">Skip to workspace</a>
    <header className="app-header"><div className="brand"><span className="brand-mark" aria-hidden="true">L</span><div><h1>LCG Command Center</h1><p>Conversations and contracts</p></div></div><span className="foundation-badge">Your workspace</span></header>
    <main id="workspace" tabIndex={-1}>
      <div className="workspace-grid">
        <section className="panel chat-panel" aria-label="Conversation workspace">
          <aside className="chat-history" aria-label="Conversation history">
            <button className="extract-button" disabled={!!busy || loadingHistory} onClick={newChat}>＋ New Chat</button>
            <h2>Chats</h2>
            {loadingHistory && <p role="status">Loading history…</p>}
            <nav aria-label="Saved conversations">{conversations.map(chat => <button key={chat.id} className={`history-item ${chat.id === active ? 'selected' : ''}`} disabled={!!busy || loadingHistory}
              aria-current={chat.id === active ? 'page' : undefined} onClick={() => switchChat(chat.id)}><span>{chat.title}</span><time dateTime={chat.created_at}>{new Date(chat.created_at).toLocaleDateString()}</time></button>)}</nav>
            {!loadingHistory && !conversations.length && <p>No saved chats yet.</p>}
          </aside>
          <div className="chat-main">
            <header className="chat-header"><h2>{current?.title ?? 'Start a conversation'}</h2>
              <div className="chat-tools">
                <button className="connection-button" disabled={!current || !!busy || loading} onClick={() => { setTitle(current!.title); setRenaming(!renaming); setDeleting(false) }}>Rename</button>
                <button className="connection-button" disabled={!current || !!busy || loading} onClick={() => { setDeleting(!deleting); setRenaming(false) }}>Delete</button>
                <button className="connection-button" disabled={!!busy || loadingHistory || loading} onClick={() => { setError(null); setRevision(value => value + 1) }}>Refresh chats</button>
              </div>
            </header>
            {renaming && <form className="chat-rename" onSubmit={rename}><label htmlFor="chat-title">Chat name</label><input id="chat-title" autoFocus maxLength={200} value={title} onChange={event => setTitle(event.target.value)} disabled={!!busy} /><button className="connection-button" disabled={!!busy || !title.trim()}>Save</button><button className="connection-button" type="button" onClick={() => setRenaming(false)}>Cancel</button></form>}
            {deleting && <div className="chat-delete" role="alert"><p>Delete this chat and its messages? Extracted document records are retained.</p><button className="connection-button" disabled={!!busy} onClick={deleteChat}>Delete chat</button><button className="connection-button" disabled={!!busy} onClick={() => setDeleting(false)}>Cancel</button></div>}
            {error && <p className="upload-error chat-error" role="alert">{error}</p>}
            <div className="chat-transcript" aria-label="Messages and attachments" aria-busy={loading}>
              {loading ? <p className="chat-placeholder" role="status">Loading conversation…</p> : <>
                {!timeline.length && <div className="chat-placeholder"><h3>{active ? 'What would you like to work on?' : 'A place for your work.'}</h3><p>{active ? 'Save a message or attach a contract PDF.' : 'Create a new chat to save messages and contract details.'}</p><p>AI replies are not connected yet.</p></div>}
                {timeline.map(item => item.kind === 'message' ? <article key={item.value.id} className={`chat-message ${item.value.role === 'user' ? 'from-user' : ''}`}>
                  <header><strong>{item.value.role === 'user' ? 'You' : fieldLabel(item.value.role)}</strong><time dateTime={item.value.created_at}>{timestamp(item.value.created_at)}</time></header><p>{item.value.content}</p>
                </article> : <article className="chat-attachment" key={item.value.id}>
                  <header><strong>▤ {item.value.metadata.filename}</strong><time dateTime={item.value.created_at}>{timestamp(item.value.created_at)}</time></header>
                  <p>{item.value.extraction.page_count} page(s) · Extracted information saved</p>
                  <button className="connection-button" disabled={!!busy || extraction?.extraction_id === item.value.id} onClick={() => void mutate('Selecting document…', async () => {
                    setExtraction(await request<ContractExtraction>(`/contracts/${item.value.id}/select`, { method: 'POST' }))
                  })}>{extraction?.extraction_id === item.value.id ? 'Selected for inspection' : 'Use for inspection'}</button>
                  <details><summary>Review extracted information</summary>
                    {item.value.extraction.warnings.length > 0 && <ul className="review-warnings">{item.value.extraction.warnings.map(warning => <li key={warning.code}>{warning.message}</li>)}</ul>}
                    <div className="review-sections">{reviewSections.map(([key, title]) => <details key={key}><summary>{title}</summary><dl>{Object.entries(item.value.extraction.original[key] ?? {}).map(([name, field]) => <div className={`review-field field-${field.status}`} key={name}><dt>{fieldLabel(name)} <span className="field-status">{fieldLabel(field.status)}</span></dt><dd><p className="field-value">{displayValue(field.value)}</p><p className="field-source">{field.original ? `Source · page ${field.source_page}: ${field.original}` : 'No source value.'}</p></dd></div>)}</dl></details>)}</div>
                  </details>
                </article>)}
              </>}
              <div ref={end} />
            </div>
            <form className="composer chat-composer" onSubmit={submit} aria-busy={!!busy}>
              <label htmlFor="chat-message">Message</label>
              <div className="composer-input"><textarea id="chat-message" rows={3} maxLength={20000} placeholder="Write a message…" disabled={!active || !loaded || !!busy || loadingHistory}
                value={draft} onChange={event => { if (active) setDrafts(previous => ({ ...previous, [active]: event.target.value })) }}
                onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
                <button className="send-button" aria-label="Send message or upload PDF" disabled={!active || !loaded || !!busy || loadingHistory || (!draft.trim() && !file)}>↑</button></div>
              <div className="chat-upload"><label className="connection-button" htmlFor="chat-pdf">Attach PDF</label><input ref={fileInput} id="chat-pdf" type="file" accept=".pdf,application/pdf" disabled={!active || !loaded || !!busy} onChange={event => {
                const selected = event.target.files?.[0] ?? null
                setError(null); setFile(null)
                if (selected && (!selected.name.toLowerCase().endsWith('.pdf') || selected.size === 0 || selected.size > 5 * 1024 * 1024)) { setError('Choose a non-empty PDF up to 5 MB.'); event.target.value = ''; return }
                setFile(selected)
              }} />{file && <><span>{file.name}</span><button type="button" className="connection-button" disabled={!!busy} onClick={() => { setFile(null); if (fileInput.current) fileInput.current.value = '' }}>Remove</button></>}</div>
              <p role="status">{busy || 'Messages and extracted details are saved. Original PDF files are not retained.'}</p>
            </form>
          </div>
        </section>
        <BrowserWorkspace extraction={extraction} onRestore={ignoreBrowserRestore} />
      </div>
    </main>
  </div>
}
export default App
