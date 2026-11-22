# Changes

1. **Quoted memos crashed the parser.** `parse_transactions` split each line on `,`, so a
   quoted memo containing a comma produced too many fields (and `""` escapes were never
   unescaped). It now reads rows with `csv.reader`, which implements RFC 4180 quoting.
2. **Balances were a few cents off.** Amounts were converted with `float()`, so values like
   `0.10` weren't exact and sums drifted. Amounts are now `decimal.Decimal`, and totals
   start from `Decimal(0)`.
3. **The same account appeared under several keys.** `balances` keyed totals by the raw
   account string, so `Ops`, ` ops ` and `OPS` were separate accounts. Names are now
   normalized with `strip().lower()`.
4. **Mixed currencies were silently added together.** Nothing checked the currency of an
   account's transactions. `balances` now remembers each account's currency and raises
   `ValueError` naming the account when a second one appears.
5. **Month keys weren't `YYYY-MM`.** `monthly_summary` formatted the month without zero
   padding (`2026-1`). It now uses `{year:04d}-{month:02d}`, which also keeps keys in
   chronological order.
6. **`out` was negative.** Negative amounts were added to `out` as-is. `out` now sums
   their absolute values, so both totals are zero or positive.
