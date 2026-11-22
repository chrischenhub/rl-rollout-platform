Reference for the judge (the agent never sees this). The starting code has six bugs:

1. `parse.py` splits lines on "," so RFC 4180 quoted fields (commas, "" escapes) break.
2. `parse.py` converts amounts with float(), so sums are inexact; the README requires Decimal.
3. `balance.py` keys totals by the raw account string instead of strip().lower().
4. `balance.py` never checks that an account uses a single currency.
5. `report.py` formats months without zero padding ("2026-1" instead of "2026-01").
6. `report.py` adds negative amounts to "out" as-is instead of their absolute values.
