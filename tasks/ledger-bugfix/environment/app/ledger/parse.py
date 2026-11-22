from dataclasses import dataclass
from datetime import date

HEADER = ["date", "account", "amount", "currency", "memo"]


@dataclass(frozen=True)
class Transaction:
    date: date
    account: str
    amount: float
    currency: str
    memo: str


def parse_transactions(text: str) -> list[Transaction]:
    lines = text.strip().splitlines()
    if not lines or lines[0].split(",") != HEADER:
        raise ValueError(f"unexpected header: {lines[0] if lines else ''!r}")
    txns = []
    for line in lines[1:]:
        if not line.strip():
            continue
        day, account, amount, currency, memo = line.split(",")
        txns.append(Transaction(date.fromisoformat(day), account, float(amount), currency, memo))
    return txns
