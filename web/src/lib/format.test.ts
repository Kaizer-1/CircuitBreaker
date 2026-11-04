import { arrow, dateline, inr, percent, quantity, rupees, signedRupees } from './format'

describe('money and numbers', () => {
  it('groups rupees the Indian way, from paise', () => {
    expect(inr(10_000_000)).toBe('₹1,00,000.00')
    expect(inr(10_007_300)).toBe('₹1,00,073.00')
    expect(inr(1_000_000_000)).toBe('₹1,00,00,000.00')
    expect(rupees(24_785)).toBe('247.85')
    expect(rupees(118_640)).toBe('1,186.40')
    expect(rupees(5)).toBe('0.05')
  })

  it('uses a real minus sign and always shows the sign of a change', () => {
    expect(inr(-7_000)).toBe('−₹70.00')
    expect(signedRupees(235)).toBe('+2.35')
    expect(signedRupees(-190)).toBe('−1.90')
    expect(signedRupees(0)).toBe('±0.00')
    expect(percent(0.957)).toBe('+0.96%')
    expect(percent(-0.36)).toBe('−0.36%')
  })

  it('pairs every change with an arrow, so colour is never the only cue', () => {
    expect([arrow(1), arrow(-1), arrow(0)]).toEqual(['▲', '▼', ''])
  })

  it('formats quantities and the dateline', () => {
    expect(quantity(53_623)).toBe('53,623')
    expect(quantity(100_000)).toBe('1,00,000')
    expect(dateline(new Date('2026-11-20T10:00:00Z'))).toBe('Friday 20 November 2026')
  })
})
