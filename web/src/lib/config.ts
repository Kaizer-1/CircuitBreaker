// Runtime configuration from /config.js (written by the web container from its environment).

declare global {
  interface Window {
    CB_CONFIG?: { publicBaseUrl?: string }
  }
}

export const LOCALHOST_WARNING = "QR points to localhost. Phones can't join. Set PUBLIC_BASE_URL."
export const UNSET_WARNING =
  "PUBLIC_BASE_URL is not set: the QR uses this page's own address. Set PUBLIC_BASE_URL."

function configured(): string {
  return window.CB_CONFIG?.publicBaseUrl?.trim() ?? ''
}

/** Where phones reach this app: PUBLIC_BASE_URL (the laptop's LAN address on demo day), never
 * guessed, because on a phone hotspot the browser's own origin may be localhost. */
export function publicBaseUrl(): string {
  return (configured() || window.location.origin).replace(/\/+$/, '')
}

export function joinUrl(): string {
  return `${publicBaseUrl()}/join`
}

/** True when the address only works on this computer (a phone that scans it reaches itself). */
export function isLoopback(url: string): boolean {
  let host: string
  try {
    host = new URL(url).hostname
  } catch {
    return true // not a usable address at all
  }
  return (
    host === 'localhost' ||
    host.endsWith('.localhost') ||
    /^127\./.test(host) ||
    host === '[::1]' ||
    host === '0.0.0.0'
  )
}

/** The presenter's warning when the join QR code cannot work for phones, else null. */
export function joinUrlWarning(): string | null {
  if (isLoopback(publicBaseUrl())) return LOCALHOST_WARNING
  if (!configured()) return UNSET_WARNING
  return null
}
