import { isLoopback, joinUrl, joinUrlWarning, LOCALHOST_WARNING, UNSET_WARNING } from './config'

const configure = (publicBaseUrl?: string) => {
  window.CB_CONFIG = publicBaseUrl === undefined ? undefined : { publicBaseUrl }
}

describe('the join address for the QR code', () => {
  afterEach(() => configure(undefined))

  it.each([
    'http://localhost:5173',
    'http://LOCALHOST:5173',
    'http://app.localhost',
    'http://127.0.0.1:5173',
    'http://127.1.2.3',
    'http://[::1]:5173',
    'http://0.0.0.0:5173',
    'not a url',
  ])('%s only works on this computer', (url) => {
    expect(isLoopback(url)).toBe(true)
  })

  it.each(['http://192.168.43.12:5173', 'http://10.0.0.5', 'https://cb.example.org'])(
    '%s can be reached from a phone',
    (url) => expect(isLoopback(url)).toBe(false),
  )

  it('warns when PUBLIC_BASE_URL points to localhost (the Compose default)', () => {
    configure('http://localhost:5173/')
    expect(joinUrl()).toBe('http://localhost:5173/join')
    expect(joinUrlWarning()).toBe(LOCALHOST_WARNING)
  })

  it('warns when PUBLIC_BASE_URL is unset, whatever the page’s own address', () => {
    configure('  ')
    expect(joinUrl()).toBe(`${window.location.origin}/join`)
    expect(joinUrlWarning()).toBe(
      isLoopback(window.location.origin) ? LOCALHOST_WARNING : UNSET_WARNING,
    )
  })

  it('is quiet when PUBLIC_BASE_URL is the laptop’s LAN address', () => {
    configure('http://192.168.43.12:5173')
    expect(joinUrl()).toBe('http://192.168.43.12:5173/join')
    expect(joinUrlWarning()).toBeNull()
  })
})
