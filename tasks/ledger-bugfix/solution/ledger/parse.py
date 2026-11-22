import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

HEADER = ["date", "account", "amount", "currency", "memo"]


@dataclass(frozen=True)
class Transaction:
    date: date
    account: str
    amount: Decimal
    currency: str
    memo: str


def parse_transactions(text: str) -> list[Transaction]:
    rows = [row for row in csv.reader(io.StringIO(text.strip())) if row and any(f.strip() for f in row)]
    if not rows or rows[0] != HEADER:
        raise ValueError(f"unexpected header: {','.join(rows[0]) if rows else ''!r}")
    txns = []
    for row in rows[1:]:
        if len(row) != len(HEADER):
            raise ValueError(f"expected {len(HEADER)} fields, got {len(row)}: {row!r}")
        day, account, amount, currency, memo = row
        try:
            value = Decimal(amount.strip())
        except InvalidOperation:
            raise ValueError(f"bad amount {amount!r}") from None
        txns.append(Transaction(date.fromisoformat(day.strip()), account, value, currency.strip(), memo))
    return txns
