import { type FormEvent, useState } from 'react'
import { adminLogin } from '../api'

/** A-04: the presenter signs in with the passcode (the dashboard and System Status are admin-only). */
export function AdminLogin({
  onToken,
  purpose,
}: {
  onToken: (token: string) => void
  purpose: string
}) {
  const [nickname, setNickname] = useState('presenter')
  const [passcode, setPasscode] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    try {
      onToken(await adminLogin(nickname, passcode))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <form className="login" onSubmit={(e) => void submit(e)} aria-label="Admin sign-in">
      <p className="muted">{purpose}</p>
      <label>
        Nickname <input value={nickname} onChange={(e) => setNickname(e.target.value)} />
      </label>
      <label>
        Passcode{' '}
        <input type="password" value={passcode} onChange={(e) => setPasscode(e.target.value)} />
      </label>
      <button type="submit">Sign in</button>
      {error && (
        <p className="problem" role="alert">
          {error}
        </p>
      )}
    </form>
  )
}
