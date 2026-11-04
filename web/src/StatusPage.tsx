import { type FormEvent, useState } from 'react'
import { adminLogin, type DependencyReport, type ServiceReport, type SystemStatus } from './api'
import { useSystemStatus } from './useSystemStatus'

const SERVICE_LABEL: Record<ServiceReport['status'], string> = {
  ready: 'Healthy',
  not_ready: 'Not ready',
  unreachable: 'Unreachable',
}
const DEPENDENCY_LABEL: Record<DependencyReport['status'], string> = {
  ok: 'Healthy',
  degraded: 'Degraded',
  down: 'Down',
}

function Badge({ tone, label }: { tone: 'good' | 'bad' | 'warn' | 'idle'; label: string }) {
  return (
    <span className={`badge badge-${tone}`}>
      <span className="dot" aria-hidden="true" />
      {label}
    </span>
  )
}

function ServiceCard({ service }: { service: ServiceReport }) {
  const tone = service.status === 'ready' ? 'good' : 'bad'
  const failing = Object.entries(service.checks ?? {}).filter(([, c]) => !c.ok)
  return (
    <li className={`card card-${tone}`} data-testid={`service-${service.name}`}>
      <div className="card-head">
        <h3>{service.name}</h3>
        <Badge tone={tone} label={SERVICE_LABEL[service.status]} />
      </div>
      {service.latency_ms !== undefined && <p className="muted">{service.latency_ms} ms</p>}
      {service.error && <p className="problem">{service.error}</p>}
      {failing.map(([name, check]) => (
        <p className="problem" key={name}>
          {name}: {check.error ?? 'failing'}
        </p>
      ))}
    </li>
  )
}

function DependencyRow({ dependency }: { dependency: DependencyReport }) {
  const tone =
    dependency.status === 'ok' ? 'good' : dependency.status === 'degraded' ? 'warn' : 'bad'
  return (
    <li className="row" data-testid={`dependency-${dependency.name}`}>
      <div>
        <strong>{dependency.name}</strong>
        <span className="muted">
          {' '}
          used by {dependency.used_by.map((u) => u.service).join(', ')}
        </span>
      </div>
      <Badge tone={tone} label={DEPENDENCY_LABEL[dependency.status]} />
    </li>
  )
}

function Report({ status }: { status: SystemStatus }) {
  const bad = status.services.filter((s) => s.status !== 'ready').length
  return (
    <>
      <div className={`banner banner-${bad === 0 ? 'good' : 'bad'}`} role="status">
        {bad === 0
          ? 'All systems healthy'
          : `${bad} of ${status.services.length} services need attention`}
      </div>
      <h2>Services</h2>
      <ul className="grid">
        {status.services.map((s) => (
          <ServiceCard key={s.name} service={s} />
        ))}
      </ul>
      <h2>Dependencies</h2>
      <ul className="rows">
        {status.dependencies.map((d) => (
          <DependencyRow key={d.name} dependency={d} />
        ))}
      </ul>
      <p className="muted">Last checked {new Date(status.checked_at).toLocaleTimeString()}</p>
    </>
  )
}

const TOKEN_KEY = 'circuitbreaker.admin_token'

function storedToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null // storage can be blocked (private windows); the page still works without it
  }
}

function storeToken(token: string | null) {
  try {
    if (token === null) sessionStorage.removeItem(TOKEN_KEY)
    else sessionStorage.setItem(TOKEN_KEY, token)
  } catch {
    // not remembered across reloads; nothing else breaks
  }
}

/** A-04: the status report is admin-only, so the presenter signs in with the passcode first. */
function AdminLogin({ onToken }: { onToken: (token: string) => void }) {
  const [nickname, setNickname] = useState('presenter')
  const [passcode, setPasscode] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    try {
      onToken(await adminLogin(nickname, passcode))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <form className="login" onSubmit={(e) => void submit(e)} aria-label="Admin sign-in">
      <p className="muted">
        The system status is for the admin. Sign in with the presenter passcode.
      </p>
      <label>
        Nickname <input value={nickname} onChange={(e) => setNickname(e.target.value)} />
      </label>
      <label>
        Passcode{' '}
        <input type="password" value={passcode} onChange={(e) => setPasscode(e.target.value)} />
      </label>
      <button type="submit">Sign in</button>
      {error && (
        <p className="problem" role="alert">
          {error}
        </p>
      )}
    </form>
  )
}

export function StatusPage({ intervalMs }: { intervalMs?: number }) {
  const [token, setToken] = useState<string | null>(storedToken)
  const snapshot = useSystemStatus(token, intervalMs)

  function signedIn(value: string) {
    storeToken(value)
    setToken(value)
  }

  return (
    <main>
      <header>
        <h1>CircuitBreaker</h1>
        <p className="muted">System status · refreshes every few seconds</p>
      </header>
      {snapshot.kind === 'loading' && <p className="muted">Checking…</p>}
      {snapshot.kind === 'needs_login' && <AdminLogin onToken={signedIn} />}
      {snapshot.kind === 'gateway_unreachable' && (
        <div className="banner banner-bad" role="alert">
          Cannot reach the API gateway. {snapshot.error}
        </div>
      )}
      {snapshot.kind === 'ok' && <Report status={snapshot.status} />}
    </main>
  )
}
