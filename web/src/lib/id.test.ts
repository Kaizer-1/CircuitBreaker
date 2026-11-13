// Idempotency keys must work on the phones' origin, plain http://<LAN IP>, where browsers do not
// offer crypto.randomUUID (a secure-context-only API).
import { newId } from './id'

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

afterEach(() => vi.unstubAllGlobals())

describe('newId', () => {
  it('works where crypto.randomUUID is undefined (http on a LAN address)', () => {
    const real = globalThis.crypto
    vi.stubGlobal('crypto', { getRandomValues: real.getRandomValues.bind(real) })
    expect(globalThis.crypto.randomUUID).toBeUndefined()
    expect(newId()).toMatch(V4)
  })

  it('is an RFC 4122 version 4 UUID: version nibble 4, variant bits 10', () => {
    for (const fill of [0x00, 0xff]) {
      const id = newId({
        getRandomValues: <T extends ArrayBufferView | null>(a: T) => {
          new Uint8Array((a as Uint8Array).buffer).fill(fill)
          return a
        },
      })
      expect(id).toMatch(V4)
      const bytes = id.replaceAll('-', '')
      expect(bytes[12]).toBe('4') // byte 6, high nibble
      expect(parseInt(bytes[16], 16) >> 2).toBe(0b10) // byte 8, top two bits
    }
    expect(newId({ getRandomValues: <T extends ArrayBufferView | null>(a: T) => a })).toBe(
      '00000000-0000-4000-8000-000000000000',
    )
  })

  it('does not repeat', () => {
    const ids = new Set(Array.from({ length: 10_000 }, () => newId()))
    expect(ids.size).toBe(10_000)
  })
})
