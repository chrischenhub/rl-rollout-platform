from ledger.parse import Transaction


def monthly_summary(txns: list[Transaction]) -> dict:
    summary = {}
    for t in sorted(txns, key=lambda t: t.date):
        key = f"{t.date.year}-{t.date.month}"
        bucket = summary.setdefault(key, {"in": 0, "out": 0})
        if t.amount > 0:
            bucket["in"] += t.amount
        else:
            bucket["out"] += t.amount
    return summary
