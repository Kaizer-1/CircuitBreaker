// The Chaos Panel (presenter only; J-15, J-16 → Q-21, Q-22, A-10, A-11, A-20): four buttons that
// break the exchange on purpose, each with its state, and one that puts everything back. The SRE
// agent never sees this screen or chaos-service: it works from symptoms (ADR-0020).
//
// A scenario that would touch a service another running experiment already affects is refused by
// chaos-service (409); the panel shows its reason. The reset ends every experiment and undoes every
// fault, including the replicas the agent added during a panic sell.
import { useCallback, useEffect, useState } from 'react'
import { FKeys, PresenterGate, Sections } from '../components/PresenterNav'
import { ThemeToggle } from '../components/ThemeToggle'
import {
  chaos,
  type Experiment,
  latestByScenario,
  type ResetResult,
  type Scenario,
  type ScenarioId,
  stateLine,
} from '../lib/chaos'
import { clock, dateline } from '../lib/format'
import { ApiError } from '../lib/market'
import { useNow } from './presenterHooks'

const POLL_MS = 2_000
const TIMELINE_ROWS = 14

const FIXES: Record<string, string> = {
  rollback_deployment: 'Roll back order-service',
  restart_deployment: 'Restart market-data-service',
  scale_deployment: 'Scale order-service up',
  do_nothing: 'Do nothing, and escalate',
}

export function Chaos() {
  return (
    <PresenterGate purpose="The Chaos Panel is for the presenter. Sign in with the presenter passcode.">
      {(token, onExpired) => <Screen token={token} onExpired={onExpired} />}
    </PresenterGate>
  )
}

interface ChaosState {
  scenarios: Scenario[]
  experiments: Experiment[]
  resets: ResetResult[]
  reachable: boolean
}

function useChaos(token: string, onExpired: () => void) {
  const [state, setState] = useState<ChaosState>({
    scenarios: [],
    experiments: [],
    resets: [],
    reachable: true,
  })
  const [asked, setAsked] = useState(0) // bumped after an action: read again at once
  useEffect(() => {
    let stop = false
    const load = () =>
      Promise.all([chaos.scenarios(token), chaos.experiments(token)])
        .then(([scenarios, list]) => {
          if (!stop)
            setState({ scenarios, experiments: list.items, resets: list.resets, reachable: true })
        })
        .catch((e: unknown) => {
          if (e instanceof ApiError && e.status === 401) onExpired()
          else if (!stop) setState((s) => ({ ...s, reachable: false }))
        })
    void load()
    const id = window.setInterval(() => void load(), POLL_MS)
    return () => {
      stop = true
      window.clearInterval(id)
    }
  }, [token, onExpired, asked])
  const refresh = useCallback(() => setAsked((n) => n + 1), [])
  return { ...state, refresh }
}

interface TimelineRow {
  at: string
  who: string
  message: string
}

function timeline(experiments: Experiment[], resets: ResetResult[], names: Map<string, string>) {
  const rows: TimelineRow[] = []
  for (const e of experiments)
    for (const t of e.timeline)
      rows.push({ at: t.at, who: names.get(e.scenario) ?? e.scenario, message: t.message })
  for (const r of resets)
    rows.push({
      at: r.reset_at,
      who: 'Presenter reset',
      message: r.undone.length ? `${r.undone.join('; ')}.` : 'Nothing was left to undo.',
    })
  return rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, TIMELINE_ROWS)
}

function Screen({ token, onExpired }: { token: string; onExpired: () => void }) {
  const { scenarios, experiments, resets, reachable, refresh } = useChaos(token, onExpired)
  const now = useNow()
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const latest = latestByScenario(experiments)
  const running = experiments.filter((e) => e.status === 'RUNNING')
  const names = new Map(scenarios.map((s) => [s.scenario as string, s.name]))

  const act = async (key: string, work: () => Promise<unknown>, done: string) => {
    setBusy(key)
    setNotice(null)
    try {
      await work()
      setNotice(done)
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : 'chaos-service did not answer.')
    } finally {
      setBusy(null)
      refresh()
    }
  }
  const start = (s: Scenario) =>
    act(s.scenario, () => chaos.start(token, s.scenario as ScenarioId), `${s.name} started.`)
  const stop = (s: Scenario, e: Experiment) =>
    act(s.scenario, () => chaos.stop(token, e.experiment_id), `${s.name} stopped.`)
  const reset = () =>
    act(
      'reset',
      () => chaos.reset(token),
      'Reset: every fault undone, the exchange is back to normal.',
    )

  return (
    <div className="dash chaos">
      <header className="dash-head">
        <div className="title paper-only">
          <h1 className="masthead">
            CircuitBreaker <em>Markets</em>
          </h1>
          <Sections active="chaos" />
        </div>
        <span className="dateline paper-only">{dateline(now)} · the chaos desk</span>
        <div className="command terminal-only" aria-label="Command line">
          <span className="badge">CIRCUITBREAKER</span>
          <span>
            &gt; <span className="typed">CHAOS</span> PANEL
            <span className="cursor" aria-hidden="true" />
          </span>
        </div>
        <div className="right">
          <span>
            Live · <b>{clock(now)}</b> IST
          </span>
          <ThemeToggle />
        </div>
      </header>
      <FKeys active="chaos" />

      <div className="chaos-strip" role="status" aria-label="Chaos status">
        {!reachable ? (
          <span className="down">● chaos-service unreachable</span>
        ) : running.length ? (
          <span className="down">
            ● {running.length} experiment{running.length > 1 ? 's' : ''} running
          </span>
        ) : (
          <span className="up">● No chaos: the exchange is running normally</span>
        )}
        {notice && <span className="notice">{notice}</span>}
        <button
          type="button"
          className="chaos-button reset"
          disabled={busy !== null}
          onClick={() => void reset()}
        >
          {busy === 'reset' ? 'Resetting…' : 'Reset everything'}
        </button>
      </div>

      <section className="panel chaos-scenarios" aria-label="Scenarios">
        <h2>
          Scenarios <small>The agent sees only the symptoms, never this panel</small>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Scenario</th>
              <th>What breaks</th>
              <th>The right fix</th>
              <th>State</th>
              <th aria-label="Action" />
            </tr>
          </thead>
          <tbody>
            {scenarios.map((s) => {
              const e = latest.get(s.scenario as ScenarioId)
              const on = e?.status === 'RUNNING'
              return (
                <tr
                  key={s.scenario}
                  className={on ? 'running' : undefined}
                  data-testid={s.scenario}
                >
                  <th scope="row">{s.name}</th>
                  <td className="what">{s.description}</td>
                  <td>{FIXES[s.expected_fix] ?? s.expected_fix}</td>
                  <td className={`state${on ? ' down' : ''}`}>{stateLine(e, now.getTime())}</td>
                  <td className="action">
                    {on && e ? (
                      <button
                        type="button"
                        className="chaos-button stop"
                        disabled={busy !== null}
                        onClick={() => void stop(s, e)}
                      >
                        Stop
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="chaos-button"
                        disabled={busy !== null}
                        onClick={() => void start(s)}
                      >
                        {busy === s.scenario ? 'Starting…' : 'Start'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>

      <section className="panel chaos-timeline" aria-label="What happened">
        <h2>
          What happened <small>chaos-service's own record, newest first</small>
        </h2>
        <table>
          <tbody>
            {timeline(experiments, resets, names).map((row, n) => (
              <tr key={`${row.at}-${n}`}>
                <td className="num">{clock(new Date(row.at))}</td>
                <th scope="row">{row.who}</th>
                <td>{row.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
