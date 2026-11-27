// The live incident feed (Q-19) for the control room: every incident newest first, each with its
// steps in order (opened, evidence, diagnosis, proposal, action, check, resolved), the presenter's
// Approve / Decline while approval mode holds a proposal (A-15), and the presenter's resets merged
// in from chaos-service (Q-22) so the room sees why things went back to normal. The agent itself
// never reads chaos-service; only this screen puts the two side by side.
//
// No icons: each step's kind is a word and a colour (the design rules).
import { clock } from '../lib/format'
import {
  asides,
  type FeedIncident,
  type FeedState,
  incidentsNewestFirst,
  lineOf,
  outcomeOf,
  splitSentence,
} from '../lib/incidents'

export interface ResetLine {
  at: string
  message: string
}

type Item = { at: string; incident: FeedIncident } | { at: string; reset: ResetLine }

const MAX_ITEMS = 8 // what fits a projector; older ones are on Q-17

export function IncidentFeed({
  feed,
  resets,
  busy,
  onDecide,
}: {
  feed: FeedState
  resets: ResetLine[]
  busy: string | null
  onDecide: (incidentId: string, decision: 'APPROVE' | 'DECLINE') => void
}) {
  const items: Item[] = [
    ...incidentsNewestFirst(feed).map((incident) => ({
      at: incident.summary.opened_at,
      incident,
    })),
    ...resets.map((reset) => ({ at: reset.at, reset })),
  ]
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
    .slice(0, MAX_ITEMS)

  if (!items.length)
    return <p className="feed-empty">No incidents yet. The agent is watching every alert.</p>

  return (
    <ol className="feed">
      {items.map((item) =>
        'reset' in item ? (
          <li key={`reset-${item.at}`} className="feed-item reset" data-testid="feed-reset">
            <header>
              <span className="num">{clock(new Date(item.at))}</span>
              <b>Presenter reset</b>
            </header>
            <p>{item.reset.message}</p>
          </li>
        ) : (
          <Incident
            key={item.incident.summary.incident_id}
            incident={item.incident}
            busy={busy}
            onDecide={onDecide}
          />
        ),
      )}
    </ol>
  )
}

function Incident({
  incident,
  busy,
  onDecide,
}: {
  incident: FeedIncident
  busy: string | null
  onDecide: (incidentId: string, decision: 'APPROVE' | 'DECLINE') => void
}) {
  const s = incident.summary
  const open = s.status !== 'RESOLVED'
  const tone = !open
    ? s.resolution === 'ESCALATED'
      ? 'warn'
      : 'good'
    : s.status === 'AWAITING_APPROVAL'
      ? 'warn'
      : 'bad'
  return (
    <li className={`feed-item ${open ? 'open' : 'closed'}`} data-testid={s.incident_id}>
      <header>
        <span className="num">{clock(new Date(s.opened_at))}</span>
        <b>{s.incident_id}</b>
        <span className="service">{s.service}</span>
        <span className="alert">{s.alert_name}</span>
        <span className={`outcome ${tone}`}>{outcomeOf(s)}</span>
      </header>
      {asides(incident.entries).map((a) => (
        <p key={`${a.kind}-${a.subject}`} className={`aside ${a.kind.toLowerCase()}`}>
          <span className="kind">{a.kind === 'SYMPTOM' ? 'Symptom' : 'After-effect'}</span>
          {a.kind === 'SYMPTOM' ? `${a.subject}: symptom of ${s.incident_id}` : a.subject} ·{' '}
          {a.state}
        </p>
      ))}
      <ol className="steps">
        {incident.entries
          .filter((e) => e.kind !== 'SYMPTOM' && e.kind !== 'AFTER_EFFECT')
          .map((e, n) => {
            const { label, tone: t } = lineOf(e)
            const [headline, reasoning] = splitSentence(e.message)
            return (
              <li key={`${e.at}-${n}`} className={`step ${t}`}>
                <span className="num">{clock(new Date(e.at))}</span>
                <span className="kind">{label}</span>
                <span className="message">
                  <b>{headline}</b>
                  {reasoning && <small className="reasoning">{reasoning}</small>}
                </span>
              </li>
            )
          })}
      </ol>
      {incident.pending && (
        <div className="decide" role="group" aria-label={`Decide on ${s.incident_id}`}>
          <span>
            Waiting for you: <b>{describe(incident.pending)}</b>
          </span>
          <button
            type="button"
            className="chaos-button"
            disabled={busy !== null}
            onClick={() => onDecide(s.incident_id, 'APPROVE')}
          >
            {busy === `${s.incident_id}:APPROVE` ? 'Approving…' : 'Approve'}
          </button>
          <button
            type="button"
            className="chaos-button stop"
            disabled={busy !== null}
            onClick={() => onDecide(s.incident_id, 'DECLINE')}
          >
            {busy === `${s.incident_id}:DECLINE` ? 'Declining…' : 'Decline'}
          </button>
        </div>
      )}
    </li>
  )
}

function describe(r: NonNullable<FeedIncident['pending']>): string {
  const target = r.target_service ?? ''
  switch (r.action) {
    case 'rollback_deployment':
      return `roll back ${target}${r.parameters.to_revision ? ` to revision ${r.parameters.to_revision}` : ''}`
    case 'scale_deployment':
      return `scale ${target} to ${r.parameters.replicas ?? '?'} replicas`
    case 'restart_deployment':
      return `restart ${target}`
    default:
      return 'do nothing'
  }
}
