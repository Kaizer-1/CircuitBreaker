// Nicknames (A-01): shown exactly as the trader typed them. Only when the name is already taken
// (409 NICKNAME_TAKEN) does the join page add a short random suffix, RajuTrader → RajuTrader_7k,
// and the screens show that suffix de-emphasised.

import { ApiError, api, type JoinAnswer } from './market'

export const NICKNAME = /^[A-Za-z0-9_]{3,20}$/ // JoinRequest in api/api-gateway.yaml
const MAX_LENGTH = 20
// No 0/o, 1/l/i: the suffix is read off a phone and said out loud.
const ALPHABET = '23456789abcdefghjkmnpqrstuvwxyz'
const SUFFIX_CHARS = 2

/** `nickname` plus `_xy`, cut so the whole name still fits the 20-character rule. */
export function withSuffix(nickname: string, random: () => number = Math.random): string {
  const pick = () => ALPHABET[Math.floor(random() * ALPHABET.length)]
  const suffix = `_${Array.from({ length: SUFFIX_CHARS }, pick).join('')}`
  return nickname.slice(0, MAX_LENGTH - suffix.length) + suffix
}

/** The name as typed and the suffix the join page added (empty when it added none). */
export function splitNickname(nickname: string, suffix = ''): [string, string] {
  return suffix && nickname.endsWith(suffix)
    ? [nickname.slice(0, nickname.length - suffix.length), suffix]
    : [nickname, '']
}

const SUFFIX_TRIES = 3

export const nicknameTaken = (e: unknown) => e instanceof ApiError && e.code === 'NICKNAME_TAKEN'

/** The name as typed; only if it is taken, the same name with a short suffix (a few tries). */
export async function joinAs(
  nickname: string,
  join: (name: string) => Promise<JoinAnswer> = api.join,
  random: () => number = Math.random,
): Promise<{ session: JoinAnswer; suffix: string }> {
  try {
    return { session: await join(nickname), suffix: '' }
  } catch (e) {
    if (!nicknameTaken(e)) throw e
  }
  for (let n = 1; ; n++) {
    const name = withSuffix(nickname, random)
    try {
      return { session: await join(name), suffix: name.slice(-(SUFFIX_CHARS + 1)) }
    } catch (e) {
      if (!nicknameTaken(e) || n === SUFFIX_TRIES) throw e
    }
  }
}
