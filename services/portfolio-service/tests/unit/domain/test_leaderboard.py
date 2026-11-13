"""The leaderboard rule (Q-11): net worth marked to market, ties share a rank, then nickname."""

from portfolio_service.domain.model import Account, AccountKind, Holding
from portfolio_service.domain.rules import leaderboard

START = 10_000_000  # ₹1,00,000 in paise


def account(name: str, cash: int, *, reserved: int = 0, kind: str = "HUMAN") -> Account:
    return Account(
        trader_id=f"{name}-id",
        nickname=name,
        kind=AccountKind(kind),
        cash_paise=cash,
        reserved_cash_paise=reserved,
        starting_value_paise=START,
        created_at="2026-11-20T10:00:00.000Z",
    )


def shares(name: str, ticker: str, quantity: int, reserved: int = 0) -> Holding:
    return Holding(f"{name}-id", ticker, quantity, reserved, 24_500)


PRICES = {"CALVERA": 25_000, "KORVIN": 10_000}


def ranks(board: list[dict[str, object]]) -> list[tuple[object, object]]:
    return [(e["rank"], e["nickname"]) for e in board]


def test_traders_are_ranked_by_cash_plus_shares_at_the_last_price() -> None:
    standings = [
        (account("Asha", 9_000_000), [shares("Asha", "CALVERA", 50)]),  # 9,000,000 + 1,250,000
        (account("Bilal", 10_100_000), []),
        (account("Chitra", 8_000_000), [shares("Chitra", "KORVIN", 150)]),  # + 1,500,000
    ]
    board = leaderboard(standings, PRICES, 20)
    assert ranks(board) == [(1, "Asha"), (2, "Bilal"), (3, "Chitra")]
    assert board[0]["net_worth_paise"] == 10_250_000
    assert board[0]["pnl_percent"] == 2.5
    assert board[2]["pnl_percent"] == -5.0
    assert board[0]["kind"] == "HUMAN" and board[0]["trader_id"] == "Asha-id"


def test_reserved_cash_and_reserved_shares_still_count() -> None:
    """An open order does not make anyone poorer: the money and shares are still theirs."""
    standings = [
        (account("Dev", 10_000_000, reserved=4_000_000), [shares("Dev", "CALVERA", 10, 10)]),
        (account("Esha", 10_200_000), []),
    ]
    board = leaderboard(standings, PRICES, 20)
    assert ranks(board) == [(1, "Dev"), (2, "Esha")]
    assert board[0]["net_worth_paise"] == 10_250_000


def test_equal_net_worth_shares_a_rank_and_the_next_rank_skips() -> None:
    standings: list[tuple[Account, list[Holding]]] = [
        (account("zara", 10_000_000), []),
        (account("Meera", 10_500_000), []),
        (account("arjun", 10_000_000), []),
        (account("Kabir", 10_000_000), []),
        (account("Omar", 9_000_000), []),
    ]
    board = leaderboard(standings, PRICES, 20)
    # 1, then three tied for 2nd listed A to Z ignoring case, then 5th (not 3rd).
    assert ranks(board) == [(1, "Meera"), (2, "arjun"), (2, "Kabir"), (2, "zara"), (5, "Omar")]


def test_a_price_move_reorders_the_board() -> None:
    standings = [
        (account("Farhan", 5_000_000), [shares("Farhan", "CALVERA", 200)]),
        (account("Gita", 10_000_000), []),
    ]
    level = leaderboard(standings, PRICES, 20)  # both worth exactly ₹1,00,000: tied, A to Z
    assert ranks(level) == [(1, "Farhan"), (1, "Gita")]
    falling = PRICES | {"CALVERA": 24_000}  # Farhan's shares lose ₹2,000
    assert ranks(leaderboard(standings, falling, 20)) == [(1, "Gita"), (2, "Farhan")]
    rising = PRICES | {"CALVERA": 26_000}
    assert ranks(leaderboard(standings, rising, 20)) == [(1, "Farhan"), (2, "Gita")]


def test_the_limit_cuts_the_list_but_keeps_the_true_ranks() -> None:
    standings: list[tuple[Account, list[Holding]]] = [
        (account(f"t{n:02d}", 10_000_000 - n * 1_000), []) for n in range(30)
    ]
    standings.append((account("tie", 10_000_000 - 1_000), []))  # tied with t01
    board = leaderboard(standings, PRICES, 3)
    assert ranks(board) == [(1, "t00"), (2, "t01"), (2, "tie")]


def test_an_empty_market_has_an_empty_board() -> None:
    assert leaderboard([], PRICES, 20) == []
