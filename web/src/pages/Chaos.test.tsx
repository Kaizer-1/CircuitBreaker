// The control room in both skins: four scenarios with their right fix and state, start and stop,
// a refused start shows chaos-service's reason, the reset reaches chaos-service; beside them the
// agent's live feed with Approve / Decline and the approval toggle, the budget and cooldowns (a
// scenario whose service is cooling down says it is waiting), and the presenter's resets merged in.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { type Experiment, type Scenario, chaos } from '../lib/chaos'
import {
  agent,
  type AgentSettings,
  applyFeed,
  emptyFeed,
  type FeedState,
  type IncidentSummary,
} from '../lib/incidents'
import { ApiError } from '../lib/market'
import { saveAdminToken } from '../lib/session'
import { BOTH, showTheme } from '../../test/themes'
import { Chaos } from './Chaos'

const NOW = new Date('2026-11-25T09:02:00.000Z')
vi.mock('./presenterHooks', () => ({ useNow: () => NOW }))
const live = vi.hoisted(() => ({
  feed: null as FeedState | null,
  settings: null as AgentSettings | null,
  refresh: vi.fn(),
}))
vi.mock('../incidents/hooks', () => ({
  useIncidentFeed: () => [live.feed, 'live'],
  useAgentSettings: () => ({
    settings: live.settings,
    readAt: NOW.getTime(),
    reachable: live.settings !== null,
    refresh: live.refresh,
  }),
}))
vi.mock('../market/hooks', () => ({ useInstruments: () => [] }))

const SETTINGS: AgentSettings = {
  environment: 'prod',
  approval_required: true,
  paused: false,
  model: 'anthropic/claude-sonnet-5.5',
  budget: { actions_left_this_hour: 4, cooldown_seconds_by_service: { 'order-service': 80 } },
}
const INCIDENT: IncidentSummary = {
  incident_id: 'INC-0046',
  alert_name: 'OrderServiceHighErrorRate',
  service: 'order-service',
  severity: 'critical',
  status: 'OPEN',
  resolution: null,
  summary: 'order-service errors at 31% (normally under 1%)',
  opened_at: '2026-11-25T09:01:00.000Z',
  resolved_at: null,
  time_to_recover_seconds: null,
  actions_taken: [],
}

function feedWithProposal(): FeedState {
  let s = applyFeed(emptyFeed, { type: 'incident_opened', data: INCIDENT }, 0)
  s = applyFeed(
    s,
    {
      type: 'incident_update',
      data: {
        incident_id: 'INC-0046',
        entry: {
          at: '2026-11-25T09:01:11.000Z',
          kind: 'DIAGNOSIS',
          message: 'order-service fails with internal errors since release 1.4.1.',
        },
      },
    },
    0,
  )
  return applyFeed(
    s,
    {
      type: 'approval_required',
      data: {
        incident_id: 'INC-0046',
        remediation: {
          remediation_id: 'r-1',
          action: 'rollback_deployment',
          target_service: 'order-service',
          parameters: { to_revision: 52 },
          explanation: 'A new release.',
          model: 'anthropic/claude-sonnet-5.5',
          status: 'PROPOSED',
          requires_approval: true,
        },
      },
    },
    0,
  )
}

const SCENARIOS: Scenario[] = [
  ['BAD_DEPLOY', 'Bad deploy', 'rollback_deployment'],
  ['MEMORY_LEAK', 'Memory leak', 'restart_deployment'],
  ['TRAFFIC_SPIKE', 'Panic sell', 'scale_deployment'],
  ['PORTFOLIO_DB_DEGRADED', 'Portfolio database degraded', 'do_nothing'],
].map(([scenario, name, fix]) => ({
  scenario: scenario as Scenario['scenario'],
  name,
  description: `${name}, described.`,
  target_service: 'order-service',
  expected_fix: fix,
  default_duration_seconds: 300,
  default_parameters: {},
}))

const LEAK: Experiment = {
  experiment_id: 'leak-1',
  scenario: 'MEMORY_LEAK',
  target_service: 'market-data-service',
  status: 'RUNNING',
  parameters: { rate_mb_per_minute: 80 },
  started_at: '2026-11-25T09:00:00.000Z',
  ends_at: '2026-11-25T09:10:00.000Z',
  ended_at: null,
  incident_id: null,
  time_to_detect_seconds: null,
  time_to_recover_seconds: null,
  agent_actions: [],
  timeline: [{ at: '2026-11-25T09:00:00.000Z', message: 'Started by the presenter.' }],
}

beforeEach(() => {
  live.feed = emptyFeed
  live.settings = SETTINGS
  saveAdminToken('admin-token')
  vi.spyOn(chaos, 'scenarios').mockResolvedValue(SCENARIOS)
  vi.spyOn(chaos, 'experiments').mockResolvedValue({
    items: [LEAK],
    resets: [
      { reset_at: '2026-11-25T08:55:00.000Z', undone: ['the panic sell stopped'], failed: [] },
    ],
  })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  saveAdminToken(null)
})

describe.each(BOTH)('the chaos panel (%s skin)', (theme) => {
  beforeEach(() => showTheme(theme))

  test('every scenario shows its right fix and its state', async () => {
    render(<Chaos />)
    const leak = await screen.findByTestId('MEMORY_LEAK')
    expect(within(leak).getByText('Restart market-data-service')).toBeInTheDocument()
    expect(
      within(leak).getByText('Running · 8:00 left · waiting for the agent'),
    ).toBeInTheDocument()
    expect(within(leak).getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    const decline = screen.getByTestId('PORTFOLIO_DB_DEGRADED')
    expect(within(decline).getByText('Do nothing, and escalate')).toBeInTheDocument()
    expect(within(decline).getByText('Ready')).toBeInTheDocument()
    expect(screen.getByText('● 1 experiment running')).toBeInTheDocument()
    // chaos-service's own record on the left, and merged into the agent's feed on the right
    expect(screen.getAllByText('Presenter reset')).toHaveLength(2)
    expect(screen.getByTestId('feed-reset')).toHaveTextContent('the panic sell stopped.')
  })

  test("a scenario whose service is in the agent's cooldown says it is waiting", async () => {
    render(<Chaos />)
    const panic = await screen.findByTestId('TRAFFIC_SPIKE')
    expect(within(panic).getByText('Waiting: agent cooldown 1:20')).toBeInTheDocument()
    expect(within(screen.getByTestId('PORTFOLIO_DB_DEGRADED')).getByText('Ready')).toBeVisible()
    const line = screen.getByRole('status', { name: "The agent's settings and budget" })
    expect(line).toHaveTextContent('4 of 6 actions left this hour')
    expect(line).toHaveTextContent('order-service cooldown 1:20')
  })

  test('the presenter approves or declines a waiting proposal, and turns approval off', async () => {
    live.feed = feedWithProposal()
    const decide = vi.spyOn(agent, 'decide').mockResolvedValue({} as never)
    const approval = vi.spyOn(agent, 'setApproval').mockResolvedValue(SETTINGS)
    render(<Chaos />)
    const incident = await screen.findByTestId('INC-0046')
    expect(incident).toHaveTextContent('Waiting for the presenter')
    expect(incident).toHaveTextContent('Diagnosis')
    expect(within(incident).getByRole('group')).toHaveTextContent(
      'Waiting for you: roll back order-service to revision 52',
    )
    fireEvent.click(within(incident).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(decide).toHaveBeenCalledWith('admin-token', 'INC-0046', 'APPROVE'))
    await screen.findByText('INC-0046: approved, the agent acts.')
    fireEvent.click(within(incident).getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(decide).toHaveBeenCalledWith('admin-token', 'INC-0046', 'DECLINE'))
    fireEvent.click(screen.getByRole('button', { name: 'Turn approval off' }))
    await waitFor(() => expect(approval).toHaveBeenCalledWith('admin-token', false))
    expect(live.refresh).toHaveBeenCalled()
  })

  test('an agent that does not answer is said plainly', async () => {
    live.settings = null
    render(<Chaos />)
    expect(await screen.findByText('● The agent is not answering')).toBeInTheDocument()
    expect(within(await screen.findByTestId('TRAFFIC_SPIKE')).getByText('Ready')).toBeVisible()
  })

  test('start, a refused start with its reason, stop and reset all reach chaos-service', async () => {
    const start = vi.spyOn(chaos, 'start')
    start.mockResolvedValueOnce({ ...LEAK, scenario: 'BAD_DEPLOY' })
    start.mockRejectedValueOnce(
      new ApiError(
        409,
        'EXPERIMENT_ALREADY_RUNNING',
        'Experiment x (BAD_DEPLOY) already affects order-service. Stop it first.',
      ),
    )
    const stop = vi.spyOn(chaos, 'stop').mockResolvedValue({ ...LEAK, status: 'STOPPED' })
    const reset = vi
      .spyOn(chaos, 'reset')
      .mockResolvedValue({ reset_at: NOW.toISOString(), undone: [], failed: [] })
    render(<Chaos />)

    fireEvent.click(
      within(await screen.findByTestId('BAD_DEPLOY')).getByRole('button', { name: 'Start' }),
    )
    await screen.findByText('Bad deploy started.')
    expect(start).toHaveBeenLastCalledWith('admin-token', 'BAD_DEPLOY')

    fireEvent.click(
      within(screen.getByTestId('TRAFFIC_SPIKE')).getByRole('button', { name: 'Start' }),
    )
    await screen.findByText(/already affects order-service/)

    fireEvent.click(within(screen.getByTestId('MEMORY_LEAK')).getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledWith('admin-token', 'leak-1'))

    fireEvent.click(screen.getByRole('button', { name: 'Reset everything' }))
    await screen.findByText(/every fault undone/)
    expect(reset).toHaveBeenCalledWith('admin-token')
  })
})

test('without the passcode the panel is not shown', () => {
  saveAdminToken(null)
  render(<Chaos />)
  expect(screen.queryByRole('button', { name: 'Reset everything' })).toBeNull()
  expect(screen.getByText(/The control room is for the presenter/)).toBeInTheDocument()
})
