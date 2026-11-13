// The Q-13 client: the URL, pong for ping, frames and end handed on, controls sent, no reconnect.
import { closeReason, openReplay } from './replay'

class FakeSocket {
  static last: FakeSocket
  readyState = 1
  sent: string[] = []
  closedWith: number | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e: { code: number; reason: string }) => void) | null = null
  constructor(readonly url: string) {
    FakeSocket.last = this
  }
  send(text: string) {
    this.sent.push(text)
  }
  close(code: number) {
    this.closedWith = code
  }
  receive(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) })
  }
}

function open() {
  const messages: unknown[] = []
  const closes: number[] = []
  const control = openReplay({
    ticker: 'CALVERA',
    from: '2026-11-20T10:40:00.000Z',
    to: '2026-11-20T10:45:00.000Z',
    speed: 10,
    token: 'tok en',
    onMessage: (m) => messages.push(m),
    onClose: (code) => closes.push(code),
    WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
  })
  return { control, messages, closes, socket: FakeSocket.last }
}

describe('the replay client', () => {
  it('opens the channel with the window, the speed and the token', () => {
    const { socket } = open()
    const url = new URL(socket.url)
    expect(url.pathname).toBe('/v1/ws/replay/CALVERA')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      from: '2026-11-20T10:40:00.000Z',
      to: '2026-11-20T10:45:00.000Z',
      speed: '10',
      access_token: 'tok en',
    })
  })

  it('answers a ping, hands on frames and the end, and ignores anything else', () => {
    const { socket, messages } = open()
    socket.receive({ type: 'ping' })
    socket.receive({ type: 'frame', data: { as_of_sequence: 1 } })
    socket.receive({ type: 'end', data: { frames_sent: 1 } })
    socket.receive({ type: 'mystery' })
    socket.onmessage?.({ data: 'not json' })
    expect(socket.sent).toEqual(['{"type":"pong","data":{}}'])
    expect(messages).toEqual([
      { type: 'frame', data: { as_of_sequence: 1 } },
      { type: 'end', data: { frames_sent: 1 } },
    ])
  })

  it('sends pause, resume and speed, and closing it itself is not reported as a drop', () => {
    const { socket, control, closes } = open()
    control.pause()
    control.resume()
    control.setSpeed(60)
    expect(socket.sent.map((t) => JSON.parse(t) as unknown)).toEqual([
      { type: 'pause', data: {} },
      { type: 'resume', data: {} },
      { type: 'speed', data: { speed: 60 } },
    ])
    control.close()
    socket.onclose?.({ code: 1000, reason: '' })
    expect([socket.closedWith, closes]).toEqual([1000, []])
  })

  it('reports a close by the server once, and never reconnects', () => {
    const { socket, closes } = open()
    socket.onclose?.({ code: 1013, reason: 'busy' })
    expect(closes).toEqual([1013])
    expect(FakeSocket.last).toBe(socket)
  })

  it('says why a replay stopped in words', () => {
    expect(closeReason(1000)).toBeNull()
    expect(closeReason(1013)).toMatch(/Too many replays/)
    expect(closeReason(4422)).toMatch(/30 minutes/)
    expect(closeReason(4401)).toMatch(/expired/)
    expect(closeReason(1011)).toMatch(/did not answer/)
  })
})
