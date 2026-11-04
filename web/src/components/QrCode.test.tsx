import { render, screen } from '@testing-library/react'
import { QrCode } from './QrCode'

it('draws a QR code for the join URL, in the theme colours', () => {
  render(<QrCode text="http://192.168.43.12:5173/join" size={200} />)
  const svg = screen.getByRole('img', { name: 'QR code for http://192.168.43.12:5173/join' })
  expect(svg.querySelector('rect')).toHaveAttribute('fill', 'var(--qr-light)')
  const path = svg.querySelector('path')!
  expect(path).toHaveAttribute('fill', 'var(--qr-dark)')
  expect(path.getAttribute('d')!.split('M').length).toBeGreaterThan(200) // modules drawn
})
