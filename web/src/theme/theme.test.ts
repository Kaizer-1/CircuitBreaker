import html from '../../index.html?raw'
import { chooseTheme, resolveTheme, STORAGE_KEY } from './theme'

describe('which theme shows', () => {
  it('the URL wins, then the remembered choice, then the device setting', () => {
    expect(resolveTheme('?theme=paper', 'terminal', true)).toBe('paper')
    expect(resolveTheme('?theme=terminal', 'paper', false)).toBe('terminal')
    expect(resolveTheme('?theme=neon', 'terminal', false)).toBe('terminal') // unknown values are ignored
    expect(resolveTheme('', 'paper', true)).toBe('paper')
    expect(resolveTheme('', null, true)).toBe('terminal')
    expect(resolveTheme('', null, false)).toBe('paper')
  })

  it('the boot script in index.html applies the same rules before the first paint', () => {
    const boot = html.match(/<script>([\s\S]*?)<\/script>/)![1]
    for (const search of ['', '?theme=paper', '?theme=terminal', '?theme=bogus'])
      for (const stored of [null, 'paper', 'terminal', 'junk'])
        for (const dark of [false, true]) {
          const root = { dataset: {} as Record<string, string> }
          const run = new Function(
            'location',
            'localStorage',
            'window',
            'document',
            'URLSearchParams',
            boot,
          )
          run(
            { search },
            { getItem: () => stored },
            { matchMedia: () => ({ matches: dark }) },
            { documentElement: root },
            URLSearchParams,
          )
          expect(root.dataset.theme).toBe(resolveTheme(search, stored, dark))
        }
  })

  it('the toggle shows the theme and remembers it; blocked storage breaks nothing', () => {
    chooseTheme('terminal')
    expect(document.documentElement.dataset.theme).toBe('terminal')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('terminal')
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => chooseTheme('paper')).not.toThrow()
    expect(document.documentElement.dataset.theme).toBe('paper')
    spy.mockRestore()
  })
})
