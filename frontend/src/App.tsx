import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'
import BrowserWorkspace from './BrowserWorkspace'

const healthUrl = 'http://127.0.0.1:8000/api/health'
type ConnectionStatus = 'loading' | 'connected' | 'error'
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

function App() {
  const [connection, setConnection] = useState<ConnectionStatus>('loading')
  const [checkAttempt, setCheckAttempt] = useState(0)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [extraction, setExtraction] = useState<ContractExtraction | null>(null)
  const uploadController = useRef<AbortController | null>(null)

  useEffect(() => () => uploadController.current?.abort(), [])

  async function uploadContract(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedFile || uploading) return
    setUploading(true)
    setUploadError(null)
    setExtraction(null)
    const controller = new AbortController()
    uploadController.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 30000)
    const form = new FormData()
    form.append('file', selectedFile)
    try {
      const response = await fetch('http://127.0.0.1:8000/api/contracts/extract', {
        method: 'POST', headers: { 'X-LCG-Local': '1' }, body: form, signal: controller.signal,
      })
      if (!response.ok) {
        const error = await response.json().catch(() => null)
        throw new Error(typeof error?.detail === 'string' ? error.detail : `Extraction failed (${response.status}). Please try again.`)
      }
      const result: ContractExtraction = await response.json()
      if (!result.original || !Array.isArray(result.warnings) || reviewSections.some(([key]) => !result.original[key])) {
        throw new Error('The server returned an unexpected extraction result. Please try again.')
      }
      setExtraction(result)
    } catch (error) {
      setUploadError(controller.signal.aborted
        ? 'Extraction timed out. Please try again with a smaller PDF.'
        : error instanceof TypeError
          ? 'Could not reach the extraction service. Check the backend connection and try again.'
          : error instanceof Error ? error.message : 'The PDF could not be extracted. Please try again.')
    } finally {
      window.clearTimeout(timeout)
      uploadController.current = null
      setUploading(false)
    }
  }

  const extractedFields = extraction ? Object.values(extraction.original).flatMap(Object.values) : []
  const extractedCount = extractedFields.filter((field) => field.status === 'extracted').length
  const missingCount = extractedFields.filter((field) => field.status === 'missing').length
  const reviewCount = extractedFields.filter((field) => field.status === 'needs_review').length

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
          <span className="foundation-badge">Read-only LCW exploration</span>
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
              <span className="panel-tag">Document review</span>
            </header>

            <div className="conversation-content">
              <p className="section-label">START WITH A DOCUMENT</p>
              <div className="welcome-message">
                <h3>Review a subsidy contract.</h3>
                <p>Upload the training contract to extract its details, review the source values, and see what needs attention.</p>
                <div className="preview-note">Original document values only. Nothing is submitted to LCW.</div>
              </div>

              <form className="contract-upload" onSubmit={uploadContract} aria-busy={uploading}>
                <label htmlFor="contract-file">Select a contract PDF</label>
                <input
                  id="contract-file"
                  type="file"
                  accept=".pdf,application/pdf"
                  disabled={uploading}
                  aria-describedby="upload-help"
                  onChange={(event) => {
                    const file = event.target.files?.[0] ?? null
                    setExtraction(null)
                    setUploadError(null)
                    setSelectedFile(null)
                    if (file && (!file.name.toLowerCase().endsWith('.pdf') || file.size === 0 || file.size > 5 * 1024 * 1024)) {
                      setUploadError('Choose a non-empty PDF file no larger than 5 MB.')
                      event.target.value = ''
                      return
                    }
                    setSelectedFile(file)
                  }}
                />
                <p id="upload-help">Text-based PDF · Up to 5 MB and 10 pages. Uploaded documents are not saved.</p>
                <button className="extract-button" type="submit" disabled={!selectedFile || uploading}>
                  {uploading ? 'Extracting contract…' : extraction ? 'Extract again' : 'Extract contract'}
                </button>
              </form>
              <div role="status" aria-atomic="true" className="extraction-status">
                {uploading && 'Reading the PDF and checking its fields…'}
                {extraction && `${extractedCount} fields extracted · ${missingCount} missing or unassigned · ${reviewCount} need review`}
              </div>
              {uploadError && <p className="upload-error" role="alert">{uploadError}</p>}

              {extraction && (
                <section className="contract-review" aria-labelledby="review-title">
                  <h3 id="review-title">Original contract review</h3>
                  <p className="review-description">{selectedFile?.name ?? 'Restored PDF extraction'} · {extraction.page_count} {extraction.page_count === 1 ? 'page' : 'pages'}. Expand each section to compare normalized values with the source. Values are read-only.</p>
                  {extraction.warnings.length > 0 && (
                    <div className="review-warnings">
                      <h4>Review notes</h4>
                      <ul>{extraction.warnings.map((warning) => <li key={warning.code}>{warning.message}</li>)}</ul>
                    </div>
                  )}
                  <div className="review-sections">
                    {reviewSections.map(([key, title]) => (
                      <details key={key} open={key === 'identity' || key === 'existing_pra'}>
                        <summary>{title}<span>{Object.values(extraction.original[key]).some((field) => field.status !== 'extracted') ? 'Needs attention' : 'All extracted'}</span></summary>
                        <dl>
                          {Object.entries(extraction.original[key]).map(([name, field]) => (
                            <div key={name} className={`review-field field-${field.status}`}>
                              <dt>{fieldLabel(name)}<span className="field-status">{field.status === 'extracted' ? 'Extracted' : field.status === 'missing' ? 'Missing / unassigned' : 'Needs review'}</span></dt>
                              <dd>
                                <p className="field-value">{field.value === null ? field.status === 'needs_review' ? 'Review source value' : 'Not provided / not assigned' : displayValue(field.value)}</p>
                                {field.original && <p className="field-source">Source · page {field.source_page}: {field.original}</p>}
                                {!field.original && <p className="field-source">No value found in the document.</p>}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      </details>
                    ))}
                  </div>
                </section>
              )}
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

          <BrowserWorkspace extraction={extraction} onRestore={(stored) => setExtraction((current) => current ?? stored)} />
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
