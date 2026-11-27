// The incident feed's state (Q-19): snapshot, updates in order and once, the presenter's pending
// decision, after-effects as their own kind, the dashboard's one line, and vitals going stale.
import {
  applyFeed,
  cooldownOf,
  emptyFeed,
  type FeedMessage,
  type FeedState,
  type IncidentSummary,
  lineOf,
  outcomeOf,
  statusLine,
  VITALS_STALE_MS,
  vitalsStale,
  withIncident,
} from './incidents'

const OPENED: IncidentSummary = {
  incident_id: 'INC-0052',
  alert_name: 'OrderServiceHighErrorRate',
  service: 'order-service',
  severity: 'critical',
  status: 'OPEN',
  resolution: null,
  summary: 'order-service errors at 30.75% (normally under 1%)',
  opened_at: '2026-11-26T21:23:25.957Z',
  resolved_at: null,
  time_to_recover_seconds: null,
  actions_taken: [],
}

function apply(state: FeedState, ...messages: FeedMessage[]): FeedState {
  return messages.reduce((s, m) => applyFeed(s, m, 1_000), state)
}

const update = (kind: string, message: string, at: string): FeedMessage =>
  ({
    type: 'incident_update',
    data: { incident_id: 'INC-0052', entry: { at, kind, message } },
  }) as FeedMessage

test('updates are added in order, once, and only to incidents the feed knows', () => {
  const s = apply(
    emptyFeed,
    update('OPENED', 'too early', '2026-11-26T21:23:20.000Z'), // before its summary: ignored
    { type: 'incident_opened', data: OPENED },
    update('OPENED', OPENED.summary, '2026-11-26T21:23:25.957Z'),
    update('EXECUTED', 'Scaled order-service from 2 to 5 replicas.', '2026-11-26T21:23:28.355Z'),
    update('EXECUTED', 'Scaled order-service from 2 to 5 replicas.', '2026-11-26T21:23:28.355Z'),
  )
  expect(s.incidents['INC-0052']?.entries.map((e) => e.kind)).toEqual(['OPENED', 'EXECUTED'])
})

test('a proposal waits for the presenter until a decision line arrives', () => {
  const remediation = {
    remediation_id: 'r-1',
    action: 'rollback_deployment',
    target_service: 'order-service',
    parameters: { to_revision: 52 },
    explanation: 'A new release.',
    model: 'anthropic/claude-sonnet-5.5',
    status: 'PROPOSED' as const,
    requires_approval: true,
  }
  let s = apply(
    emptyFeed,
    { type: 'incident_opened', data: OPENED },
    { type: 'approval_required', data: { incident_id: 'INC-0052', remediation } },
  )
  expect(s.incidents['INC-0052']?.pending?.remediation_id).toBe('r-1')
  expect(outcomeOf(s.incidents['INC-0052']!.summary)).toBe('Waiting for the presenter')
  expect(statusLine(s)).toBe('INC-0052 order-service: waiting for the presenter')
  s = apply(s, update('DECLINED', 'Declined by presenter.', '2026-11-26T21:24:00.000Z'))
  expect(s.incidents['INC-0052']?.pending).toBeNull()
  expect(s.incidents['INC-0052']?.summary.status).toBe('OPEN')
})

test('the snapshot is the truth on a new connection; a timeline read later fills in, in order', () => {
  let s = apply(emptyFeed, { type: 'snapshot', data: [OPENED] })
  s = apply(s, update('EXECUTED', 'Scaled.', '2026-11-26T21:23:28.355Z'))
  s = withIncident(s, {
    ...OPENED,
    pending_remediation: null,
    timeline: [
      { at: '2026-11-26T21:23:25.957Z', kind: 'OPENED', message: OPENED.summary },
      { at: '2026-11-26T21:23:28.355Z', kind: 'EXECUTED', message: 'Scaled.' },
    ],
  })
  expect(s.incidents['INC-0052']?.entries.map((e) => e.kind)).toEqual(['OPENED', 'EXECUTED'])
  const resolved = { ...OPENED, status: 'RESOLVED' as const, resolution: 'RECOVERED' as const }
  s = apply(s, {
    type: 'incident_resolved',
    data: { ...resolved, time_to_recover_seconds: 38 },
  })
  expect(outcomeOf(s.incidents['INC-0052']!.summary)).toBe('Recovered in 38 s')
  expect(statusLine(s)).toBeNull() // nothing open: no line on the dashboard
})

test('every step kind reads as a word with a tone, the after-effect too', () => {
  expect(lineOf({ at: '', kind: 'AFTER_EFFECT', message: '' })).toEqual({
    label: 'After-effect',
    tone: 'quiet',
  })
  expect(lineOf({ at: '', kind: 'OPENED', message: '' }).tone).toBe('bad')
  expect(lineOf({ at: '', kind: 'RESOLVED', message: '' }).tone).toBe('good')
  expect(lineOf({ at: '', kind: 'EXECUTED', message: '' }).label).toBe('Action')
})

test('vitals go stale after 15 s without a new one', () => {
  expect(vitalsStale(emptyFeed, 0)).toBe(true) // none yet
  const vitals = {
    at: '2026-11-26T21:23:30.000Z',
    service: 'order-service',
    requests_per_second: 71.5,
    error_ratio: 0.2,
    p95_seconds: 2.3,
    replicas: { desired: 5, ready: 3 },
    market_data_memory_of_limit: 0.27,
  }
  const s = applyFeed(emptyFeed, { type: 'vitals', data: vitals }, 100_000)
  expect(vitalsStale(s, 100_000 + VITALS_STALE_MS)).toBe(false)
  expect(vitalsStale(s, 100_000 + VITALS_STALE_MS + 1)).toBe(true)
})

test('a service with no cooldown has 0 left', () => {
  const settings = {
    environment: 'prod',
    approval_required: false,
    paused: false,
    model: 'm',
    budget: { actions_left_this_hour: 5, cooldown_seconds_by_service: { 'order-service': 120 } },
  }
  expect(cooldownOf(settings, 'order-service')).toBe(120)
  expect(cooldownOf(settings, 'market-data-service')).toBe(0)
  expect(cooldownOf(null, 'order-service')).toBe(0)
})
