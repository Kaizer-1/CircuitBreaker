// The Chaos Panel in both skins: four scenarios with their right fix and state, start and stop,
// a refused start shows chaos-service's reason, and the reset reaches chaos-service.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { type Experiment, type Scenario, chaos } from '../lib/chaos'
import { ApiError } from '../lib/market'
import { saveAdminToken } from '../lib/session'
import { BOTH, showTheme } from '../../test/themes'
import { Chaos } from './Chaos'

const NOW = new Date('2026-11-25T09:02:00.000Z')
vi.mock('./presenterHooks', () => ({ useNow: () => NOW }))

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
    expect(screen.getByText('Presenter reset')).toBeInTheDocument() // on the timeline
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
  expect(screen.getByText(/The Chaos Panel is for the presenter/)).toBeInTheDocument()
})
