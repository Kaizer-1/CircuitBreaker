import {
  countdown,
  type Experiment,
  latestByScenario,
  readinessLine,
  type Scenario,
  stateLine,
} from './chaos'

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

const scenario = (over: Partial<Scenario>): Scenario => ({
  scenario: 'TRAFFIC_SPIKE',
  name: 'Panic sell',
  description: '',
  target_service: 'order-service',
  expected_fix: 'scale_deployment',
  default_duration_seconds: 300,
  default_parameters: {},
  ...over,
})

test("a scenario whose service is in the agent's cooldown says it is waiting, and how long", () => {
  const budget = {
    actions_left_this_hour: 4,
    cooldown_seconds_by_service: { 'order-service': 82 },
    read_ms_ago: 2_000, // the countdown runs on between Q-20 polls
  }
  expect(readinessLine(scenario({}), undefined, budget, NOW)).toEqual({
    text: 'Waiting: agent cooldown 1:20',
    waiting: true,
  })
  const fixed = experiment({ status: 'REMEDIATED', time_to_recover_seconds: 41 })
  expect(readinessLine(scenario({}), fixed, budget, NOW).text).toBe(
    'Fixed by the agent in 41 s · waiting: agent cooldown 1:20',
  )
  // Another service's scenario, a decline the agent answers with no action, a running one: no wait.
  const leak = scenario({
    target_service: 'market-data-service',
    expected_fix: 'restart_deployment',
  })
  expect(readinessLine(leak, undefined, budget, NOW)).toEqual({ text: 'Ready', waiting: false })
  const decline = scenario({ target_service: 'order-service', expected_fix: 'do_nothing' })
  expect(readinessLine(decline, undefined, budget, NOW).waiting).toBe(false)
  expect(readinessLine(scenario({}), experiment({}), budget, NOW).waiting).toBe(false)
  // The countdown has run out since the poll; the hourly budget used up waits too.
  expect(readinessLine(scenario({}), undefined, { ...budget, read_ms_ago: 82_000 }, NOW).text).toBe(
    'Ready',
  )
  const spent = { ...budget, actions_left_this_hour: 0, cooldown_seconds_by_service: {} }
  expect(readinessLine(leak, undefined, spent, NOW).text).toBe(
    "Waiting: the agent's hourly budget is used up",
  )
  expect(readinessLine(scenario({}), undefined, null, NOW).text).toBe('Ready') // agent unreachable
})
