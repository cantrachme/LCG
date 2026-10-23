import { useEffect, useRef, useState } from 'react'
import type { ContractExtraction } from './App'

const api = 'http://127.0.0.1:8000/api/browser'
type Event = { stage: string; state: string; at: string }
type Job = { id: string; extraction_id: string; state: string; stage: string; version: number; events: Event[] }
type Session = {
  state: string; message: string; owner: string; active: boolean; busy: boolean
  epoch: number; loading: boolean; launch_mode: string; control_pending: boolean
  location: string | null; preview_available: boolean; authenticated: boolean; inspection: Job | null
}
type Comparison = {
  field: string; status: 'matching' | 'different' | 'missing' | 'not_verified'
  document: { value: unknown; original: string | null; source_page: number | null }
  portal: { value: unknown; status: string; evidence: string }; reason: string
}
type Result = {
  identity: Record<string, string>; contracts: Record<string, unknown>[]; assignments: Record<string, unknown>[]
  contract_selection: string; assignment_selection: string
  coverage: { status: string; reason: string }; comparison: Comparison[]; notes: string[]
}
type Inspection = Job & { source: ContractExtraction; result: Result | null }

async function request(path: string, method = 'GET', signal?: AbortSignal, body?: unknown) {
  const response = await fetch(`${api}/${path}`, {
    method, headers: { 'X-LCG-Local': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined, cache: 'no-store', signal,
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(typeof error?.detail === 'string' ? error.detail : 'The browser request could not be completed. Check the existing local window.')
  }
  return response
}
function label(value: string) { return value.replaceAll('_', ' ').replaceAll('.', ' · ') }
function show(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
const running = (job?: Job | null) => !!job && ['inspecting', 'paused', 'awaiting_authentication', 'awaiting_mfa'].includes(job.state)

export default function BrowserWorkspace({ extraction, onRestore }: {
  extraction: ContractExtraction | null; onRestore: (source: ContractExtraction) => void
}) {
  const [session, setSession] = useState<Session | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [inspection, setInspection] = useState<Inspection | null>(null)
  const version = useRef('')
  const actionController = useRef<AbortController | null>(null)
  const actionPending = useRef(false)
  const actionVersion = useRef(0)
  const restore = useRef(onRestore)
  useEffect(() => { restore.current = onRestore }, [onRestore])

  useEffect(() => {
    let active = true
    let timer: number
    const controller = new AbortController()
    async function refresh() {
      try {
        if (!document.hidden) {
          const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(10000)])
          const current: Session = await (await request('status', 'GET', signal)).json()
          if (!active) return
          setSession(current)
          setConnectionError(null)
          const key = current.inspection ? `${current.inspection.id}:${current.inspection.version}` : ''
          if (key !== version.current) {
            // Server owns the job; reloading/unmounting the frontend never cancels it.
            const detail: Inspection | null = await (await request('inspection', 'GET', signal)).json()
            if (!active) return
            setInspection(detail)
            version.current = key
          }

        }
      } catch {
        if (active) {
          setConnectionError('Browser connection unavailable. Reconnecting does not restart Chromium or sign in.')
        }
      } finally {
        if (active) timer = window.setTimeout(refresh, document.hidden ? 5000 : 2000)
      }
    }
    void request('document', 'GET', controller.signal).then(response => response.json()).then((source: ContractExtraction | null) => {
      if (active && source) restore.current(source)
    }).catch(() => { /* Status polling reports the connection error. */ })
    void request('reconnect', 'POST', controller.signal).catch(() => { /* Never launches a new session. */ }).finally(() => { if (active) void refresh() })
    return () => {
      active = false
      controller.abort()
      actionController.current?.abort()
      window.clearTimeout(timer)
    }
  }, [])

  async function act(action: string) {
    if (actionPending.current && action !== 'take-control') return
    actionPending.current = true
    const currentAction = ++actionVersion.current
    setBusy(true)
    setError(null)
    const controller = new AbortController()
    actionController.current = controller
    try {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(40000)])
      const body = action === 'inspect' ? { extraction_id: extraction?.extraction_id, request_id: crypto.randomUUID() } : undefined
      const current: Session = await (await request(action, 'POST', signal, body)).json()
      if (currentAction !== actionVersion.current) return
      setSession(current)
      if (action === 'inspect' && current.inspection?.id !== inspection?.id) setInspection(null)
    } catch (failure) {
      if (currentAction === actionVersion.current) setError(failure instanceof Error ? failure.message : 'Browser action failed. Check the existing window.')
    } finally {
      if (currentAction === actionVersion.current) {
        actionPending.current = false
        setBusy(false)
        actionController.current = null
      }
    }
  }

  const job = session?.inspection
  const result = inspection?.result
  const canInspect = !!extraction?.extraction_id && session?.authenticated && session.owner === 'agent' && !running(job)
  return <section className="panel browser-panel" aria-labelledby="browser-title">
    <header className="panel-header">
      <span className="panel-icon" aria-hidden="true">↗</span>
      <div><h2 id="browser-title">Browser Agent</h2><p>One dedicated local Chromium · Read-only inspection</p></div>
      <span className="panel-tag">{label(session?.state ?? 'idle')}</span>
    </header>
    <div className="browser-content live-browser">
      <div className="browser-actions">
        <button className="connection-button" disabled={busy || session?.active} onClick={() => void act('reconnect')}>Reconnect existing browser</button>
        <button className="connection-button" disabled={busy || session?.active} onClick={() => void act('start')}>Open embedded browser</button>

        <button className="connection-button" disabled={!session?.active} onClick={() => void act(session?.owner === 'human' ? 'return-control' : 'take-control')}>
          {session?.owner === 'human' ? 'Return Control' : 'Take Control'}
        </button>
        <button className="connection-button" disabled={busy || !running(job)} onClick={() => void act('cancel-inspection')}>Stop inspection</button>
        <button className="connection-button" disabled={busy || !session?.active || running(job)} onClick={() => void act('close')}>Close browser explicitly</button>
      </div>
      <button className="extract-button" disabled={busy || !canInspect} onClick={() => void act('inspect')}>Inspect in LCW</button>
      {!extraction && <p className="review-description">Upload and extract a PDF to inspect its records. Browser reconnection and status checks never initiate login.</p>}
      {extraction && !session?.authenticated && <p className="review-description">Inspection requires an already authenticated LCW session. No login is triggered by Inspect, PDF upload, refresh or retry.</p>}
      <p className="browser-session-status" role="status">{session?.message ?? 'Waiting for browser status.'}</p>
      {connectionError && <p className="upload-error" role="alert">{connectionError}</p>}
      {error && <p className="upload-error" role="alert">{error}</p>}
      <div className="browser-location">{session?.location ?? 'No controlled page open'}</div>
      <EmbeddedBrowser session={session} onStatus={setSession} />
      {session?.active && session.launch_mode !== 'headless' && <p className="review-description">Reconnected to the existing browser without closing it. To switch to headless mode when ready, stop inspection, explicitly close the browser, then open it again. The profile is retained.</p>}
      {job && <section className="inspection-progress" aria-label="Inspection progress">
        <h3>{label(job.state)}</h3><p>{job.stage}</p>
        <ol>{job.events.map((event, index) => <li key={`${index}-${event.at}`}><time>{new Date(event.at).toLocaleTimeString()}</time> {event.stage}</li>)}</ol>
      </section>}
      {result && <section className="inspection-result" aria-label="LCW comparison results">
        <h3>PDF compared with observed LCW records</h3>
        {inspection?.extraction_id !== extraction?.extraction_id && <p className="upload-error">This result belongs to an earlier PDF extraction. Inspect the current PDF to update it.</p>}
        <dl>{Object.entries(result.identity).map(([key, value]) => <div key={key}><dt>{label(key)}</dt><dd>{value}</dd></div>)}</dl>
        <p>PRA coverage: <strong>{label(result.coverage.status)}</strong> — {result.coverage.reason}</p>
        <details open><summary>Observed contracts ({result.contracts.length}) · selection: {result.contract_selection}</summary>
          {result.contracts.map((record, i) => <dl key={i}>{Object.entries(record).map(([key, value]) => <div key={key}><dt>{label(key)}</dt><dd>{show(value)}</dd></div>)}</dl>)}
        </details>
        <details open><summary>Observed assignments ({result.assignments.length}) · selection: {result.assignment_selection}</summary>
          {result.assignments.map((record, i) => <dl key={i}>{Object.entries(record).map(([key, value]) => <div key={key}><dt>{label(key)}</dt><dd>{show(value)}</dd></div>)}</dl>)}
        </details>
        <ul>{result.notes.map(note => <li key={note}>{note}</li>)}</ul>
        <div className="comparison-scroll"><table className="comparison-table"><caption>Source values and portal observations. No changes were applied.</caption>
          <thead><tr><th>Field</th><th>PDF</th><th>LCW</th><th>Result</th></tr></thead>
          <tbody>{result.comparison.map(row => <tr key={row.field} className={`comparison-${row.status}`}>
            <th scope="row">{label(row.field)}</th>
            <td>{show(row.document.value)}<small>Page {row.document.source_page ?? '—'} · {row.document.original ?? 'No source value'}</small></td>
            <td>{show(row.portal.value)}<small>{row.portal.evidence}</small></td>
            <td>{label(row.status)}<small>{row.reason}</small></td>
          </tr>)}</tbody>
        </table></div>
      </section>}
      <div className="browser-note"><p>One profile is retained across application restarts. Microsoft/LCW controls authentication expiry. Take Control pauses at the next bounded operation; Return Control re-verifies identity before restarting inspection. No agent editing, add, copy or save actions are implemented.</p></div>
    </div>
  </section>
}


function EmbeddedBrowser({ session, onStatus }: { session: Session | null; onStatus: (value: Session) => void }) {
  const [frame, setFrame] = useState<string | null>(null)
  const [state, setState] = useState('disconnected')
  const [message, setMessage] = useState('Open or reconnect to the dedicated browser.')
  const [expanded, setExpanded] = useState(false)
  const socket = useRef<WebSocket | null>(null)
  const current = useRef(session)
  const surface = useRef<HTMLDivElement | null>(null)
  const image = useRef<HTMLImageElement | null>(null)
  const inputQueue = useRef<Record<string, unknown>[]>([])
  const awaitingInput = useRef(false)
  useEffect(() => { current.current = session }, [session])
  const active = !!session?.active

  useEffect(() => {
    if (!active) return
    let stopped = false
    let retry: number
    let heartbeat: number
    let connection: WebSocket | null = null
    const controller = new AbortController()
    async function connect() {
      setState('loading')
      setMessage('Connecting to the controlled page…')
      try {
        const { ticket } = await (await request('viewer-ticket', 'POST', controller.signal)).json()
        if (stopped) return
        connection = new WebSocket(`${api.replace('http:', 'ws:')}/stream`)
        socket.current = connection
        connection.onopen = () => connection?.send(ticket)
        connection.onmessage = (event) => {
          if (stopped) return
          const data = JSON.parse(event.data)
          if (data.type === 'frame') {
            const next = new Image()
            next.onload = () => {
              if (stopped || connection?.readyState !== WebSocket.OPEN) return
              setFrame(next.src)
              setMessage('')
              setState('live')
              connection.send(JSON.stringify({ type: 'ack' }))
            }
            next.onerror = () => connection?.close()
            next.src = `data:image/jpeg;base64,${data.data}`
          } else if (data.type === 'status') {
            current.current = data.session
            onStatus(data.session)
            if (data.session.owner !== 'human') inputQueue.current = []
            if (!data.session.preview_available) {
              setFrame(null)
              setState('paused')
              setMessage('Page unavailable. Use LCW home to return to the configured portal.')
            }
          } else if (data.type === 'input_ack' || data.type === 'input_error') {
            awaitingInput.current = false
            if (data.type === 'input_error') {
              inputQueue.current = []
              setMessage(data.message)
            } else {
              setMessage('')
              const next = inputQueue.current.shift()
              if (next && connection?.readyState === WebSocket.OPEN) {
                awaitingInput.current = true
                connection.send(JSON.stringify(next))
              }
            }
          }
        }
        connection.onclose = (event) => {
          socket.current = null
          setFrame(null)
          setState('disconnected')
          inputQueue.current = []
          awaitingInput.current = false
          setMessage(event.code === 4409 ? 'Another Command Center tab has the viewer. Close that tab to connect here.' : 'Viewer disconnected. Reconnecting to the same session…')
          if (!stopped) retry = window.setTimeout(connect, 2000)
        }
        connection.onerror = () => { setState('error'); setMessage('Browser connection interrupted.') }
        window.clearInterval(heartbeat)
        heartbeat = window.setInterval(() => {
          if (connection?.readyState === WebSocket.OPEN) connection.send(JSON.stringify({ type: 'ping' }))
        }, 5000)
      } catch {
        if (!stopped) {
          setState('error')
          setMessage('Viewer unavailable. Retrying without launching or signing in.')
          retry = window.setTimeout(connect, 3000)
        }
      }
    }
    void connect()
    return () => {
      stopped = true
      controller.abort()
      window.clearTimeout(retry)
      window.clearInterval(heartbeat)
      if (connection) { connection.onclose = null; connection.close() }
      socket.current = null
      inputQueue.current = []
      awaitingInput.current = false
    }
  }, [active, onStatus])

  const enabled = active && state === 'live' && session?.owner === 'human' && !session.control_pending && session.preview_available
  const canNavigate = active && session?.owner === 'human' && !session.control_pending && !['disconnected', 'error', 'loading'].includes(state)
  function send(data: Record<string, unknown>) {
    const owner = current.current
    if (!(enabled || (canNavigate && ['home', 'back', 'forward'].includes(String(data.type)))) || !owner || owner.owner !== 'human' || owner.control_pending || socket.current?.readyState !== WebSocket.OPEN) return
    const event = { ...data, epoch: owner.epoch }
    if (awaitingInput.current) {
      if (inputQueue.current.length < 64) inputQueue.current.push(event)
      else { inputQueue.current = []; setMessage('Input paused: connection is slow. Retry after it catches up.') }
    } else {
      awaitingInput.current = true
      socket.current.send(JSON.stringify(event))
    }
  }
  function point(clientX: number, clientY: number) {
    const rect = image.current!.getBoundingClientRect()
    return { x: Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (clientY - rect.top) / rect.height)) }
  }
  // Native non-passive listener keeps scrolling inside the remote page.
  const wheelSend = useRef(send)
  useEffect(() => { wheelSend.current = send })
  useEffect(() => {
    const element = surface.current
    if (!element) return
    const wheel = (event: WheelEvent) => {
      event.preventDefault()
      if (!image.current) return
      const factor = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 800 : 1
      wheelSend.current({ type: 'wheel', ...point(event.clientX, event.clientY), delta_x: Math.max(-1600, Math.min(1600, event.deltaX * factor)), delta_y: Math.max(-1600, Math.min(1600, event.deltaY * factor)) })
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [])

  return <div className={`embedded-browser${expanded ? ' browser-expanded' : ''}`}>
    <div className="browser-actions viewer-toolbar">
      <button className="connection-button" disabled={!canNavigate} onClick={() => send({ type: 'back' })}>Back</button>
      <button className="connection-button" disabled={!canNavigate} onClick={() => send({ type: 'forward' })}>Forward</button>
      <button className="connection-button" disabled={!canNavigate} onClick={() => send({ type: 'home' })}>LCW home</button>
      <button className="connection-button" disabled={!enabled} onClick={() => send({ type: 'reload' })}>Reload page</button>
      <button className="connection-button" onClick={() => setExpanded(!expanded)}>{expanded ? 'Exit expanded view' : 'Expand browser'}</button>
      <span role="status">{!active ? 'Disconnected' : session.loading ? 'Loading page…' : enabled ? 'Human control' : session.owner === 'human' ? 'Paused · waiting for current operation' : 'Agent control · input disabled'}</span>
    </div>
    <div ref={surface} className={`browser-surface ${enabled ? 'human-control' : ''}`} tabIndex={enabled ? 0 : -1}
      role="application" aria-label="Interactive LCW browser" aria-disabled={!enabled}
      onClick={event => {
        if (event.target !== image.current) return
        surface.current?.focus({ preventScroll: true })
        send({ type: 'click', ...point(event.clientX, event.clientY) })
      }}
      onKeyDown={event => {
        if (!enabled) return
        // Allow an explicit keyboard exit from the remote surface.
        if (event.key === 'Escape' && event.shiftKey) { surface.current?.blur(); return }
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'v') return
        event.preventDefault()
        if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) send({ type: 'text', text: event.key })
        else if (['Tab', 'Enter', 'Backspace', 'Delete', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown', 'a'].includes(event.key)) {
          send({ type: 'key', key: event.key, shift: event.shiftKey, control: event.ctrlKey, meta: event.metaKey })
        }
      }}
      onPaste={event => { event.preventDefault(); send({ type: 'text', text: event.clipboardData.getData('text/plain').slice(0, 2000) }) }}>
      {active && frame ? <img ref={image} src={frame} draggable={false} alt="Live controlled LCW page" />
        : <div className="browser-empty-state"><h3>{active ? (state === 'paused' ? 'Browser paused' : 'Connecting browser') : 'Your dedicated LCW session'}</h3><p>{message}</p></div>}
    </div>
    <p className="viewer-caption">{active ? `Live browser · ${state} · ${enabled ? 'Click to type; Shift+Escape exits keyboard control.' : 'Select Take Control to interact.'}` : 'Open or reconnect to view the browser.'}</p>
    {message && active && <p className="viewer-caption" role="status">{message}</p>}
  </div>
}
