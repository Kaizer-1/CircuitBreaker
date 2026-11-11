import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BOTH, showTheme } from '../../test/themes'
import { Join } from './Join'

// The gateway refuses a nickname on its blocklist (A-01, projector safety) with 422
// VALIDATION_FAILED; the phone shows the polite reason and does not retry with a suffix.
const REFUSED = {
  type: 'https://circuitbreaker.local/errors/validation-failed',
  title: 'Please choose another nickname',
  status: 422,
  code: 'VALIDATION_FAILED',
  detail: "That nickname can't be shown on the big screen. Please choose another one.",
  errors: [{ field: 'nickname', message: 'Please choose another nickname.' }],
}

describe.each(BOTH)('joining (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the polite reason when the exchange refuses a nickname on its blocklist', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: () => Promise.resolve(REFUSED),
    })
    vi.stubGlobal('fetch', fetch)
    render(<Join />)
    fireEvent.change(screen.getByLabelText('Nickname'), { target: { value: 'B1tch_99' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start trading' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "That nickname can't be shown on the big screen. Please choose another one.",
    )
    expect(fetch).toHaveBeenCalledTimes(1) // no retry with a suffix: only a taken name gets one
  })
})
