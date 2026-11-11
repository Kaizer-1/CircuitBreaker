// The leaderboard on the projector (Q-11): the human traders ranked by net worth, marked to market
// at the last traded price. Ties share a rank (1, 1, 3). Bots and load accounts are not on it.
import { arrow, direction, percent, rupees } from '../lib/format'
import type { Board } from '../market/hooks'
import { Nickname } from './Nickname'

export function Leaderboard({ board }: { board: Board | null }) {
  const entries = board?.entries ?? []
  return (
    <section className="panel leaderboard" aria-label="Leaderboard">
      <h2>
        <span>Leaderboard</span>
        <small>Humans · net worth · every few seconds</small>
      </h2>
      {entries.length > 0 ? (
        <table>
          <tbody>
            {entries.map((e) => (
              <tr key={e.trader_id}>
                <td className="num rank">{e.rank}</td>
                <td className="who">
                  <Nickname nickname={e.nickname} />
                </td>
                <td className="num">₹{rupees(e.net_worth_paise)}</td>
                <td className={`num ${direction(e.pnl_percent)}`}>
                  {arrow(e.pnl_percent)} {percent(e.pnl_percent)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="empty">No traders yet. Scan the code to join.</p>
      )}
    </section>
  )
}
