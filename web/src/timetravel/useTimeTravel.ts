// The time-travel screen's actions: seek (Q-12 + the chart's candles from Q-03), play / pause /
// speed (Q-13), back to live, and the two proofs: a rewind to now compares the rebuilt book with
// the live engine (Q-12 `verification`), and a replay's end hash is compared with Q-12 at its end.
import { useCallback, useEffect, useReducer, useRef } from 'react'
import { ApiError, api, type ReplayEnd, type ReplayFrame, type Ticker } from '../lib/market'
import { closeReason, openReplay, type ReplayControl } from './replay'
import { initialState, reduce, replayEnd, type Speed, type TimeTravelState } from './state'

const CHART_WINDOW_MS = 10 * 60_000 // 5-second candles before the moment shown
const NOW_AHEAD_MS = 1_000 // "now" asks a little ahead; market-data counts up to 2 s ahead as now

const iso = (at: number) => new Date(at).toISOString()

function describe(e: unknown): string {
  if (e instanceof ApiError && e.code === 'OVERLOADED')
    return 'Time travel is busy right now. Try again in a moment.'
  if (e instanceof ApiError && e.code === 'TIME_IN_FUTURE')
    return 'That moment has not happened yet.'
  return e instanceof Error ? e.message : String(e)
}

export interface TimeTravel {
  state: TimeTravelState
  seek: (at: number) => void
  seekNow: () => void
  play: () => void
  pause: () => void
  setSpeed: (speed: Speed) => void
  backToLive: () => void
}

export function useTimeTravel(
  token: string,
  ticker: Ticker,
  now: () => number = Date.now,
  replayImpl: typeof openReplay = openReplay,
): TimeTravel {
  const [state, dispatch] = useReducer(reduce, initialState)
  const replay = useRef<ReplayControl | null>(null)
  const latest = useRef(state)
  useEffect(() => {
    latest.current = state
  })

  const stopReplay = useCallback(() => {
    replay.current?.close()
    replay.current = null
  }, [])

  const load = useCallback(
    async (at: number, asked: string) => {
      dispatch({ kind: 'seek', at })
      try {
        const [view, candles] = await Promise.all([
          api.timeTravel(token, ticker, asked),
          api.candlesBetween(token, ticker, '5s', iso(at - CHART_WINDOW_MS), iso(at), 120),
        ])
        dispatch({ kind: 'loaded', at, view, candles })
      } catch (e) {
        dispatch({ kind: 'error', message: describe(e) })
      }
    },
    [token, ticker],
  )

  const seek = useCallback(
    (at: number) => {
      stopReplay()
      void load(at, iso(at))
    },
    [load, stopReplay],
  )

  /** Rewind to now: the rebuilt market, proved equal to the live engine's book. */
  const seekNow = useCallback(() => {
    stopReplay()
    const at = now()
    void load(at, iso(at + NOW_AHEAD_MS))
  }, [load, now, stopReplay])

  const checkReplay = useCallback(
    (end: ReplayEnd) => {
      api
        .timeTravel(token, ticker, end.to)
        .then((v) => dispatch({ kind: 'replay-checked', matches: v.state_hash === end.state_hash }))
        .catch(() => {})
    },
    [token, ticker],
  )

  const play = useCallback(() => {
    const s = latest.current
    if (s.mode === 'paused' && replay.current) {
      replay.current.resume()
      dispatch({ kind: 'resume' })
      return
    }
    if (!s.view) return
    const from = s.view.at
    const to = replayEnd(from, now())
    if (to <= from) {
      dispatch({ kind: 'error', message: 'Nothing to replay yet: this moment is now.' })
      return
    }
    stopReplay()
    dispatch({ kind: 'play' })
    replay.current = replayImpl({
      ticker,
      from: iso(from),
      to: iso(to),
      speed: s.speed,
      token,
      onMessage: (message) => {
        if (message.type === 'frame')
          dispatch({ kind: 'frame', frame: message.data as ReplayFrame })
        else {
          const end = message.data as ReplayEnd
          dispatch({ kind: 'end', end })
          checkReplay(end)
        }
      },
      onClose: (code) => {
        replay.current = null
        const reason = closeReason(code)
        if (reason) {
          dispatch({ kind: 'pause' })
          dispatch({ kind: 'error', message: reason })
        }
      },
    })
  }, [checkReplay, now, replayImpl, stopReplay, ticker, token])

  const pause = useCallback(() => {
    replay.current?.pause()
    dispatch({ kind: 'pause' })
  }, [])

  const setSpeed = useCallback((speed: Speed) => {
    replay.current?.setSpeed(speed)
    dispatch({ kind: 'speed', speed })
  }, [])

  const backToLive = useCallback(() => {
    stopReplay()
    dispatch({ kind: 'live' })
    api
      .timeTravel(token, ticker, iso(now() + NOW_AHEAD_MS))
      .then((v) => dispatch({ kind: 'verified', verification: v.verification }))
      .catch(() => dispatch({ kind: 'verified', verification: null }))
  }, [now, stopReplay, ticker, token])

  // Another ticker: stay at the same moment (or live), and never leave a replay running.
  const shown = useRef(ticker)
  useEffect(() => {
    if (shown.current === ticker) return
    shown.current = ticker
    const s = latest.current
    stopReplay()
    if (s.mode === 'live') backToLive()
    else if (s.view) seek(s.view.at)
  }, [ticker, backToLive, seek, stopReplay])

  useEffect(() => stopReplay, [stopReplay]) // leaving the screen ends the replay at once

  return { state, seek, seekNow, play, pause, setSpeed, backToLive }
}
