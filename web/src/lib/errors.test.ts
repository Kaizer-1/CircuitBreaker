// A failed action tells the person why in plain words, and the console gets the whole error.
import { explain, report } from './errors'
import { ApiError, NetworkError } from './market'

afterEach(() => vi.restoreAllMocks())

describe('explain', () => {
  it('names the reason: no connection, the server refused, the exchange failed, the app failed', () => {
    expect(explain(new NetworkError(new TypeError('Failed to fetch')))).toBe(
      'No connection to the exchange. Check the Wi-Fi and try again.',
    )
    expect(explain(new ApiError(422, 'VALIDATION_FAILED', 'Not enough cash.'))).toBe(
      'Not enough cash.',
    )
    expect(explain(new ApiError(503, 'DEPENDENCY_UNAVAILABLE', 'portfolio down'))).toBe(
      'The exchange had a problem (HTTP 503). Please try again in a moment.',
    )
    // A bug in the page: never the raw message (a phone on the projector showing "TypeError").
    expect(explain(new TypeError('crypto.randomUUID is not a function'))).toBe(
      'Something went wrong in the app. Please try again.',
    )
  })
})

describe('report', () => {
  it('logs the whole error with what was being done', () => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const bug = new TypeError('crypto.randomUUID is not a function')
    expect(report('Placing the order', bug)).toBe(
      'Something went wrong in the app. Please try again.',
    )
    expect(log).toHaveBeenCalledWith('Placing the order failed:', bug)
  })
})
