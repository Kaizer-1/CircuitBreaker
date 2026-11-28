// The live incident feed (Q-19) and the agent's settings (Q-20) as React hooks for the presenter's
// screens. The feed keeps its state in lib/incidents.ts; a screen that connects after an incident
// opened fills that incident's timeline in from Q-18.
import { useCallback, useEffect, useReducer, useState } from 'react'
import {
  agent,
  type AgentSettings,
  applyFeed,
  emptyFeed,
  type FeedMessage,
  type FeedState,
  type Incident,
  type IncidentSummary,
  withIncident,
} from '../lib/incidents'
import { openStream, type StreamState } from '../market/stream'

const RESOLVED_TO_FILL = 4 // the newest resolved incidents whose timelines a fresh screen reads

type Action =
  { kind: 'message'; message: FeedMessage; at: number } | { kind: 'incident'; incident: Incident }

function reducer(state: FeedState, action: Action): FeedState {
  return action.kind === 'message'
    ? applyFeed(state, action.message, action.at)
    : withIncident(state, action.incident)
}

/** Which incidents of a snapshot need their timeline read: the open ones, a few resolved. */
export function toFill(snapshot: IncidentSummary[]): string[] {
  const open = snapshot.filter((s) => s.status !== 'RESOLVED')
  const resolved = snapshot
    .filter((s) => s.status === 'RESOLVED')
    .sort((a, b) => Date.parse(b.opened_at) - Date.parse(a.opened_at))
    .slice(0, RESOLVED_TO_FILL)
  return [...open, ...resolved].map((s) => s.incident_id)
}

export function useIncidentFeed(token: string | null): [FeedState, StreamState] {
  const [state, dispatch] = useReducer(reducer, emptyFeed)
  const [stream, setStream] = useState<StreamState>('connecting')
  useEffect(() => {
    if (!token) return
    let stop = false
    const fill = (ids: string[]) =>
      ids.forEach((id) => {
        agent
          .incident(token, id)
          .then((incident) => !stop && dispatch({ kind: 'incident', incident }))
          .catch(() => {}) // the live updates still arrive; only the earlier lines are missing
      })
    const close = openStream({
      path: '/v1/ws/incidents',
      token,
      onMessage: (message) => {
        const m = message as FeedMessage
        dispatch({ kind: 'message', message: m, at: Date.now() })
        if (m.type === 'snapshot') fill(toFill(m.data))
      },
      onState: setStream,
    })
    return () => {
      stop = true
      close()
    }
  }, [token])
  return [state, stream]
}

/** Q-20 polled (the budget and cooldowns change with time); `refresh` reads it again at once. */
export function useAgentSettings(
  token: string | null,
  everyMs = 2000,
): {
  settings: AgentSettings | null
  readAt: number | null // when they were read (Date.now()), so a countdown can run between polls
  reachable: boolean
  refresh: () => void
} {
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const [readAt, setReadAt] = useState<number | null>(null)
  const [reachable, setReachable] = useState(true)
  const [asked, setAsked] = useState(0)
  useEffect(() => {
    if (!token) return
    let stop = false
    const load = () =>
      agent
        .settings(token)
        .then((s) => {
          if (!stop) {
            setSettings(s)
            setReadAt(Date.now())
            setReachable(true)
          }
        })
        .catch(() => !stop && setReachable(false))
    void load()
    const id = window.setInterval(() => void load(), everyMs)
    return () => {
      stop = true
      window.clearInterval(id)
    }
  }, [token, everyMs, asked])
  const refresh = useCallback(() => setAsked((n) => n + 1), [])
  return { settings, readAt, reachable, refresh }
}
