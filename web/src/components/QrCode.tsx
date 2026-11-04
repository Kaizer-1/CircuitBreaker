import qrcode from 'qrcode-generator'
import { useMemo } from 'react'

/** A scannable QR code for `text`, drawn as SVG in the theme's colours (--qr-dark on --qr-light). */
export function QrCode({ text, size }: { text: string; size: number }) {
  const { count, cells } = useMemo(() => {
    const qr = qrcode(0, 'M') // the smallest version that fits, medium error correction
    qr.addData(text)
    qr.make()
    const n = qr.getModuleCount()
    const dark: [number, number][] = []
    for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (qr.isDark(r, c)) dark.push([c, r])
    return { count: n, cells: dark }
  }, [text])
  const quiet = 2 // modules of margin: scanners need a quiet zone
  const span = count + quiet * 2
  return (
    <svg
      className="qr"
      width={size}
      height={size}
      viewBox={`0 0 ${span} ${span}`}
      role="img"
      aria-label={`QR code for ${text}`}
      shapeRendering="crispEdges"
    >
      <rect width={span} height={span} fill="var(--qr-light)" />
      <path
        fill="var(--qr-dark)"
        d={cells.map(([x, y]) => `M${x + quiet} ${y + quiet}h1v1h-1z`).join('')}
      />
    </svg>
  )
}
