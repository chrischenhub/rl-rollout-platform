"""Hidden checks for pyswe/ledger-bugfix. Each test maps to a rubric criterion."""

from datetime import date
from decimal import Decimal

import pytest

from ledger import balances, monthly_summary, parse_transactions

HEAD = "date,account,amount,currency,memo\n"


def rows(*lines: str) -> str:
    return HEAD + "\n".join(lines) + "\n"


# quoted_fields
def test_quoted_memo_with_commas():
    txns = parse_transactions(rows('2026-01-03,ops,-12.00,USD,"lunch, client, downtown"', "2026-01-04,ops,5,USD,plain"))
    assert [t.memo for t in txns] == ["lunch, client, downtown", "plain"]


def test_escaped_quotes_in_memo():
    txns = parse_transactions(rows('2026-01-03,ops,-1,USD,"the ""big"" one, finally"'))
    assert txns[0].memo == 'the "big" one, finally'
    assert txns[0].amount == Decimal("-1")


# exact_decimal
def test_amounts_are_exact_decimals():
    txns = parse_transactions(rows("2026-02-01,ops,0.10,USD,a", "2026-02-01,ops,0.20,USD,b", "2026-02-02,ops,-0.30,USD,c"))
    assert all(isinstance(t.amount, Decimal) for t in txns)
    assert txns[0].amount == Decimal("0.10")
    assert balances(txns)["ops"] == Decimal("0.00")


def test_many_small_amounts_sum_exactly():
    txns = parse_transactions(rows(*[f"2026-02-{d:02d},ops,0.01,USD,x" for d in range(1, 29)] * 4))
    assert balances(txns)["ops"] == Decimal("1.12")


# account_normalization
def test_accounts_are_normalized():
    txns = parse_transactions(rows("2026-01-01,Ops,10,USD,a", "2026-01-02, ops ,5,USD,b", "2026-01-03,OPS,-3,USD,c",
                                   "2026-01-03,Payroll,-1,USD,d"))
    assert balances(txns) == {"ops": Decimal("12"), "payroll": Decimal("-1")}


# currency_mismatch
def test_mixed_currency_account_raises():
    txns = parse_transactions(rows("2026-01-01,ops,10,USD,a", "2026-01-02,OPS,5,EUR,b"))
    with pytest.raises(ValueError, match="ops"):
        balances(txns)


def test_different_currencies_in_different_accounts_are_fine():
    txns = parse_transactions(rows("2026-01-01,ops,10,USD,a", "2026-01-02,eu-ops,5,EUR,b"))
    assert balances(txns) == {"ops": Decimal("10"), "eu-ops": Decimal("5")}


# monthly_keys
def test_month_keys_are_zero_padded_and_chronological():
    txns = parse_transactions(rows("2026-10-05,ops,1,USD,a", "2025-12-31,ops,1,USD,b", "2026-01-01,ops,1,USD,c",
                                   "2026-02-15,ops,1,USD,d"))
    assert list(monthly_summary(txns)) == ["2025-12", "2026-01", "2026-02", "2026-10"]


# monthly_amounts
def test_out_is_absolute_and_in_is_positive():
    txns = parse_transactions(rows("2026-03-01,ops,100.50,USD,a", "2026-03-02,ops,-40.25,USD,b",
                                   "2026-03-03,payroll,-10,USD,c", "2026-04-01,ops,-5,USD,d"))
    summary = monthly_summary(txns)
    assert summary["2026-03"] == {"in": Decimal("100.50"), "out": Decimal("50.25")}
    assert summary["2026-04"] == {"in": Decimal("0"), "out": Decimal("5")}


# existing_behavior
def test_existing_behavior_kept():
    txns = parse_transactions(HEAD + "\n2026-03-01,ops,100,USD,seed\n\n2026-03-02,ops,-40,USD,hosting\n")
    assert [(t.date, t.memo) for t in txns] == [(date(2026, 3, 1), "seed"), (date(2026, 3, 2), "hosting")]
    with pytest.raises(ValueError):
        parse_transactions("day,acct,amt\n2026-03-01,ops,1\n")
