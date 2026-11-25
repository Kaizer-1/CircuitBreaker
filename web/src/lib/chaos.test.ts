import { countdown, type Experiment, latestByScenario, stateLine } from './chaos'

const NOW = Date.parse('2026-11-25T09:02:00.000Z')

function experiment(over: Partial<Experiment>): Experiment {
  return {
    experiment_id: 'e1',
    scenario: 'BAD_DEPLOY',
    target_service: 'order-service',
    status: 'RUNNING',
    parameters: {},
    started_at: '2026-11-25T09:00:00.000Z',
    ends_at: '2026-11-25T09:05:00.000Z',
    ended_at: null,
    incident_id: null,
    time_to_detect_seconds: null,
    time_to_recover_seconds: null,
    agent_actions: [],
    timeline: [],
    ...over,
  }
}

test('a scenario says whether it is ready, running or how it ended', () => {
  expect(stateLine(undefined, NOW)).toBe('Ready')
  expect(stateLine(experiment({}), NOW)).toBe('Running · 3:00 left · waiting for the agent')
  expect(stateLine(experiment({ incident_id: 'INC-0007', time_to_detect_seconds: 41 }), NOW)).toBe(
    'Running · 3:00 left · INC-0007 detected in 41 s',
  )
  expect(stateLine(experiment({ status: 'REMEDIATED', time_to_recover_seconds: 136 }), NOW)).toBe(
    'Fixed by the agent in 136 s',
  )
  expect(stateLine(experiment({ status: 'STOPPED' }), NOW)).toBe('Stopped')
  expect(stateLine(experiment({ status: 'EXPIRED' }), NOW)).toBe('Timed out')
})

test('countdowns never go below zero and the newest experiment of each scenario wins', () => {
  expect([countdown(125), countdown(-4), countdown(59.6)]).toEqual(['2:05', '0:00', '1:00'])
  const newest = experiment({ experiment_id: 'new' })
  const latest = latestByScenario([newest, experiment({ experiment_id: 'old' })])
  expect(latest.get('BAD_DEPLOY')?.experiment_id).toBe('new')
})
