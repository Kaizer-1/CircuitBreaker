// What to tell a person when an action fails, and the full error for the console. Never swallow an
// error: the order bug of November 2026 (a TypeError before the request was sent) stayed hidden
// because the page said "Try again" and logged nothing.
import { ApiError, NetworkError } from './market'

/** A short, plain reason for the toast or form; never a raw exception message. */
export function explain(e: unknown): string {
  if (e instanceof NetworkError)
    return 'No connection to the exchange. Check the Wi-Fi and try again.'
  if (e instanceof ApiError && e.status >= 500)
    return `The exchange had a problem (HTTP ${e.status}). Please try again in a moment.`
  if (e instanceof ApiError) return e.message // the server's own reason (Problem Details detail)
  return 'Something went wrong in the app. Please try again.'
}

/** Log the whole error (with what we were doing) and return the reason to show. */
export function report(action: string, e: unknown): string {
  console.error(`${action} failed:`, e)
  return explain(e)
}
