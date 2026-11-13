// J-01 → A-01: scan the QR code, pick a nickname, start with ₹1,00,000. No password.
import { type FormEvent, useEffect, useState } from 'react'
import { ThemeToggle } from '../components/ThemeToggle'
import { report } from '../lib/errors'
import { joinAs, NICKNAME, nicknameTaken } from '../lib/nickname'
import { navigate } from '../lib/router'
import { loadTrader, saveTrader } from '../lib/session'

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
      setError('3 to 20 letters, digits or _.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const { session: s, suffix } = await joinAs(nickname)
      saveTrader({
        token: s.access_token,
        traderId: s.trader_id,
        nickname: s.nickname,
        suffix: suffix || undefined,
        expiresAt: s.expires_at,
      })
      navigate('/trade')
    } catch (e) {
      setError(
        nicknameTaken(e)
          ? `Someone is already trading as ${nickname}. Pick another nickname.`
          : report('Joining', e),
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
