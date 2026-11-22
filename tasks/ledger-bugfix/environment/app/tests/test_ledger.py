from datetime import date

import pytest

from ledger import balances, parse_transactions

CSV = """date,account,amount,currency,memo
2026-03-01,ops,100,USD,seed
2026-03-02,ops,-40,USD,hosting
2026-03-02,payroll,-25,USD,contractor
"""


def test_parses_rows_in_order():
    txns = parse_transactions(CSV)
    assert [t.memo for t in txns] == ["seed", "hosting", "contractor"]
    assert txns[0].date == date(2026, 3, 1)


def test_balance_per_account():
    totals = balances(parse_transactions(CSV))
    assert totals["ops"] == 60
    assert totals["payroll"] == -25


def test_rejects_unknown_header():
    with pytest.raises(ValueError):
        parse_transactions("day,acct,amt\n2026-03-01,ops,1\n")


def test_memo_with_comma():
    txns = parse_transactions('date,account,amount,currency,memo\n2026-03-05,ops,-9.99,USD,"coffee, team"\n')
    assert txns[0].memo == "coffee, team"
