from ledger.parse import Transaction


def balances(txns: list[Transaction]) -> dict:
    totals = {}
    for t in txns:
        totals[t.account] = totals.get(t.account, 0) + t.amount
    return totals
