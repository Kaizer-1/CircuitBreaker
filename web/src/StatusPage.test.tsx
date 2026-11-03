import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SystemStatus } from './api'
import { StatusPage } from './StatusPage'

const healthy: SystemStatus = {
  status: 'healthy',
  checked_at: '2026-11-03T13:17:19Z',
  services: [
    { name: 'api-gateway', status: 'ready', checks: {} },
    {
      name: 'order-service',
      status: 'ready',
      latency_ms: 12.3,
      checks: { redpanda: { ok: true }, cassandra: { ok: true } },
    },
  ],
  dependencies: [
    { name: 'cassandra', status: 'ok', used_by: [{ service: 'order-service', ok: true }] },
    { name: 'redpanda', status: 'ok', used_by: [{ service: 'order-service', ok: true }] },
  ],
}

const unhealthy: SystemStatus = {
  ...healthy,
  status: 'unhealthy',
  services: [
    healthy.services[0],
    { name: 'order-service', status: 'unreachable', error: 'ConnectError' },
  ],
}

function mockFetch(body: unknown, ok = true, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok, status, json: () => Promise.resolve(body) }),
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('StatusPage', () => {
  it('shows every service and dependency as healthy', async () => {
    mockFetch(healthy)
    render(<StatusPage intervalMs={60_000} />)
    expect(await screen.findByText('All systems healthy')).toBeInTheDocument()
    expect(screen.getByTestId('service-order-service')).toHaveTextContent('Healthy')
    expect(screen.getByTestId('dependency-cassandra')).toHaveTextContent('Healthy')
    expect(screen.getByTestId('dependency-redpanda')).toHaveTextContent('Healthy')
  })

  it('turns a service red when it is unreachable', async () => {
    mockFetch(unhealthy)
    render(<StatusPage intervalMs={60_000} />)
    expect(await screen.findByText('1 of 2 services need attention')).toBeInTheDocument()
    expect(screen.getByTestId('service-order-service')).toHaveTextContent('Unreachable')
    expect(screen.getByTestId('service-api-gateway')).toHaveTextContent('Healthy')
  })

  it('says so when the gateway itself cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    render(<StatusPage intervalMs={60_000} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach the API gateway')
  })

  it('rejects a response in an unexpected shape', async () => {
    mockFetch({ hello: 'world' })
    render(<StatusPage intervalMs={60_000} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('unexpected shape')
  })
})
