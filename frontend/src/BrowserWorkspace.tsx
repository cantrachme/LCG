import { useEffect, useRef, useState } from 'react'
import type { ContractExtraction } from './App'

const api = 'http://127.0.0.1:8000/api/browser'
type Event = { stage: string; state: string; at: string }
type Job = { id: string; extraction_id: string; state: string; stage: string; version: number; events: Event[] }
type Session = {
  state: string; message: string; owner: string; active: boolean; busy: boolean
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
  const [frame, setFrame] = useState<string | null>(null)
  const [captured, setCaptured] = useState<string | null>(null)
  const [inspection, setInspection] = useState<Inspection | null>(null)
  const frameUrl = useRef<string | null>(null)
  const version = useRef('')
  const actionController = useRef<AbortController | null>(null)
  const actionPending = useRef(false)
  const actionVersion = useRef(0)
  const restore = useRef(onRestore)
  useEffect(() => { restore.current = onRestore }, [onRestore])

  function clearFrame() {
    if (frameUrl.current) URL.revokeObjectURL(frameUrl.current)
    frameUrl.current = null
    setFrame(null)
    setCaptured(null)
  }

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
          if (current.preview_available) {
            const response = await fetch(`${api}/frame`, { headers: { 'X-LCG-Local': '1' }, cache: 'no-store', signal })
            if (response.ok) {
              const blob = await response.blob()
              if (!active) return
              const next = URL.createObjectURL(blob)
              if (frameUrl.current) URL.revokeObjectURL(frameUrl.current)
              frameUrl.current = next
              setFrame(next)
              const capturedAt = response.headers.get('X-LCG-Captured-At')
              setCaptured(capturedAt ? new Date(capturedAt).toLocaleTimeString() : 'time unavailable')
            } else if (response.status !== 409) {
              clearFrame()
            }
          } else if (!current.preview_available) clearFrame()
        }
      } catch {
        if (active) {
          clearFrame()
          setConnectionError('Browser connection unavailable. Reconnecting does not restart Chromium or sign in.')
        }
      } finally {
        if (active) timer = window.setTimeout(refresh, document.hidden ? 5000 : 1500)
      }
    }
    void request('document', 'GET', controller.signal).then(response => response.json()).then((source: ContractExtraction | null) => {
      if (active && source) restore.current(source)
    }).catch(() => { /* Status polling reports the connection error. */ })
    void refresh()
    return () => {
      active = false
      controller.abort()
      actionController.current?.abort()
      window.clearTimeout(timer)
      if (frameUrl.current) URL.revokeObjectURL(frameUrl.current)
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
      if (!current.preview_available) clearFrame()
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
        <button className="connection-button" disabled={busy || session?.active} onClick={() => void act('start')}>Open dedicated browser</button>
        <button className="connection-button" disabled={busy || session?.state !== 'awaiting_authentication'} onClick={() => void act('login')}>Sign in explicitly</button>
        <button className="connection-button" disabled={!session?.active} onClick={() => void act(session?.owner === 'human' ? 'return-control' : 'take-control')}>
          {session?.owner === 'human' ? 'Return control' : 'Take control locally'}
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
      {frame ? <figure className="live-frame">
        <img src={frame} alt="View of the exact LCW Chromium page controlled by Playwright" />
        <figcaption>Same controlled page · Captured {captured} · View only{session?.busy ? ' · Navigation in progress' : ''}</figcaption>
      </figure> : <div className="browser-empty-state">
        <h3>{session?.active ? 'Use the existing Chromium window' : 'Your dedicated LCW session'}</h3>
        <p>The preview appears after portal authentication is verified. Manual input takes place in the same local window; the image does not accept clicks or typing.</p>
      </div>}
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
