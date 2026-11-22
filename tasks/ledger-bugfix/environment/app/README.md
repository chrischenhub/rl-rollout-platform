# ledger

Parses bank-style CSV exports and reports on them.

## Input

CSV text with this header row:

```
date,account,amount,currency,memo
```

- `date`: `YYYY-MM-DD`.
- `account`: an account name. Case and surrounding whitespace don't matter: `Ops`,
  ` ops ` and `OPS` are the same account. Results are keyed by the normalized name
  (stripped, lower-case).
- `amount`: a decimal string such as `-12.50`. Positive is money in, negative is money out.
- `currency`: a three-letter code such as `USD`.
- `memo`: free text. Follows RFC 4180: a field may be wrapped in double quotes, and a
  quoted field may contain commas and escaped quotes (`""`).

Blank lines are ignored. Any other header raises `ValueError`.

## API

- `parse_transactions(text) -> list[Transaction]`, in file order. `Transaction.amount`
  is a `decimal.Decimal` holding the amount exactly as written.
- `balances(txns) -> dict[str, Decimal]`: the sum of amounts per normalized account.
  An account with transactions in more than one currency raises `ValueError` naming
  the account.
- `monthly_summary(txns) -> dict[str, dict[str, Decimal]]`: keyed by month as
  `YYYY-MM`, in chronological order. Each value is `{"in": ..., "out": ...}`: `in` is
  the sum of positive amounts and `out` the sum of the absolute values of negative
  amounts, so both are zero or positive.
