import { splitNickname } from '../lib/nickname'

/** A nickname as typed; a suffix added because the name was taken is shown quieter. */
export function Nickname({ nickname, suffix }: { nickname: string; suffix?: string }) {
  const [typed, added] = splitNickname(nickname, suffix)
  return (
    <span className="nick" title={added ? `${typed} was taken, so ${added} was added` : undefined}>
      {typed}
      {added && <span className="suffix">{added}</span>}
    </span>
  )
}
