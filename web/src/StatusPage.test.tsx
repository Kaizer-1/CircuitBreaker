import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
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

const TOKEN_KEY = 'circuitbreaker.admin_token'

beforeEach(() => {
  sessionStorage.setItem(TOKEN_KEY, 'admin-token') // signed in, unless a test says otherwise
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  sessionStorage.clear()
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

  it('sends the admin token with every poll', async () => {
    mockFetch(healthy)
    render(<StatusPage intervalMs={60_000} />)
    await screen.findByText('All systems healthy')
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/v1/system/status')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer admin-token')
  })

  it('asks for the passcode when not signed in, then shows the report', async () => {
    sessionStorage.clear()
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: () => Promise.resolve({ access_token: 'new-token' }),
      })
      .mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve(healthy) })
    vi.stubGlobal('fetch', fetchMock)
    render(<StatusPage intervalMs={60_000} />)
    fireEvent.change(screen.getByLabelText(/Passcode/), { target: { value: 'the-passcode' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('All systems healthy')).toBeInTheDocument()
    const [loginUrl, loginInit] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(loginUrl).toBe('/v1/admin/sessions')
    expect(JSON.parse(loginInit.body as string)).toEqual({
      nickname: 'presenter',
      passcode: 'the-passcode',
    })
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe('new-token')
  })

  it('says so when the passcode is wrong', async () => {
    sessionStorage.clear()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: () => Promise.resolve({}) }),
    )
    render(<StatusPage intervalMs={60_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong passcode')
  })

  it('asks to sign in again when the token has expired', async () => {
    mockFetch({ code: 'UNAUTHENTICATED' }, false, 401)
    render(<StatusPage intervalMs={60_000} />)
    expect(await screen.findByRole('form', { name: 'Admin sign-in' })).toBeInTheDocument()
  })
})
