// The time-travel actions against a fake API and a fake replay socket: what is asked, and when.
import { act, renderHook, waitFor } from '@testing-library/react'
import { api, type ReplayEnd, type TimeTravelView } from '../lib/market'
import type { ReplayControl, ReplayOptions } from './replay'
import { useTimeTravel } from './useTimeTravel'

const NOW = Date.parse('2026-11-20T11:00:00.000Z')
const T = NOW - 20 * 60_000
const iso = (at: number) => new Date(at).toISOString()
const HASH = 'c'.repeat(64)

function answer(at: string, verification: TimeTravelView['verification'] = null): TimeTravelView {
  return {
    ticker: 'CALVERA',
    at,
    as_of_sequence: 100,
    last_price_paise: 24500,
    order_book: {
      ticker: 'CALVERA',
      as_of_sequence: 100,
      as_of: at,
      best_bid_paise: null,
      best_ask_paise: null,
      spread_paise: null,
      bids: [],
      asks: [],
    },
    candles: [],
    recent_trades: [],
    state_hash: HASH,
    verification,
  }
}

class FakeReplay {
  options: ReplayOptions | null = null
  sent: string[] = []
  open = (options: ReplayOptions): ReplayControl => {
    this.options = options
    return {
      pause: () => this.sent.push('pause'),
      resume: () => this.sent.push('resume'),
      setSpeed: (s) => this.sent.push(`speed ${s}`),
      close: () => this.sent.push('close'),
    }
  }
}

let timeTravel: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  timeTravel = vi.spyOn(api, 'timeTravel').mockImplementation((_t, _k, at) => {
    const live = Date.parse(at) > NOW
    return Promise.resolve(
      answer(at, live ? { live_sequence: 100, live_state_hash: HASH, matches: true } : null),
    )
  })
  vi.spyOn(api, 'candlesBetween').mockResolvedValue([])
})
afterEach(() => vi.restoreAllMocks())

function setup() {
  const replay = new FakeReplay()
  const hook = renderHook(() => useTimeTravel('token', 'CALVERA', () => NOW, replay.open))
  return { replay, hook }
}

describe('useTimeTravel', () => {
  it('a seek rebuilds the market at that moment and pauses there', async () => {
    const { hook } = setup()
    act(() => hook.result.current.seek(T))
    expect(hook.result.current.state.mode).toBe('seeking')
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    expect(timeTravel).toHaveBeenCalledWith('token', 'CALVERA', iso(T))
    expect(api.candlesBetween).toHaveBeenCalledWith(
      'token',
      'CALVERA',
      '5s',
      iso(T - 10 * 60_000),
      iso(T),
      120,
    )
    expect(hook.result.current.state.view?.stateHash).toBe(HASH)
  })

  it('a rewind to now asks a little ahead, so it lands after the latest event, and proves it', async () => {
    const { hook } = setup()
    act(() => hook.result.current.seekNow())
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    expect(timeTravel).toHaveBeenCalledWith('token', 'CALVERA', iso(NOW + 1_000))
    expect(hook.result.current.state.verification?.matches).toBe(true)
  })

  it('play replays from the moment shown at the chosen speed; frames and the end arrive; the end is checked', async () => {
    const { hook, replay } = setup()
    act(() => hook.result.current.seek(T))
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    act(() => hook.result.current.setSpeed(60))
    act(() => hook.result.current.play())
    expect(hook.result.current.state.mode).toBe('playing')
    expect(replay.options).toMatchObject({ from: iso(T), to: iso(NOW - 1_000), speed: 60 })

    act(() => hook.result.current.pause())
    await waitFor(() => expect(timeTravel).toHaveBeenLastCalledWith('token', 'CALVERA', iso(T)))
    act(() => hook.result.current.play()) // resumes the same replay
    act(() => hook.result.current.setSpeed(10))
    expect(replay.sent).toEqual(['pause', 'resume', 'speed 10'])

    const frame = {
      as_of: iso(T + 1_000),
      as_of_sequence: 101,
      order_book: answer(iso(T)).order_book,
      last_trade: null,
      candle: answer(iso(T)).candles[0] ?? {
        bucket_start: iso(T),
        open_paise: 1,
        high_paise: 1,
        low_paise: 1,
        close_paise: 1,
        volume: 0,
        trade_count: 0,
      },
    }
    act(() => replay.options?.onMessage({ type: 'frame', data: frame }))
    expect(hook.result.current.state.view?.sequence).toBe(101)
    const end: ReplayEnd = {
      frames_sent: 2,
      from: iso(T),
      to: iso(T + 60_000),
      as_of_sequence: 101,
      state_hash: HASH,
    }
    act(() => replay.options?.onMessage({ type: 'end', data: end }))
    expect(hook.result.current.state.mode).toBe('ended')
    await waitFor(() => expect(hook.result.current.state.replay?.matches).toBe(true))
    expect(timeTravel).toHaveBeenLastCalledWith('token', 'CALVERA', end.to)
  })

  it('a full replica (1013) is said in words, and the screen stays where it was', async () => {
    const { hook, replay } = setup()
    act(() => hook.result.current.seek(T))
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    act(() => hook.result.current.play())
    act(() => replay.options?.onClose(1013, 'busy'))
    expect(hook.result.current.state.mode).toBe('paused')
    expect(hook.result.current.state.error).toMatch(/Too many replays/)
  })

  it('back to live closes the replay, shows the live market and proves the rebuild exact', async () => {
    const { hook, replay } = setup()
    act(() => hook.result.current.seek(T))
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    act(() => hook.result.current.play())
    act(() => hook.result.current.backToLive())
    expect(replay.sent).toContain('close')
    expect(hook.result.current.state.mode).toBe('live')
    await waitFor(() => expect(hook.result.current.state.verification?.matches).toBe(true))
  })

  it('leaving the screen ends a running replay at once', async () => {
    const { hook, replay } = setup()
    act(() => hook.result.current.seek(T))
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    act(() => hook.result.current.play())
    hook.unmount()
    expect(replay.sent).toEqual(['close'])
  })

  it('verify against live proves the rebuild without changing what is shown', async () => {
    const { hook } = setup()
    act(() => hook.result.current.seek(T))
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    act(() => hook.result.current.verify())
    await waitFor(() => expect(hook.result.current.state.verification?.matches).toBe(true))
    expect(timeTravel).toHaveBeenLastCalledWith('token', 'CALVERA', iso(NOW + 1_000))
    expect([hook.result.current.state.mode, hook.result.current.state.view?.at]).toEqual([
      'paused',
      T,
    ])
  })

  it('nothing to replay at now', async () => {
    const { hook, replay } = setup()
    act(() => hook.result.current.seek(NOW))
    await waitFor(() => expect(hook.result.current.state.mode).toBe('paused'))
    act(() => hook.result.current.play())
    expect(replay.options).toBeNull()
    expect(hook.result.current.state.error).toMatch(/Nothing to replay/)
  })
})
