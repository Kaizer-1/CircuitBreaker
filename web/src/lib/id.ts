// Idempotency keys for A-05 (api/order-service.yaml: a client-generated unique key).
//
// Not crypto.randomUUID(): browsers offer it only in a secure context (https or localhost), and phones
// reach the laptop over plain http://<LAN IP>, where it is undefined. crypto.getRandomValues works on
// every origin, so we build an RFC 4122 version 4 UUID from it, and use this one path everywhere
// (localhost too), so the tests run the code the phones run.

type RandomSource = Pick<Crypto, 'getRandomValues'>

/** A random RFC 4122 v4 UUID, e.g. "3b241101-e2bb-4255-8caf-4136c566a962". */
export function newId(random: RandomSource = globalThis.crypto): string {
  const bytes = random.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4: the high nibble of byte 6 is 0100
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // variant RFC 4122: the top two bits of byte 8 are 10
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
