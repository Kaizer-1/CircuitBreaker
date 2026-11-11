// The historical-mode markers (time travel): no one may mistake the past for the live market. The
// banner sits under the header with the "Back to live" button; every panel's title also carries a
// tag. Paper: an archive edition, "From the archives · <date, time>". Terminal: an amber band,
// "HISTORICAL MODE · <time>". Both are text, so they also reach screen readers.
import { clock, dateline, shortDate } from '../lib/format'

export function HistoricalBanner({
  at,
  status,
  onBackToLive,
}: {
  at: number
  status: string
  onBackToLive: () => void
}) {
  const moment = new Date(at)
  return (
    <div className="historical" role="status" aria-label="Historical mode">
      <span className="paper-only kicker">From the archives</span>
      <span className="paper-only edition">
        {dateline(moment)} · <b>{clock(moment)}</b> IST
      </span>
      <span className="terminal-only edition">
        HISTORICAL MODE · <b>{clock(moment)}</b> IST · {shortDate(moment)}
      </span>
      <span className="state">{status}</span>
      <button type="button" className="live-button" onClick={onBackToLive}>
        <span className="terminal-only">
          <b>F1</b>{' '}
        </span>
        Back to live
      </button>
    </div>
  )
}

/** On every panel's title in historical mode. */
export function HistoryTag({ at }: { at: number }) {
  const time = clock(new Date(at))
  return (
    <span className="history-tag" data-testid="history-tag">
      <span className="paper-only">From the archives · {time}</span>
      <span className="terminal-only">HIST {time}</span>
    </span>
  )
}
