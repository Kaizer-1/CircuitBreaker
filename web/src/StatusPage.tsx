import { useState } from 'react'
import { type DependencyReport, type ServiceReport, type SystemStatus } from './api'
import { AdminLogin } from './components/AdminLogin'
import { ThemeToggle } from './components/ThemeToggle'
import { loadAdminToken, saveAdminToken } from './lib/session'
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

export function StatusPage({ intervalMs }: { intervalMs?: number }) {
  const [token, setToken] = useState<string | null>(loadAdminToken)
  const snapshot = useSystemStatus(token, intervalMs)

  function signedIn(value: string) {
    saveAdminToken(value)
    setToken(value)
  }

  return (
    <main className="status-page">
      <header>
        <div>
          <h1 className="masthead paper-only">
            CircuitBreaker <em>System</em>
          </h1>
          <div className="command terminal-only">
            <span className="badge">CIRCUITBREAKER</span>
            <span>
              &gt; <span className="typed">SYSTEM</span> STATUS
              <span className="cursor" aria-hidden="true" />
            </span>
          </div>
          <p className="muted">System status · refreshes every few seconds</p>
        </div>
        <ThemeToggle />
      </header>
      {snapshot.kind === 'loading' && <p className="muted">Checking…</p>}
      {snapshot.kind === 'needs_login' && (
        <AdminLogin
          onToken={signedIn}
          purpose="The system status is for the admin. Sign in with the presenter passcode."
        />
      )}
      {snapshot.kind === 'gateway_unreachable' && (
        <div className="banner banner-bad" role="alert">
          Cannot reach the API gateway. {snapshot.error}
        </div>
      )}
      {snapshot.kind === 'ok' && <Report status={snapshot.status} />}
    </main>
  )
}
