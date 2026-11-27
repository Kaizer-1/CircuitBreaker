// The control room (presenter only; owner, 2026-11-27): the Chaos Panel's four scenarios on the left
// (J-15, J-16 → Q-21, Q-22, A-10, A-11, A-20), the SRE agent's live incident feed on the right
// (Q-19, with Approve / Decline A-15 and the approval toggle A-14), and above both a market strip:
// the tickers' prices and the numbers the agent sees. So the audience watches a fault, its impact
// and the agent's fix on one screen. The agent never sees this screen or chaos-service: it works
// from symptoms (ADR-0020); only this screen puts the two side by side.
//
// A scenario that would touch a service another running experiment already affects is refused by
// chaos-service (409); the panel shows its reason. While the agent's cooldown on a scenario's
// service runs (or its hourly budget is used up) the scenario says it is waiting, and for how long.
// The reset ends every experiment and undoes every fault, including the replicas the agent added.
import { useCallback, useEffect, useState } from 'react'
import { IncidentFeed, type ResetLine } from '../components/IncidentFeed'
import { MarketStrip } from '../components/MarketStrip'
import { FKeys, PresenterGate, Sections } from '../components/PresenterNav'
import { ThemeToggle } from '../components/ThemeToggle'
import { useAgentSettings, useIncidentFeed } from '../incidents/hooks'
import {
  type AgentBudget,
  chaos,
  countdown,
  type Experiment,
  latestByScenario,
  readinessLine,
  type ResetResult,
  type Scenario,
  type ScenarioId,
} from '../lib/chaos'
import { clock, dateline } from '../lib/format'
import { agent } from '../lib/incidents'
import { ApiError } from '../lib/market'
import { useInstruments } from '../market/hooks'
import { useNow } from './presenterHooks'

const POLL_MS = 2_000
const TIMELINE_ROWS = 8

const FIXES: Record<string, string> = {
  rollback_deployment: 'Roll back order-service',
  restart_deployment: 'Restart market-data-service',
  scale_deployment: 'Scale order-service up',
  do_nothing: 'Do nothing, and escalate',
}

export function Chaos() {
  return (
    <PresenterGate purpose="The control room is for the presenter. Sign in with the presenter passcode.">
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

function resetLines(resets: ResetResult[]): ResetLine[] {
  return resets.map((r) => ({
    at: r.reset_at,
    message: r.undone.length ? `${r.undone.join('; ')}.` : 'Nothing was left to undo.',
  }))
}

function Screen({ token, onExpired }: { token: string; onExpired: () => void }) {
  const { scenarios, experiments, resets, reachable, refresh } = useChaos(token, onExpired)
  const [feed, feedState] = useIncidentFeed(token)
  const agentSettings = useAgentSettings(token, 2000)
  const instruments = useInstruments(token, 3000)
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
  const decide = (id: string, decision: 'APPROVE' | 'DECLINE') =>
    void act(
      `${id}:${decision}`,
      () => agent.decide(token, id, decision),
      decision === 'APPROVE' ? `${id}: approved, the agent acts.` : `${id}: declined, escalated.`,
    ).then(agentSettings.refresh)
  const settings = agentSettings.settings
  const toggleApproval = () =>
    void act(
      'approval',
      () => agent.setApproval(token, !settings?.approval_required),
      settings?.approval_required
        ? 'Approval off: the agent acts by itself.'
        : 'Approval on: the agent waits for you.',
    ).then(agentSettings.refresh)
  const budget: AgentBudget | null =
    settings?.budget && agentSettings.readAt !== null
      ? { ...settings.budget, read_ms_ago: now.getTime() - agentSettings.readAt }
      : null
  const cooldowns = budget
    ? Object.entries(budget.cooldown_seconds_by_service)
        .map(([service, seconds]) => [service, seconds - budget.read_ms_ago / 1000] as const)
        .filter(([, left]) => left >= 1)
    : []

  return (
    <div className="dash chaos control">
      <header className="dash-head">
        <div className="title paper-only">
          <h1 className="masthead">
            CircuitBreaker <em>Markets</em>
          </h1>
          <Sections active="chaos" />
        </div>
        <span className="dateline paper-only">{dateline(now)} · the control room</span>
        <div className="command terminal-only" aria-label="Command line">
          <span className="badge">CIRCUITBREAKER</span>
          <span>
            &gt; <span className="typed">CONTROL</span> ROOM
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

      <MarketStrip instruments={instruments} feed={feed} now={now.getTime()} />

      <div className="control-main">
        <div className="control-left">
          <section className="panel chaos-scenarios" aria-label="Scenarios">
            <h2>
              Scenarios <small>The agent sees only the symptoms, never this panel</small>
            </h2>
            <table>
              <thead>
                <tr>
                  <th>Scenario</th>
                  <th>The right fix</th>
                  <th>State</th>
                  <th aria-label="Action" />
                </tr>
              </thead>
              <tbody>
                {scenarios.map((s) => {
                  const e = latest.get(s.scenario as ScenarioId)
                  const on = e?.status === 'RUNNING'
                  const ready = readinessLine(s, e, budget, now.getTime())
                  return (
                    <tr
                      key={s.scenario}
                      className={on ? 'running' : undefined}
                      data-testid={s.scenario}
                    >
                      <th scope="row">
                        {s.name}
                        <small className="what">{s.description}</small>
                      </th>
                      <td>{FIXES[s.expected_fix] ?? s.expected_fix}</td>
                      <td className={`state${on ? ' down' : ready.waiting ? ' waiting' : ''}`}>
                        {ready.text}
                      </td>
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

        <section className="panel control-feed" aria-label="The SRE agent">
          <h2>
            The SRE agent{' '}
            <small>
              {feedState === 'live' ? 'Live feed' : 'Feed reconnecting…'}
              {settings ? ` · ${settings.model}` : ''}
            </small>
          </h2>
          <div className="agent-line" role="status" aria-label="The agent's settings and budget">
            {!agentSettings.reachable || !settings ? (
              <span className="down">● The agent is not answering</span>
            ) : (
              <>
                <span className={settings.approval_required ? 'waiting' : 'up'}>
                  ● Approval {settings.approval_required ? 'on: it waits for you' : 'off'}
                </span>
                <button
                  type="button"
                  className="chaos-button small"
                  disabled={busy !== null}
                  onClick={toggleApproval}
                >
                  {settings.approval_required ? 'Turn approval off' : 'Turn approval on'}
                </button>
                {budget && (
                  <span>
                    <b className="num">{budget.actions_left_this_hour}</b> of 6 actions left this
                    hour
                  </span>
                )}
                {cooldowns.map(([service, left]) => (
                  <span key={service}>
                    {service} cooldown <b className="num">{countdown(left)}</b>
                  </span>
                ))}
              </>
            )}
          </div>
          <IncidentFeed feed={feed} resets={resetLines(resets)} busy={busy} onDecide={decide} />
        </section>
      </div>
    </div>
  )
}
