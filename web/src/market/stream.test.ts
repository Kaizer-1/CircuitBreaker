import { openStream, type StreamState } from './stream'

class FakeSocket {
  static all: FakeSocket[] = []
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  constructor(readonly url: string) {
    FakeSocket.all.push(this)
  }
  send(text: string) {
    this.sent.push(text)
  }
  close() {}
}

describe('a WebSocket channel', () => {
  beforeEach(() => (FakeSocket.all = []))

  it('puts the token in the URL, answers pings and passes other messages on', () => {
    const got: unknown[] = []
    openStream({
      path: '/v1/ws/market/ASTRYX',
      token: 'a b',
      onMessage: (m) => got.push(m),
      WebSocketImpl: FakeSocket as never,
    })
    const ws = FakeSocket.all[0]
    expect(ws.url).toMatch(/\/v1\/ws\/market\/ASTRYX\?access_token=a%20b$/)
    ws.onmessage!({ data: '{"type":"ping"}' })
    ws.onmessage!({ data: '{"type":"trade","data":{"sequence":3}}' })
    ws.onmessage!({ data: 'not json' })
    expect(ws.sent).toEqual(['{"type":"pong"}'])
    expect(got).toEqual([{ type: 'trade', data: { sequence: 3 } }])
  })

  it('reconnects with back-off from 1 s up to 30 s, and resets once live', () => {
    const waits: number[] = []
    const states: StreamState[] = []
    openStream({
      path: '/v1/ws/portfolio',
      token: 't',
      onMessage: () => {},
      onState: (s) => states.push(s),
      WebSocketImpl: FakeSocket as never,
      setTimer: (fn, ms) => {
        waits.push(ms)
        fn()
      },
    })
    for (let i = 0; i < 7; i++) FakeSocket.all[FakeSocket.all.length - 1].onclose!({ code: 1006 })
    expect(waits).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000])
    FakeSocket.all[FakeSocket.all.length - 1].onopen!()
    FakeSocket.all[FakeSocket.all.length - 1].onclose!({ code: 1011 })
    expect(waits.at(-1)).toBe(1000)
    expect(states).toContain('reconnecting')
    expect(states).toContain('live')
  })

  it('stops for good on 4401, 4403 and 4404', () => {
    const waits: number[] = []
    const states: StreamState[] = []
    openStream({
      path: '/v1/ws/portfolio',
      token: 't',
      onMessage: () => {},
      onState: (s) => states.push(s),
      WebSocketImpl: FakeSocket as never,
      setTimer: (_, ms) => waits.push(ms),
    })
    FakeSocket.all[0].onclose!({ code: 4401 })
    expect(waits).toEqual([])
    expect(states.at(-1)).toBe('closed')
  })
})
