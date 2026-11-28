// The control room's incident feed: a symptom hangs under its root incident as one plain line,
// "GatewayHighErrorRate on api-gateway: symptom of INC-0005, no action" (owner, 2026-11-28: no
// "Symptom … symptom of" twice), and an after-effect keeps its word.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import { applyFeed, emptyFeed, type IncidentSummary } from '../lib/incidents'
import { IncidentFeed } from './IncidentFeed'

afterEach(cleanup)

const ROOT: IncidentSummary = {
  incident_id: 'INC-0005',
  alert_name: 'OrderServiceHighErrorRate',
  service: 'order-service',
  severity: 'critical',
  status: 'OPEN',
  resolution: null,
  summary: 'order-service answers 5xx.',
  opened_at: '2026-11-28T10:00:00.000Z',
  resolved_at: null,
  time_to_recover_seconds: null,
  actions_taken: [],
  fired_at: '2026-11-28T09:59:58.000Z',
  mitigated_at: null,
  recovered_at: null,
}

test('a symptom is one plain line under its root, an after-effect says it is one', () => {
  let feed = applyFeed(emptyFeed, { type: 'incident_opened', data: ROOT }, 0)
  for (const entry of [
    {
      at: '2026-11-28T10:00:20.000Z',
      kind: 'SYMPTOM' as const,
      message:
        "GatewayHighErrorRate on api-gateway: a symptom of INC-0005. 100% of the gateway's errors are order-service's answers, passed on; no action.",
    },
    {
      at: '2026-11-28T10:01:20.000Z',
      kind: 'AFTER_EFFECT' as const,
      message: 'ConsumerLagHigh (3000 now) after the scale: an expected after-effect, draining.',
    },
  ])
    feed = applyFeed(feed, { type: 'incident_update', data: { incident_id: 'INC-0005', entry } }, 0)
  const { container } = render(
    <IncidentFeed feed={feed} resets={[]} busy={null} onDecide={() => undefined} />,
  )
  const lines = [...container.querySelectorAll('.aside')].map((p) => p.textContent)
  expect(lines).toEqual([
    'GatewayHighErrorRate on api-gateway: symptom of INC-0005, no action',
    'After-effectConsumerLagHigh, draining, no action',
  ])
  expect(screen.queryByText(/Symptom/)).toBeNull() // the word once, in the sentence itself
})
