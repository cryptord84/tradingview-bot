# System Reference Documentation

A shareable, **sanitized** reference for the trading system — indicator logic, token coverage, position sizing, the Kalshi side, and a prioritized gap analysis.

## Files

| File | What it is |
|---|---|
| `system_reference.html` | Source of truth (edit this) |
| `system_reference.pdf` | Rendered output (what you share) |

## Sanitization

Both files are safe to share externally. They contain **no** API keys, wallet addresses, webhook URLs, TradingView slot IDs, alert IDs, account balances, usernames, or file paths. Strategy performance figures are backtest metrics only. Keep it that way — do not paste secrets or account state into the HTML.

## Regenerating the PDF

After editing `system_reference.html`:

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=docs/system_reference.pdf docs/system_reference.html
```

`--no-pdf-header-footer` suppresses Chrome's default header/footer (which would otherwise print the source file path). Any Chromium-based browser works.

## When to update

Regenerate after any roster change: new/culled alert, indicator version bump, sizing-tier change, or a Kalshi strategy state change. The content is dated in the document header.
