from decimal import Decimal

from ledger.parse import Transaction


def normalize(account: str) -> str:
    return account.strip().lower()


def balances(txns: list[Transaction]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    currency: dict[str, str] = {}
    for t in txns:
        name = normalize(t.account)
        seen = currency.setdefault(name, t.currency)
        if seen != t.currency:
            raise ValueError(f"account {name!r} mixes currencies {seen} and {t.currency}")
        totals[name] = totals.get(name, Decimal(0)) + t.amount
    return totals
