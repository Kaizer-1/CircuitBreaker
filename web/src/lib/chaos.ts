// The Chaos Panel's API (api/chaos-service.yaml through api-gateway; admin only): Q-21 scenarios,
// Q-22 experiments and resets, A-10 start, A-11 stop, A-20 reset.
import { call } from './market'

export type ScenarioId = 'BAD_DEPLOY' | 'MEMORY_LEAK' | 'TRAFFIC_SPIKE' | 'PORTFOLIO_DB_DEGRADED'
export type ExperimentStatus = 'RUNNING' | 'REMEDIATED' | 'STOPPED' | 'EXPIRED'

export interface Scenario {
  scenario: ScenarioId
  name: string
  description: string
  target_service: string
  expected_fix: string
  default_duration_seconds: number
  default_parameters: Record<string, number>
}

export interface Experiment {
  experiment_id: string
  scenario: ScenarioId
  target_service: string
  status: ExperimentStatus
  parameters: Record<string, number>
  started_at: string
  ends_at: string
  ended_at: string | null
  incident_id: string | null
  time_to_detect_seconds: number | null
  time_to_recover_seconds: number | null
  agent_actions: string[]
  timeline: { at: string; message: string }[]
}

export interface ResetResult {
  reset_at: string
  undone: string[]
  failed: string[]
}

export const chaos = {
  scenarios: (token: string) =>
    call<{ scenarios: Scenario[] }>('GET', '/v1/chaos/scenarios', token).then((b) => b.scenarios),
  experiments: (token: string) =>
    call<{ items: Experiment[]; resets: ResetResult[] }>('GET', '/v1/chaos/experiments', token),
  start: (token: string, scenario: ScenarioId) =>
    call<Experiment>('POST', '/v1/chaos/experiments', token, { scenario }),
  stop: (token: string, experimentId: string) =>
    call<Experiment>('POST', `/v1/chaos/experiments/${experimentId}/stop`, token),
  reset: (token: string) => call<ResetResult>('POST', '/v1/chaos/reset', token),
}

/** The newest experiment of each scenario (the list is newest first). */
export function latestByScenario(items: Experiment[]): Map<ScenarioId, Experiment> {
  const latest = new Map<ScenarioId, Experiment>()
  for (const e of items) if (!latest.has(e.scenario)) latest.set(e.scenario, e)
  return latest
}

/** "2:05" for seconds left. */
export function countdown(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

const OUTCOMES: Record<Exclude<ExperimentStatus, 'RUNNING'>, string> = {
  REMEDIATED: 'Fixed by the agent',
  STOPPED: 'Stopped',
  EXPIRED: 'Timed out',
}

/** One line for a scenario's state on the panel. */
export function stateLine(e: Experiment | undefined, now: number): string {
  if (!e) return 'Ready'
  if (e.status === 'RUNNING') {
    const left = countdown((Date.parse(e.ends_at) - now) / 1000)
    const detected =
      e.time_to_detect_seconds !== null
        ? ` · ${e.incident_id} detected in ${e.time_to_detect_seconds} s`
        : ' · waiting for the agent'
    return `Running · ${left} left${detected}`
  }
  const recovered = e.time_to_recover_seconds !== null ? ` in ${e.time_to_recover_seconds} s` : ''
  return `${OUTCOMES[e.status]}${recovered}`
}
