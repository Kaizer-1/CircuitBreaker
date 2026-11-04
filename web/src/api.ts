// The gateway's GET /v1/system/status (services/api-gateway/src/api_gateway/api/system_status.py).

export type ServiceState = 'ready' | 'not_ready' | 'unreachable'
export type DependencyState = 'ok' | 'degraded' | 'down'

export interface CheckResult {
  ok: boolean
  latency_ms?: number
  error?: string
}

export interface ServiceReport {
  name: string
  status: ServiceState
  latency_ms?: number
  checks?: Record<string, CheckResult>
  error?: string
}

export interface DependencyReport {
  name: string
  status: DependencyState
  used_by: { service: string; ok: boolean }[]
}

export interface SystemStatus {
  status: 'healthy' | 'unhealthy'
  checked_at: string
  services: ServiceReport[]
  dependencies: DependencyReport[]
}

export const STATUS_URL = '/v1/system/status'

function isStatus(value: unknown): value is SystemStatus {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return (
    (v.status === 'healthy' || v.status === 'unhealthy') &&
    typeof v.checked_at === 'string' &&
    Array.isArray(v.services) &&
    Array.isArray(v.dependencies)
  )
}

export const ADMIN_SESSION_URL = '/v1/admin/sessions'

/** The status report is admin-only (since Phase 2): a 401 or 403 means "sign in as admin". */
export class NotAuthorizedError extends Error {}

export async function fetchSystemStatus(
  token: string,
  signal?: AbortSignal,
): Promise<SystemStatus> {
  const response = await fetch(STATUS_URL, {
    signal,
    headers: { Accept: 'application/json', Authorization: `Bearer ${token}` },
  })
  if (response.status === 401 || response.status === 403) {
    throw new NotAuthorizedError('The admin session is missing or has expired')
  }
  if (!response.ok) throw new Error(`The gateway answered HTTP ${response.status}`)
  const body: unknown = await response.json()
  if (!isStatus(body)) throw new Error('The gateway sent a status report in an unexpected shape')
  return body
}

/** A-04: the presenter's passcode gives an admin token (POST /v1/admin/sessions). */
export async function adminLogin(nickname: string, passcode: string): Promise<string> {
  const response = await fetch(ADMIN_SESSION_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ nickname, passcode }),
  })
  if (response.status === 401) throw new NotAuthorizedError('Wrong passcode')
  if (!response.ok) throw new Error(`The gateway answered HTTP ${response.status}`)
  const body = (await response.json()) as { access_token?: unknown }
  if (typeof body.access_token !== 'string') throw new Error('No token in the answer')
  return body.access_token
}
