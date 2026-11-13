import { fireEvent, render, screen } from '@testing-library/react'
import { BOTH, showTheme } from '../../test/themes'
import { OrderTicket } from './OrderTicket'

const setup = (over: Partial<Parameters<typeof OrderTicket>[0]> = {}) => {
  const onSubmit = vi.fn(() => Promise.resolve())
  render(
    <OrderTicket
      ticker="CALVERA"
      lastPricePaise={24_785}
      availableCashPaise={1_000_000}
      availableShares={5}
      onSubmit={onSubmit}
      {...over}
    />,
  )
  return onSubmit
}
const submit = () => fireEvent.click(screen.getByRole('button', { name: /CALVERA/ }))

describe.each(BOTH)('the order ticket (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))

  it('starts at the last price and sends a valid limit buy', () => {
    const onSubmit = setup()
    expect(screen.getByLabelText(/Limit price/)).toHaveValue('247.85')
    submit()
    expect(onSubmit).toHaveBeenCalledWith({
      ticker: 'CALVERA',
      side: 'BUY',
      order_type: 'LIMIT',
      quantity: 10,
      limit_price_paise: 24_785,
    })
  })

  it('explains a price off the 5-paise tick and sends nothing', () => {
    const onSubmit = setup()
    fireEvent.change(screen.getByLabelText(/Limit price/), { target: { value: '247.83' } })
    submit()
    expect(screen.getByRole('alert')).toHaveTextContent('Prices move in steps of ₹0.05.')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('refuses a buy beyond the cash and a sell beyond the shares', () => {
    const onSubmit = setup({ availableCashPaise: 100 })
    submit()
    expect(screen.getByRole('alert')).toHaveTextContent(/Not enough cash/)
    fireEvent.click(screen.getByRole('button', { name: '▼ SELL' }))
    fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '9' } })
    submit()
    expect(screen.getByRole('alert')).toHaveTextContent(
      'You can sell at most 5 (no short selling).',
    )
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('steps the price by a tick and the quantity by a share', () => {
    setup()
    fireEvent.click(screen.getByLabelText('Five paise higher'))
    expect(screen.getByLabelText(/Limit price/)).toHaveValue('247.90')
    fireEvent.click(screen.getByLabelText('One share more'))
    expect(screen.getByLabelText('Quantity')).toHaveValue('11')
  })

  it('a market order needs no price and shows what it sets aside', () => {
    const onSubmit = setup()
    fireEvent.click(screen.getByRole('button', { name: 'MARKET' }))
    expect(screen.getByLabelText('Price')).toBeDisabled()
    expect(screen.getByText('₹2,726.50')).toBeInTheDocument() // 10 × ₹272.65 (247.85 + 10%, up to the tick)
    submit()
    expect(onSubmit).toHaveBeenCalledWith({
      ticker: 'CALVERA',
      side: 'BUY',
      order_type: 'MARKET',
      quantity: 10,
    })
  })

  it('has the theme-only details of both skins in the markup', () => {
    setup()
    expect(screen.getByText('<GO>', { exact: false })).toHaveClass('terminal-only')
    expect(screen.getByText('Available cash').parentElement).toHaveClass('paper-only')
  })
})
