// J-01 → A-01: scan the QR code, pick a nickname, start with ₹1,00,000. No password.
import { type FormEvent, useEffect, useState } from 'react'
import { ThemeToggle } from '../components/ThemeToggle'
import { ApiError, api } from '../lib/market'
import { navigate } from '../lib/router'
import { loadTrader, saveTrader } from '../lib/session'

const NICKNAME = /^[A-Za-z0-9_-]{3,20}$/

export function Join() {
  const [nickname, setNickname] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (loadTrader()) navigate('/trade') // already joined on this phone
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!NICKNAME.test(nickname)) {
      setError('3 to 20 letters, digits, - or _.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const s = await api.join(nickname)
      saveTrader({
        token: s.access_token,
        traderId: s.trader_id,
        nickname: s.nickname,
        expiresAt: s.expires_at,
      })
      navigate('/trade')
    } catch (e) {
      setError(
        e instanceof ApiError && e.code === 'NICKNAME_TAKEN'
          ? `Someone is already trading as ${nickname}. Pick another nickname.`
          : e instanceof Error
            ? e.message
            : String(e),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="gate">
      <div className="phone-head">
        <h1 className="masthead paper-only">
          CircuitBreaker <em>Markets</em>
        </h1>
        <div className="command terminal-only">
          <span className="badge">CB</span>
          <span>
            &gt; <span className="typed">JOIN</span>
            <span className="cursor" aria-hidden="true" />
          </span>
        </div>
        <ThemeToggle />
      </div>
      <form className="login" onSubmit={(e) => void submit(e)} aria-label="Join the market">
        <p className="muted">
          You get ₹1,00,000 of play money to trade five made-up stocks against your classmates and
          20 bots.
        </p>
        <label>
          Nickname
          <input
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
            autoComplete="nickname"
            autoCapitalize="off"
            maxLength={20}
            placeholder="RajuTrader"
            aria-invalid={error !== null}
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? 'Joining…' : 'Start trading'}
        </button>
        {error && (
          <p className="problem" role="alert">
            {error}
          </p>
        )}
      </form>
    </main>
  )
}
