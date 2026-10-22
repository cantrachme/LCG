import { useEffect, useState } from 'react'
import './App.css'

const healthUrl = 'http://127.0.0.1:8000/api/health'
type ConnectionStatus = 'loading' | 'connected' | 'error'

function App() {
  const [connection, setConnection] = useState<ConnectionStatus>('loading')
  const [checkAttempt, setCheckAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const timeout = window.setTimeout(() => controller.abort(), 8000)

    async function checkHealth() {
      try {
        const response = await fetch(healthUrl, {
          signal: controller.signal,
          cache: 'no-store',
        })
        if (!response.ok) throw new Error('Health check failed')

        const health: unknown = await response.json()
        if (
          typeof health !== 'object' ||
          health === null ||
          !('status' in health) ||
          health.status !== 'ok' ||
          !('service' in health) ||
          health.service !== 'lcg-backend'
        ) {
          throw new Error('Unexpected health response')
        }

        if (active) setConnection('connected')
      } catch {
        if (active) setConnection('error')
      } finally {
        window.clearTimeout(timeout)
      }
    }

    void checkHealth()
    return () => {
      active = false
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [checkAttempt])

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace">Skip to workspace</a>
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">L</span>
          <div>
            <h1>LCG Command Center</h1>
            <p>Learning Care Group</p>
          </div>
        </div>
        <div className="connection-controls">
          <div className={`connection-status ${connection}`} role="status" aria-atomic="true">
            <span className="status-dot" aria-hidden="true" />
            {connection === 'loading' && 'Checking backend…'}
            {connection === 'connected' && 'Backend connected'}
            {connection === 'error' && 'Backend unavailable'}
          </div>
          <button
            className="connection-button"
            type="button"
            disabled={connection === 'loading'}
            onClick={() => {
              setConnection('loading')
              setCheckAttempt((attempt) => attempt + 1)
            }}
          >
            {connection === 'error' ? 'Retry connection' : 'Check connection'}
          </button>
        </div>
      </header>

      <main id="workspace" tabIndex={-1}>
        <div className="workspace-heading">
          <div>
            <p className="eyebrow">OPERATIONS WORKSPACE</p>
            <h2>One place to work together.</h2>
            <p>Contract setup, with your AI coworker and browser side by side.</p>
          </div>
          <span className="foundation-badge">Foundation preview</span>
        </div>

        {connection === 'error' && (
          <div className="connection-notice">
            <strong>We couldn’t reach the backend.</strong>{' '}
            Check that the local server is running at <code>http://127.0.0.1:8000</code>,
            then retry the connection. You can still explore the workspace.
          </div>
        )}

        <div className="workspace-grid">
          <section className="panel coworker-panel" aria-labelledby="coworker-title">
            <header className="panel-header">
              <span className="panel-icon coworker-icon" aria-hidden="true">✦</span>
              <div>
                <h2 id="coworker-title">AI Coworker</h2>
                <p>Your conversation workspace</p>
              </div>
              <span className="panel-tag">Preview</span>
            </header>

            <div className="conversation-content">
              <p className="section-label">LET’S GET STARTED</p>
              <div className="welcome-message">
                <h3>A little less admin.<br />A little more care.</h3>
                <p>
                  This is where you’ll work with your AI coworker to review
                  subsidy contracts and guide each step of setup.
                </p>
                <div className="preview-note">
                  Conversation and document uploads will be available in a future release.
                </div>
              </div>

              <div className="planned-workflow">
                <h3>What’s ahead</h3>
                <p>The planned contract workflow</p>
                <ol>
                  <li><span aria-hidden="true">01</span><div><strong>Bring the details</strong><p>Upload a contract for review.</p></div></li>
                  <li><span aria-hidden="true">02</span><div><strong>Review together</strong><p>Check extracted fields and resolve questions.</p></div></li>
                  <li><span aria-hidden="true">03</span><div><strong>Stay in control</strong><p>Approve changes before they’re saved in LCW.</p></div></li>
                </ol>
              </div>
            </div>

            <div className="composer">
              <label htmlFor="message">Message your coworker</label>
              <div className="composer-input">
                <textarea id="message" placeholder="Conversation is coming soon…" disabled rows={2} aria-describedby="composer-note" />
                <button type="button" className="send-button" disabled aria-label="Send message (coming soon)">↑</button>
              </div>
              <p id="composer-note">Chat is not connected in this preview.</p>
            </div>
          </section>

          <section className="panel browser-panel" aria-labelledby="browser-title">
            <header className="panel-header">
              <span className="panel-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M3 9h18M7 6.5h.01M10 6.5h.01" strokeLinecap="round" /></svg>
              </span>
              <div>
                <h2 id="browser-title">Browser Agent</h2>
                <p>Your browser workspace</p>
              </div>
              <span className="panel-tag">Not connected</span>
            </header>

            <div className="browser-content">
              <div className="browser-empty-state">
                <div className="browser-illustration" aria-hidden="true">
                  <svg viewBox="0 0 64 64" fill="none" stroke="currentColor" strokeWidth="2"><rect x="8" y="12" width="48" height="36" rx="5" /><path d="M8 23h48M15 17.5h.01M21 17.5h.01M27 17.5h.01M24 55h16M32 48v7" strokeLinecap="round" /><path d="m28 30 9 5-9 5V30Z" strokeLinejoin="round" /></svg>
                </div>
                <span className="empty-state-label">A SPACE FOR YOUR NEXT STEP</span>
                <h3>Your browser workspace,<br />right here.</h3>
                <p>
                  A future release will bring the LCW browser session into this
                  workspace, so you can follow the agent’s progress and take
                  control when needed.
                </p>
                <span className="informational-badge"><span className="status-dot" aria-hidden="true" />Informational workspace</span>
              </div>
              <div className="browser-note">
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" strokeLinecap="round" /></svg>
                <p>No browser session is active. Browser automation and portal access are not enabled in this preview.</p>
              </div>
            </div>
          </section>
        </div>
        <footer className="workspace-footer">
          <span>Built around your workflow. Guided by you.</span>
          <span>LCG · Command Center</span>
        </footer>
      </main>
    </div>
  )
}

export default App
