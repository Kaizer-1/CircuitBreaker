// The Q-13 replay WebSocket (api/README.md): frames paced by the server, then `end`, then a normal
// close. Unlike the live streams it never reconnects: a replay that ends or drops is over, and the
// screen shows where it stopped. The viewer can pause, resume and change speed at any moment.

export interface ReplayOptions {
  ticker: string
  from: string
  to: string
  speed: number
  token: string
  onMessage: (message: { type: 'frame' | 'end'; data: unknown }) => void
  onClose: (code: number, reason: string) => void
  WebSocketImpl?: typeof WebSocket
}

export interface ReplayControl {
  pause: () => void
  resume: () => void
  setSpeed: (speed: number) => void
  close: () => void
}

/** Why a replay closed, in words for the presenter (null when it simply ended). */
export function closeReason(code: number): string | null {
  if (code === 1000) return null
  if (code === 1013) return 'Too many replays are running right now. Try again in a moment.'
  if (code === 4422)
    return 'That replay window is not allowed (at most 30 minutes, not in the future).'
  if (code === 4401) return 'The presenter session has expired. Sign in again.'
  return 'The replay stopped: market data did not answer. Try again.'
}

export function openReplay(options: ReplayOptions): ReplayControl {
  const { ticker, from, to, speed, token, onMessage, onClose, WebSocketImpl = WebSocket } = options
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const query = new URLSearchParams({ from, to, speed: String(speed), access_token: token })
  const socket = new WebSocketImpl(
    `${scheme}://${window.location.host}/v1/ws/replay/${ticker}?${query}`,
  )
  let closedByUs = false
  const send = (type: string, data: object = {}) => {
    if (socket.readyState === 1) socket.send(JSON.stringify({ type, data }))
  }
  socket.onmessage = (event: MessageEvent) => {
    let message: { type: string; data?: unknown }
    try {
      message = JSON.parse(String(event.data)) as { type: string; data?: unknown }
    } catch {
      return
    }
    if (message.type === 'ping') send('pong')
    else if (message.type === 'frame' || message.type === 'end')
      onMessage({ type: message.type, data: message.data })
  }
  socket.onclose = (event: CloseEvent) => {
    if (!closedByUs) onClose(event.code, event.reason)
  }
  return {
    pause: () => send('pause'),
    resume: () => send('resume'),
    setSpeed: (s) => send('speed', { speed: s }),
    close: () => {
      closedByUs = true
      socket.close(1000)
    },
  }
}
