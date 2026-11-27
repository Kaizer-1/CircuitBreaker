// The control room's market strip (owner, 2026-11-27): the five tickers' prices (Q-02) and the
// numbers the SRE agent sees (the feed's `vitals`, Q-19): order-service's p95 latency, error rate,
// request rate and replicas, and market-data-service's memory. So the audience sees a fault's
// impact and the agent's fix on the screen where the presenter starts it.
//
// No vitals for 15 s (Prometheus or the agent not answering): the agent's numbers grey out and say
// since when, instead of standing there as if they were current.
import { arrow, clock, direction, percent, rupees } from '../lib/format'
import { type FeedState, type Vitals, vitalsStale } from '../lib/incidents'
import type { Instrument } from '../lib/market'

// The alert thresholds (chaos scenarios §5): past them a number is shown in the "down" colour.
const P95_ALERT_SECONDS = 0.5
const ERRORS_ALERT = 0.05
const MEMORY_ALERT = 0.7

export function MarketStrip({
  instruments,
  feed,
  now,
}: {
  instruments: Instrument[]
  feed: FeedState
  now: number
}) {
  const stale = vitalsStale(feed, now)
  const v: Vitals | null = feed.vitals
  const since = feed.vitalsReceivedAt !== null ? clock(new Date(feed.vitalsReceivedAt)) : null
  return (
    <section className="market-strip" aria-label="The market and what the agent sees">
      <ul className="tickers">
        {instruments.map((i) => {
          const dir = direction(i.change_paise)
          return (
            <li key={i.ticker}>
              <b>{i.ticker}</b>
              <span className="num">{rupees(i.last_price_paise)}</span>
              <span className={`num ${dir}`}>
                {arrow(i.change_paise)}
                {percent(i.change_percent)}
              </span>
            </li>
          )
        })}
      </ul>
      <dl
        className={`vitals${stale ? ' stale' : ''}`}
        data-testid="vitals"
        aria-label={stale ? 'What the agent sees: stale' : 'What the agent sees'}
      >
        <Number
          label="Order p95"
          value={v?.p95_seconds}
          show={(s) => (s >= 1 ? `${s.toFixed(2)} s` : `${Math.round(s * 1000)} ms`)}
          alarm={(s) => s > P95_ALERT_SECONDS}
        />
        <Number
          label="Errors"
          value={v?.error_ratio}
          show={(r) => `${(r * 100).toFixed(1)}%`}
          alarm={(r) => r > ERRORS_ALERT}
        />
        <Number label="Orders/s" value={v?.requests_per_second} show={(r) => r.toFixed(0)} />
        <div>
          <dt>Replicas</dt>
          <dd className="num">
            {v?.replicas.ready ?? '–'}/{v?.replicas.desired ?? '–'}
          </dd>
        </div>
        <Number
          label="Market-data memory"
          value={v?.market_data_memory_of_limit}
          show={(m) => `${Math.round(m * 100)}%`}
          alarm={(m) => m > MEMORY_ALERT}
        />
        {stale && (
          <div className="stale-note" role="status">
            {since ? `stale since ${since}` : 'no vitals yet'}
          </div>
        )}
      </dl>
    </section>
  )
}

function Number({
  label,
  value,
  show,
  alarm,
}: {
  label: string
  value: number | null | undefined
  show: (n: number) => string
  alarm?: (n: number) => boolean
}) {
  const known = typeof value === 'number'
  return (
    <div>
      <dt>{label}</dt>
      <dd className={`num${known && alarm?.(value) ? ' down' : ''}`}>
        {known ? show(value) : '–'}
      </dd>
    </div>
  )
}
