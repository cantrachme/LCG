import { useEffect, useRef, useState } from 'react'

const api = 'http://127.0.0.1:8000/api/browser'
type Session = {
  state: string; message: string; owner: string; active: boolean
  location: string | null; preview_available: boolean; authenticated: boolean
}

async function request(path: string, method = 'GET', signal?: AbortSignal) {
  const response = await fetch(`${api}/${path}`, {
    method, headers: { 'X-LCG-Local': '1' }, cache: 'no-store', signal,
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(error?.detail || 'Browser connection failed. Check the local backend.')
  }
  return response
}

export default function BrowserWorkspace() {
  const [session, setSession] = useState<Session | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [frame, setFrame] = useState<string | null>(null)
  const [captured, setCaptured] = useState<string | null>(null)
  const [links, setLinks] = useState<string[]>([])
  const frameUrl = useRef<string | null>(null)
  const busyRef = useRef(false)
  const actionController = useRef<AbortController | null>(null)

  function clearFrame() {
    if (frameUrl.current) URL.revokeObjectURL(frameUrl.current)
    frameUrl.current = null
    setFrame(null)
    setCaptured(null)
  }

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    let timer: number
    async function refresh() {
      try {
        if (!busyRef.current && !document.hidden) {
          const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(10000)])
          const current: Session = await (await request('status', 'GET', signal)).json()
          if (!active) return
          setSession(current)
          if (current.preview_available) {
            const blob = await (await request('frame', 'GET', signal)).blob()
            if (!active) return
            clearFrame()
            frameUrl.current = URL.createObjectURL(blob)
            setFrame(frameUrl.current)
            setCaptured(new Date().toLocaleTimeString())
          } else {
            clearFrame()
          }
        }
      } catch {
        if (active) {
          clearFrame()
          setSession(null)
          setError('Browser preview unavailable. Check the backend or inspect the local Chromium window.')
        }
      } finally {
        if (active) timer = window.setTimeout(refresh, 1500)
      }
    }
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
    busyRef.current = true
    setBusy(true)
    setError(null)
    setLinks([])
    const controller = new AbortController()
    actionController.current = controller
    try {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(60000)])
      if (action === 'navigation') {
        const result = await (await request(action, 'GET', signal)).json()
        setLinks(result.links.map((item: { label: string }) => item.label))
      } else {
        const current: Session = await (await request(action, 'POST', signal)).json()
        setSession(current)
        if (!current.preview_available) clearFrame()
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Browser action failed.')
    } finally {
      busyRef.current = false
      setBusy(false)
      actionController.current = null
    }
  }

  return <section className="panel browser-panel" aria-labelledby="browser-title">
    <header className="panel-header">
      <span className="panel-icon" aria-hidden="true">↗</span>
      <div><h2 id="browser-title">Browser Agent</h2><p>Dedicated local Chromium · Read-only exploration</p></div>
      <span className="panel-tag">{session?.active ? 'Session open' : 'Not connected'}</span>
    </header>
    <div className="browser-content live-browser">
      <div className="browser-actions">
        <button className="connection-button" disabled={busy || session?.active} onClick={() => void act('start')}>Open LCW</button>
        <button className="connection-button" disabled={busy || session?.state !== 'waiting_for_login' || session.owner !== 'agent'} onClick={() => void act('login')}>Use configured sign-in</button>
        <button className="connection-button" disabled={busy || !session?.active} onClick={() => void act(session?.owner === 'human' ? 'return-control' : 'take-control')}>
          {session?.owner === 'human' ? 'Return control' : 'Take control locally'}
        </button>
        <button className="connection-button" disabled={busy || !session?.active} onClick={() => void act('close')}>Close session</button>
      </div>
      <p className="browser-session-status" role="status">{busy ? 'Working in local Chromium…' : session?.message ?? 'Open LCW to begin.'}</p>
      {error && <p className="upload-error" role="alert">{error}</p>}
      <div className="browser-location">{session?.location ?? 'No page open'}</div>
      {frame ? <figure className="live-frame">
        <img src={frame} alt="Live view of the LCW page in the Chromium session controlled by Playwright" />
        <figcaption>Same Playwright page · Updated {captured} · View only</figcaption>
      </figure> : <div className="browser-empty-state">
        <h3>{session?.active ? 'Continue in Chromium' : 'Your real LCW session'}</h3>
        <p>{session?.active ? 'Complete sign-in and MFA in the local window when Microsoft requires it. The preview pauses on sign-in pages to protect account information.' : 'Reopen the same dedicated LCW browser profile. Its live page will appear here after sign-in. Existing sign-in state is reused when the portal allows it.'}</p>
      </div>}
      <button className="connection-button" disabled={busy || !session?.preview_available} onClick={() => void act('navigation')}>Inspect visible navigation</button>
      {links.length > 0 && <details className="observed-links" open><summary>Observed link labels · routes not yet verified</summary><ul>{links.map((label, index) => <li key={`${index}-${label}`}>{label}</li>)}</ul></details>}
      <div className="browser-note"><p>Take control pauses sign-in automation and focuses this same local window. Interact there; this preview does not accept clicks. Closing retains the local profile; it does not sign you out. No contract or PRA changes are available through the agent. An existing PRA must be inspected before any decision to reuse, amend or create one.</p></div>
    </div>
  </section>
}
