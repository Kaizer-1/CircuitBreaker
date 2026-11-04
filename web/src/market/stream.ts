// A WebSocket channel of the public API (api/README.md "WebSocket channels"): token in the URL
// (browsers cannot set headers on a WebSocket), answer the server's ping with a pong, and on any
// drop reconnect with exponential back-off (1 s → 30 s); the server then sends a fresh snapshot.

export type StreamState = 'connecting' | 'live' | 'reconnecting' | 'closed'

export interface StreamOptions {
  path: string // e.g. /v1/ws/market/CHAIWALA
  token: string
  onMessage: (message: { type: string; data?: unknown }) => void
  onState?: (state: StreamState) => void
  WebSocketImpl?: typeof WebSocket
  setTimer?: (fn: () => void, ms: number) => unknown
}

const MAX_BACKOFF_MS = 30_000
// Close codes after which retrying cannot help (api/README.md): unauthenticated, forbidden, unknown.
const FINAL_CODES = new Set([4401, 4403, 4404])

export function openStream(options: StreamOptions): () => void {
  const { path, token, onMessage, onState = () => {}, WebSocketImpl = WebSocket } = options
  const setTimer = options.setTimer ?? ((fn, ms) => window.setTimeout(fn, ms))
  let socket: WebSocket | null = null
  let backoff = 1000
  let stopped = false

  const url = () => {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${scheme}://${window.location.host}${path}?access_token=${encodeURIComponent(token)}`
  }

  const connect = () => {
    if (stopped) return
    socket = new WebSocketImpl(url())
    socket.onopen = () => {
      backoff = 1000
      onState('live')
    }
    socket.onmessage = (event: MessageEvent) => {
      let message: { type: string; data?: unknown }
      try {
        message = JSON.parse(String(event.data)) as { type: string; data?: unknown }
      } catch {
        return
      }
      if (message.type === 'ping') socket?.send('{"type":"pong"}')
      else onMessage(message)
    }
    socket.onclose = (event: CloseEvent) => {
      socket = null
      if (stopped) return
      if (FINAL_CODES.has(event.code)) {
        onState('closed')
        return
      }
      onState('reconnecting')
      setTimer(connect, backoff)
      backoff = Math.min(backoff * 2, MAX_BACKOFF_MS)
    }
  }

  onState('connecting')
  connect()
  return () => {
    stopped = true
    socket?.close(1000)
  }
}
