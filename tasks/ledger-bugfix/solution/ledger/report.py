from decimal import Decimal

from ledger.parse import Transaction


def monthly_summary(txns: list[Transaction]) -> dict[str, dict[str, Decimal]]:
    summary: dict[str, dict[str, Decimal]] = {}
    for t in sorted(txns, key=lambda t: t.date):
        key = f"{t.date.year:04d}-{t.date.month:02d}"
        bucket = summary.setdefault(key, {"in": Decimal(0), "out": Decimal(0)})
        if t.amount > 0:
            bucket["in"] += t.amount
        else:
            bucket["out"] += -t.amount
    return summary
