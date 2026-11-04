import {
  checkOrder,
  parseRupees,
  protectionPaise,
  stepPrice,
  type Context,
  type Draft,
} from './orderForm'

const draft = (over: Partial<Draft> = {}): Draft => ({
  ticker: 'CHAIWALA',
  side: 'BUY',
  type: 'LIMIT',
  quantity: '10',
  price: '247.80',
  ...over,
})
const ctx = (over: Partial<Context> = {}): Context => ({
  availableCashPaise: 10_000_000,
  availableShares: 0,
  lastPricePaise: 24_785,
  ...over,
})

describe('the order ticket rules', () => {
  it('turns a valid limit buy into the A-05 body and says what it sets aside', () => {
    const c = checkOrder(draft(), ctx())
    expect(c.errors).toEqual({})
    expect(c.order).toEqual({
      ticker: 'CHAIWALA',
      side: 'BUY',
      order_type: 'LIMIT',
      quantity: 10,
      limit_price_paise: 24_780,
    })
    expect(c.reservePaise).toBe(247_800)
  })

  it('refuses prices off the 5-paise tick and amounts that are not rupees', () => {
    expect(checkOrder(draft({ price: '247.83' }), ctx()).errors.price).toMatch(/₹0\.05/)
    expect(checkOrder(draft({ price: '247.805' }), ctx()).errors.price).toBeDefined()
    expect(checkOrder(draft({ price: 'abc' }), ctx()).errors.price).toBeDefined()
    expect(checkOrder(draft({ price: '0' }), ctx()).errors.price).toBeDefined()
  })

  it('wants whole shares from 1 to 10,000', () => {
    for (const q of ['0', '-1', '2.5', '', '10001'])
      expect(checkOrder(draft({ quantity: q }), ctx()).errors.quantity).toBeDefined()
    expect(checkOrder(draft({ quantity: '10000', price: '0.05' }), ctx()).order).not.toBeNull()
  })

  it('needs the cash for a buy (no margin), counting a market buy at last + 10%', () => {
    expect(
      checkOrder(draft({ quantity: '500' }), ctx({ availableCashPaise: 100_000 })).errors.funds,
    ).toMatch(/cash/)
    expect(protectionPaise(8_820)).toBe(9_705) // the spec's example
    const market = checkOrder(
      draft({ type: 'MARKET', quantity: '4' }),
      ctx({ lastPricePaise: 8_820 }),
    )
    expect(market.reservePaise).toBe(4 * 9_705)
    expect(market.order).toEqual({
      ticker: 'CHAIWALA',
      side: 'BUY',
      order_type: 'MARKET',
      quantity: 4,
    })
  })

  it('needs the shares for a sell (no short selling)', () => {
    expect(checkOrder(draft({ side: 'SELL' }), ctx({ availableShares: 0 })).errors.funds).toMatch(
      /no shares/,
    )
    expect(
      checkOrder(draft({ side: 'SELL', quantity: '12' }), ctx({ availableShares: 10 })).errors
        .funds,
    ).toMatch(/at most 10/)
    expect(
      checkOrder(draft({ side: 'SELL', quantity: '10' }), ctx({ availableShares: 10 })).order,
    ).not.toBeNull()
  })

  it('parses rupees and steps the price by one tick', () => {
    expect(parseRupees('1,186.40')).toBe(118_640)
    expect(parseRupees('₹247.8')).toBe(24_780)
    expect(stepPrice('247.80', 1)).toBe('247.85')
    expect(stepPrice('0.05', -1)).toBe('0.05')
    expect(stepPrice('247.83', 0)).toBe('247.85')
  })
})
