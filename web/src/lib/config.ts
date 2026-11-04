// Runtime configuration from /config.js (written by the web container from its environment).

declare global {
  interface Window {
    CB_CONFIG?: { publicBaseUrl?: string }
  }
}

/** Where phones reach this app: PUBLIC_BASE_URL (the laptop's LAN address on demo day), never
 * guessed, because on a phone hotspot the browser's own origin may be localhost. */
export function publicBaseUrl(): string {
  const configured = window.CB_CONFIG?.publicBaseUrl?.trim()
  return (configured || window.location.origin).replace(/\/+$/, '')
}

export function joinUrl(): string {
  return `${publicBaseUrl()}/join`
}
