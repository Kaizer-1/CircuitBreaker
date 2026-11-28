// The SRE agent through api-gateway (api/sre-agent.yaml): Q-18 one incident, Q-20 settings with the
// guardrails' budget, A-14 the approval toggle, A-15 approve or decline; and the live feed Q-19
// (api/README.md "Q-19"), whose state is kept here as plain functions so it can be tested alone.
import { call } from './market'

export type TimelineKind =
  | 'OPENED'
  | 'EVIDENCE_COLLECTED'
  | 'DIAGNOSIS'
  | 'PROPOSED'
  | 'APPROVED'
  | 'DECLINED'
  | 'EXECUTED'
  | 'VERIFIED'
  | 'RESOLVED'
  | 'ESCALATED'
  | 'AFTER_EFFECT'
  | 'SYMPTOM'

export interface TimelineEntry {
  at: string
  kind: TimelineKind
  message: string
}

export interface IncidentSummary {
  incident_id: string
  alert_name: string
  service: string
  severity: 'warning' | 'critical'
  status: 'OPEN' | 'AWAITING_APPROVAL' | 'RESOLVED'
  resolution: 'RECOVERED' | 'AUTO_CLEARED' | 'ESCALATED' | null
  summary: string
  opened_at: string
  resolved_at: string | null
  time_to_recover_seconds: number | null
  actions_taken: string[]
  fired_at?: string | null
  mitigated_at?: string | null
  recovered_at?: string | null
}

export interface Remediation {
  remediation_id: string
  action: string
  target_service: string | null
  parameters: { replicas?: number; to_revision?: number }
  explanation: string
  model: string
  status: 'PROPOSED' | 'APPROVED' | 'DECLINED' | 'EXECUTED' | 'FAILED'
  requires_approval: boolean
}

export interface Incident extends IncidentSummary {
  pending_remediation: Remediation | null
  timeline: TimelineEntry[]
}

export interface AgentSettings {
  environment: string
  approval_required: boolean
  paused: boolean
  model: string
  budget?: { actions_left_this_hour: number; cooldown_seconds_by_service: Record<string, number> }
}

export interface Vitals {
  at: string
  service: string
  requests_per_second: number | null
  error_ratio: number | null
  p95_seconds: number | null
  replicas: { desired: number | null; ready: number | null }
  market_data_memory_of_limit: number | null
}

export const agent = {
  incident: (token: string, id: string) => call<Incident>('GET', `/v1/incidents/${id}`, token),
  settings: (token: string) => call<AgentSettings>('GET', '/v1/agent/settings', token),
  setApproval: (token: string, on: boolean) =>
    call<AgentSettings>('PUT', '/v1/agent/settings', token, { approval_required: on }),
  decide: (token: string, id: string, decision: 'APPROVE' | 'DECLINE', comment?: string) =>
    call<Incident>('POST', `/v1/incidents/${id}/decision`, token, {
      decision,
      ...(comment ? { comment } : {}),
    }),
}

// ------------------------------------------------------------------ the feed's state

export interface FeedIncident {
  summary: IncidentSummary
  entries: TimelineEntry[] // oldest first
  pending: Remediation | null // waiting for the presenter's decision (approval mode)
}

export interface FeedState {
  incidents: Record<string, FeedIncident>
  vitals: Vitals | null
  vitalsReceivedAt: number | null // the browser's clock, when the last vitals arrived
  // Escalated incidents whose alert still fires: resolved for the agent, not for the system.
  withAPerson: string[]
}

export const emptyFeed: FeedState = {
  incidents: {},
  vitals: null,
  vitalsReceivedAt: null,
  withAPerson: [],
}

export type FeedMessage =
  | { type: 'snapshot'; data: IncidentSummary[] }
  | { type: 'incident_opened'; data: IncidentSummary }
  | { type: 'incident_update'; data: { incident_id: string; entry: TimelineEntry } }
  | { type: 'approval_required'; data: { incident_id: string; remediation: Remediation } }
  | { type: 'incident_resolved'; data: IncidentSummary }
  | { type: 'vitals'; data: Vitals }
  | { type: 'with_a_person'; data: { incident_ids: string[] } }

function upsert(state: FeedState, summary: IncidentSummary): FeedState {
  const known = state.incidents[summary.incident_id]
  const pending = summary.status === 'AWAITING_APPROVAL' ? (known?.pending ?? null) : null
  return {
    ...state,
    incidents: {
      ...state.incidents,
      [summary.incident_id]: { summary, entries: known?.entries ?? [], pending },
    },
  }
}

/** One feed message applied; `receivedAt` is when it arrived (Date.now()). */
export function applyFeed(state: FeedState, message: FeedMessage, receivedAt: number): FeedState {
  switch (message.type) {
    case 'snapshot': {
      // A fresh connection: the snapshot is the truth; entries already seen are kept.
      const incidents: Record<string, FeedIncident> = {}
      for (const s of message.data) {
        const known = state.incidents[s.incident_id]
        incidents[s.incident_id] = {
          summary: s,
          entries: known?.entries ?? [],
          pending: s.status === 'AWAITING_APPROVAL' ? (known?.pending ?? null) : null,
        }
      }
      return { ...state, incidents }
    }
    case 'incident_opened':
    case 'incident_resolved':
      return upsert(state, message.data)
    case 'incident_update': {
      const { incident_id, entry } = message.data
      const known = state.incidents[incident_id]
      if (!known) return state // its incident_opened (or the snapshot) brings the summary
      if (known.entries.some((e) => e.at === entry.at && e.message === entry.message)) return state
      // A decision ends the wait (only incident_resolved carries a new summary).
      const decided = entry.kind === 'APPROVED' || entry.kind === 'DECLINED'
      const summary =
        decided && known.summary.status === 'AWAITING_APPROVAL'
          ? { ...known.summary, status: 'OPEN' as const }
          : known.summary
      return {
        ...state,
        incidents: {
          ...state.incidents,
          [incident_id]: {
            summary,
            entries: [...known.entries, entry],
            pending: decided ? null : known.pending,
          },
        },
      }
    }
    case 'approval_required': {
      const known = state.incidents[message.data.incident_id]
      if (!known) return state
      return {
        ...state,
        incidents: {
          ...state.incidents,
          [message.data.incident_id]: {
            ...known,
            summary: { ...known.summary, status: 'AWAITING_APPROVAL' },
            pending: message.data.remediation,
          },
        },
      }
    }
    case 'vitals':
      return { ...state, vitals: message.data, vitalsReceivedAt: receivedAt }
    case 'with_a_person':
      return { ...state, withAPerson: message.data.incident_ids }
  }
}

/** Fill in an incident's timeline from Q-18 (a screen that connected after it opened). */
export function withIncident(state: FeedState, incident: Incident): FeedState {
  const known = state.incidents[incident.incident_id]
  const entries = [
    ...incident.timeline,
    ...(known?.entries ?? []).filter(
      (e) => !incident.timeline.some((t) => t.at === e.at && t.message === e.message),
    ),
  ].sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
  return {
    ...state,
    incidents: {
      ...state.incidents,
      [incident.incident_id]: {
        summary: known?.summary ?? incident,
        entries,
        pending:
          incident.status === 'AWAITING_APPROVAL'
            ? incident.pending_remediation
            : (known?.pending ?? null),
      },
    },
  }
}

/** Newest first. */
export function incidentsNewestFirst(state: FeedState): FeedIncident[] {
  return Object.values(state.incidents).sort(
    (a, b) => Date.parse(b.summary.opened_at) - Date.parse(a.summary.opened_at),
  )
}

export function openIncidents(state: FeedState): FeedIncident[] {
  return incidentsNewestFirst(state).filter((i) => i.summary.status !== 'RESOLVED')
}

// ------------------------------------------------------------------ vitals: fresh or stale

/** After this long without a vitals message the strip greys out (owner, 2026-11-27). */
export const VITALS_STALE_MS = 15_000

export function vitalsStale(state: FeedState, now: number): boolean {
  return state.vitalsReceivedAt === null || now - state.vitalsReceivedAt > VITALS_STALE_MS
}

// ------------------------------------------------------------------ how a line reads

const LABELS: Record<TimelineKind, string> = {
  OPENED: 'Opened',
  EVIDENCE_COLLECTED: 'Evidence',
  DIAGNOSIS: 'Diagnosis',
  PROPOSED: 'Proposed',
  APPROVED: 'Approved',
  DECLINED: 'Declined',
  EXECUTED: 'Action',
  VERIFIED: 'Check',
  RESOLVED: 'Resolved',
  ESCALATED: 'Escalated',
  AFTER_EFFECT: 'After-effect',
  SYMPTOM: 'Symptom',
}

/** The kind's label and its tone (a colour token on the screen). */
export function lineOf(entry: TimelineEntry): {
  label: string
  tone: 'bad' | 'good' | 'act' | 'warn' | 'quiet'
} {
  const label = LABELS[entry.kind] ?? entry.kind
  switch (entry.kind) {
    case 'OPENED':
      return { label, tone: 'bad' }
    case 'RESOLVED':
      return { label, tone: 'good' }
    case 'EXECUTED':
    case 'APPROVED':
      return { label, tone: 'act' }
    case 'ESCALATED':
    case 'DECLINED':
    case 'PROPOSED':
      return { label, tone: 'warn' }
    default:
      return { label, tone: 'quiet' }
  }
}

/** The incident's headline outcome, every time with its starting point:
 * "Recovered 49 s after detection" (detection = the incident opened). */
export function outcomeOf(summary: IncidentSummary): string {
  if (summary.status === 'AWAITING_APPROVAL') return 'Waiting for the presenter'
  if (summary.status !== 'RESOLVED') return 'In progress'
  switch (summary.resolution) {
    case 'RECOVERED':
      return summary.time_to_recover_seconds !== null
        ? `Recovered ${summary.time_to_recover_seconds}${NBSP}s after detection`
        : 'Recovered'
    case 'AUTO_CLEARED':
      return 'Cleared by itself'
    case 'ESCALATED':
      return 'Escalated to a person'
    default:
      return 'Resolved'
  }
}

/** The one line for the dashboard's status bar: the newest open incident, else the newest one
 * handed to a person while its alert still fires, or null. */
export function statusLine(state: FeedState): string | null {
  const open = openIncidents(state)
  const top = open[0]
  if (!top) {
    const held = withAPerson(state)[0]
    if (!held) return null
    const s = held.summary
    return `${s.incident_id} ${s.service}: with a person, ${s.alert_name} still fires`
  }
  const last = top.entries.filter((e) => e.kind !== 'EVIDENCE_COLLECTED').at(-1)
  const what = top.pending
    ? 'waiting for the presenter'
    : last
      ? `${lineOf(last).label.toLowerCase()}: ${firstSentence(last.message)}`
      : top.summary.summary
  const more = open.length > 1 ? ` (+${open.length - 1} more)` : ''
  return `${top.summary.incident_id} ${top.summary.service}: ${what}${more}`
}

function firstSentence(text: string): string {
  return splitSentence(text)[0].replace(/[.!?]$/, '')
}

const NBSP = '\u00a0' // a number and its unit stay on one line

/** A step's first sentence (shown bold, one line) and the rest (its reasoning, smaller). */
export function splitSentence(text: string): [string, string] {
  const cut = text.search(/[.!?](\s|$)/)
  if (cut < 0) return [text, '']
  return [text.slice(0, cut + 1), text.slice(cut + 1).trim()]
}

/** Incidents the agent handed to a person whose alert still fires, newest first. */
export function withAPerson(state: FeedState): FeedIncident[] {
  return incidentsNewestFirst(state).filter((i) =>
    state.withAPerson.includes(i.summary.incident_id),
  )
}

/** "1 incident open", "1 incident open · 1 with a person", "1 incident with a person": the
 * dashboard's status, or null when nothing is open and no person holds a still-firing one. */
export function openLabel(state: FeedState): string | null {
  const n = openIncidents(state).length
  const m = state.withAPerson.length
  const incidents = (k: number) => `${k} incident${k > 1 ? 's' : ''}`
  if (n && m) return `${incidents(n)} open · ${m} with a person`
  if (n) return `${incidents(n)} open`
  return m ? `${incidents(m)} with a person` : null
}

export interface Aside {
  kind: 'SYMPTOM' | 'AFTER_EFFECT'
  subject: string // "GatewayHighErrorRate on api-gateway", or the after-effect's alert
  state: string // what became of it, in a few words
  at: string // the latest line's time
}

/** Symptom and after-effect lines folded into one line each per alert: they hang under the
 * root incident, collapsed, instead of standing between its steps (owner, 2026-11-27). */
export function asides(entries: TimelineEntry[]): Aside[] {
  const byKey = new Map<string, Aside>()
  for (const e of entries) {
    if (e.kind !== 'SYMPTOM' && e.kind !== 'AFTER_EFFECT') continue
    const subject = e.message.match(/^(\S+ on \S+|\S+)/)?.[1] ?? e.message
    const key = `${e.kind}|${subject.split(' ')[0]}`
    const state = /cleared|drained/.test(e.message)
      ? 'cleared with its cause'
      : /handed to a person/.test(e.message)
        ? 'handed to a person'
        : e.kind === 'SYMPTOM'
          ? 'no action'
          : 'draining, no action'
    byKey.set(key, { kind: e.kind, subject: subject.replace(/:$/, ''), state, at: e.at })
  }
  return [...byKey.values()]
}

/** Seconds left of the agent's cooldown on a service, 0 if none. */
export function cooldownOf(settings: AgentSettings | null, service: string): number {
  return settings?.budget?.cooldown_seconds_by_service[service] ?? 0
}
