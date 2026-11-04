import { ApiError, type JoinAnswer } from './market'
import { joinAs, NICKNAME, splitNickname, withSuffix } from './nickname'

const answer = (nickname: string) => ({ nickname, access_token: 't' }) as JoinAnswer
const takenError = () => new ApiError(409, 'NICKNAME_TAKEN', 'Nickname taken')

describe('nicknames', () => {
  it('follows the spec’s rule: letters, digits and underscores only', () => {
    expect(NICKNAME.test('Raju_Trader1')).toBe(true)
    expect(NICKNAME.test('Raju-Trader')).toBe(false) // the gateway would answer 422
    expect(NICKNAME.test('ab')).toBe(false)
  })

  it('adds a short suffix that still fits the 20-character rule', () => {
    expect(withSuffix('RajuTrader', () => 0)).toBe('RajuTrader_22')
    const long = withSuffix('ABCDEFGHIJKLMNOPQRST', () => 0.99)
    expect(long).toBe('ABCDEFGHIJKLMNOPQ_zz')
    expect(NICKNAME.test(long)).toBe(true)
  })

  it('splits off only the suffix the join page added', () => {
    expect(splitNickname('RajuTrader_7k', '_7k')).toEqual(['RajuTrader', '_7k'])
    expect(splitNickname('RajuTrader', undefined)).toEqual(['RajuTrader', ''])
    expect(splitNickname('Raju_ab', '')).toEqual(['Raju_ab', '']) // typed like that: all theirs
  })

  it('joins with the name as typed when it is free', async () => {
    const join = vi.fn(async (name: string) => answer(name))
    await expect(joinAs('RajuTrader', join)).resolves.toEqual({
      session: answer('RajuTrader'),
      suffix: '',
    })
    expect(join).toHaveBeenCalledTimes(1)
  })

  it('adds a suffix only when the name is taken', async () => {
    const join = vi.fn(async (name: string) => {
      if (name === 'RajuTrader') throw takenError()
      return answer(name)
    })
    const { session, suffix } = await joinAs('RajuTrader', join, () => 0)
    expect(session.nickname).toBe('RajuTrader_22')
    expect(suffix).toBe('_22')
  })

  it('gives up after a few taken suffixes, and passes other errors straight on', async () => {
    const taken = vi.fn(async (): Promise<JoinAnswer> => {
      throw takenError()
    })
    await expect(joinAs('RajuTrader', taken)).rejects.toMatchObject({ code: 'NICKNAME_TAKEN' })
    expect(taken).toHaveBeenCalledTimes(4) // as typed + 3 suffixes
    const down = vi.fn(async (): Promise<JoinAnswer> => {
      throw new ApiError(503, 'SERVICE_UNAVAILABLE', 'down')
    })
    await expect(joinAs('RajuTrader', down)).rejects.toMatchObject({ status: 503 })
    expect(down).toHaveBeenCalledTimes(1)
  })
})
