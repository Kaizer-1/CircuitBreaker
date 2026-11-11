import { useEffect, useReducer, useRef, useState } from 'react'
import {
  api,
  type Instrument,
  type LeaderboardEntry,
  type Portfolio,
  type Ticker,
} from '../lib/market'
import { applyMarket, emptyMarket, type MarketMessage, type MarketView } from './marketState'
import { openStream, type StreamState } from './stream'

type Action = { kind: 'message'; message: MarketMessage } | { kind: 'reset' }

function reducer(state: MarketView, action: Action): MarketView {
  return action.kind === 'reset' ? emptyMarket : applyMarket(state, action.message)
}

/** The live market of one ticker (Q-06). */
export function useMarket(ticker: Ticker, token: string | null): [MarketView, StreamState] {
  const [view, dispatch] = useReducer(reducer, emptyMarket)
  const [state, setState] = useState<StreamState>('connecting')
  useEffect(() => {
    if (!token) return
    dispatch({ kind: 'reset' })
    return openStream({
      path: `/v1/ws/market/${ticker}`,
      token,
      onMessage: (message) => dispatch({ kind: 'message', message: message as MarketMessage }),
      onState: setState,
    })
  }, [ticker, token])
  return [view, state]
}

/** Q-02, refreshed every few seconds (the day's figures for the watchlist and the selector). */
export function useInstruments(token: string | null, everyMs = 3000): Instrument[] {
  const [list, setList] = useState<Instrument[]>([])
  useEffect(() => {
    if (!token) return
    let stop = false
    const load = () =>
      api
        .instruments(token)
        .then((items) => !stop && setList(items))
        .catch(() => {})
    void load()
    const id = window.setInterval(load, everyMs)
    return () => {
      stop = true
      window.clearInterval(id)
    }
  }, [token, everyMs])
  return list
}

export interface PortfolioEvent {
  type: 'fill' | 'order_closed'
  data: Record<string, unknown>
}

/** The caller's own portfolio, live (Q-10), and its fills and closed orders as they happen. */
export function usePortfolio(
  token: string | null,
  onEvent: (event: PortfolioEvent) => void,
): [Portfolio | null, StreamState] {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)
  const [state, setState] = useState<StreamState>('connecting')
  const handler = useRef(onEvent) // the latest callback, without reopening the socket
  useEffect(() => {
    handler.current = onEvent
  })
  useEffect(() => {
    if (!token) return
    return openStream({
      path: '/v1/ws/portfolio',
      token,
      onMessage: (message) => {
        if (message.type === 'portfolio') setPortfolio(message.data as Portfolio)
        else if (message.type === 'fill' || message.type === 'order_closed')
          handler.current(message as PortfolioEvent)
      },
      onState: setState,
    })
  }, [token])
  return [portfolio, state]
}

/** All five tickers' live markets (the dashboard's watchlist, depth and tape). The ticker list
 * never changes, so the five hooks are always called in the same order. */
export function useMarkets(token: string | null): Record<Ticker, [MarketView, StreamState]> {
  return {
    CHAIWALA: useMarket('CHAIWALA', token),
    JUGAAD: useMarket('JUGAAD', token),
    DABBA: useMarket('DABBA', token),
    MONSOON: useMarket('MONSOON', token),
    FILMY: useMarket('FILMY', token),
  }
}

export interface Board {
  asOf: string
  entries: LeaderboardEntry[]
}

/** Q-11, the human traders ranked by net worth, polled every 2 s (the service recomputes it at
 * most that often). Not polled at all while the presenter has it hidden. */
export function useLeaderboard(
  token: string | null,
  enabled: boolean,
  everyMs = 2000,
  limit = 5,
): Board | null {
  const [board, setBoard] = useState<Board | null>(null)
  useEffect(() => {
    if (!token || !enabled) return
    let stop = false
    const load = () =>
      api
        .leaderboard(token, limit)
        .then((b) => !stop && setBoard({ asOf: b.as_of, entries: b.entries }))
        .catch(() => {})
    void load()
    const id = window.setInterval(load, everyMs)
    return () => {
      stop = true
      window.clearInterval(id)
    }
  }, [token, enabled, everyMs, limit])
  return board
}
