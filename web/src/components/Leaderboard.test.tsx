// The projector's leaderboard (Q-11): ranks as the service gives them (ties share one), net worth,
// and P&L with colour, arrow and sign.
import { cleanup, render, screen, within } from '@testing-library/react'
import { BOTH, showTheme } from '../../test/themes'
import { Leaderboard } from './Leaderboard'

const board = {
  asOf: '2026-11-20T10:45:00.000Z',
  entries: [
    {
      rank: 1,
      trader_id: 'a',
      nickname: 'MeeraMoon',
      kind: 'HUMAN' as const,
      net_worth_paise: 10_425_050,
      pnl_percent: 4.25,
    },
    {
      rank: 2,
      trader_id: 'b',
      nickname: 'RajuTrader',
      kind: 'HUMAN' as const,
      net_worth_paise: 10_000_000,
      pnl_percent: 0,
    },
    {
      rank: 2,
      trader_id: 'c',
      nickname: 'Asha_7k',
      kind: 'HUMAN' as const,
      net_worth_paise: 10_000_000,
      pnl_percent: 0,
    },
    {
      rank: 4,
      trader_id: 'd',
      nickname: 'Dev',
      kind: 'HUMAN' as const,
      net_worth_paise: 9_810_000,
      pnl_percent: -1.9,
    },
  ],
}

describe.each(BOTH)('the leaderboard (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))
  afterEach(cleanup)

  it('lists the traders in rank order, ties sharing a rank', () => {
    render(<Leaderboard board={board} />)
    const rows = within(screen.getByRole('region', { name: 'Leaderboard' })).getAllByRole('row')
    expect(rows.map((r) => r.textContent)).toEqual([
      '1MeeraMoon₹1,04,250.50▲ +4.25%',
      '2RajuTrader₹1,00,000.00 ±0.00%',
      '2Asha_7k₹1,00,000.00 ±0.00%',
      '4Dev₹98,100.00▼ −1.90%',
    ])
    expect(within(rows[0]).getByText(/\+4\.25%/)).toHaveClass('up')
    expect(within(rows[3]).getByText(/−1\.90%/)).toHaveClass('down')
  })

  it('says how to get on it when no one has joined yet', () => {
    render(<Leaderboard board={{ asOf: board.asOf, entries: [] }} />)
    expect(screen.getByText('No traders yet. Scan the code to join.')).toBeInTheDocument()
    render(<Leaderboard board={null} />)
    expect(screen.getAllByText(/No traders yet/)).toHaveLength(2)
  })
})
