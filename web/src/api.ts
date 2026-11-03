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

export async function fetchSystemStatus(signal?: AbortSignal): Promise<SystemStatus> {
  const response = await fetch(STATUS_URL, { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error(`The gateway answered HTTP ${response.status}`)
  const body: unknown = await response.json()
  if (!isStatus(body)) throw new Error('The gateway sent a status report in an unexpected shape')
  return body
}
