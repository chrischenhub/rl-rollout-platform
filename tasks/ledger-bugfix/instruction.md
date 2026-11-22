The `ledger` library in `/app` doesn't do what its README says. Users report that
exports with commas in the memo crash it, that balances come out a few cents off, and
that the monthly summary looks wrong.

Make the library match `/app/README.md`. Keep the public API (`Transaction`,
`parse_transactions`, `balances`, `monthly_summary`) and its signatures as they are;
use only the standard library.

When you're done, write `/app/CHANGES.md` listing each bug you fixed: what was wrong,
its root cause in the code, and how you fixed it.

`cd /app && python -m pytest` runs the existing tests; they don't cover everything.
