# Google Stitch Prompt - Copy/Paste into stitch.withgoogle.com

Copy everything below the line into Google Stitch:

---

Design a professional cryptocurrency trading bot dashboard with a dark theme (background #0f172a, cards #1e293b, accent blue #38bdf8, green #22c55e for profit, red #ef4444 for loss).

## Layout

### Header Bar
- Left: Logo (gradient blue-purple square with "S"), title "SOL Trading Bot", subtitle "TradingView + Claude AI + Jupiter"
- Right: Bot status indicator (green dot with "Running" text), live SOL price in accent blue monospace font, "Export CSV" button (blue)

### Stats Row (6 equal cards in a horizontal grid)
1. **Wallet Balance** - Large accent-blue number showing SOL amount, smaller gray text showing USD equivalent
2. **Total P&L** - Large green/red number with dollar amount, percentage below
3. **Today P&L** - Large green/red dollar amount
4. **Win Rate** - Large percentage, "XW / YL" count below in gray
5. **Total Trades** - Large number
6. **Last Signal** - Monospace timestamp

### Charts Row (2 equal columns)
- Left: "P&L Over Time" line chart with blue line, filled area below, dark grid
- Right: "Trade Distribution" doughnut chart with green (Buys), red (Sells), yellow (Closes), gray (Rejected)

### Bottom Row (3 columns: 2/3 + 1/3)
- Left (2/3): **Trade History** table with columns: Time, Type (green BUY / red SELL), Symbol, Action, Amount, Price, P&L (colored), TX (blue link). Scrollable, max 400px height. Sticky header row.
- Right (1/3): **Risk Settings** panel with editable number inputs:
  - Max Purchase SOL
  - Max Purchase USD
  - Max Leverage (1-20)
  - Max Position Size %
  - Risk Per Trade %
  - Low Balance Shutdown SOL
  - Daily Loss Limit %
  - Geo Risk Weight (0-1)
  - Blue "Save Settings" button at bottom

### Footer
- Centered small gray text: "TradingView SOL Bot v1.0 — Powered by Claude AI + Jupiter"

## Style Requirements
- Dark mode only, no light mode
- Card elements have subtle border (#334155) and 12px border radius
- Stats values are 1.75rem bold
- Use monospace font for prices, timestamps, and transaction hashes
- Scrollbar styled to match dark theme (thin, dark track, slate thumb)
- Subtle hover effects on table rows and buttons
- Green pulsing dot animation for bot status
- Responsive: 6-col stats on desktop, 4-col on tablet, 2-col on mobile
- Charts use Chart.js styling with dark grid lines (#1e293b)

## API Endpoints (for data binding)
- GET /api/stats → { wallet_balance_sol, wallet_balance_usd, sol_price_usd, total_trades, winning_trades, losing_trades, win_rate, total_pnl_usd, total_pnl_percent, today_pnl_usd, last_signal_time, bot_status }
- GET /api/trades?limit=50 → { trades: [{ timestamp, tx_id, signal_type, symbol, action, amount_sol, price_usd, pnl_usd }] }
- GET /api/settings → { risk: { max_purchase_sol, max_purchase_usd, max_leverage, ... }, geo_risk: { weight } }
- POST /api/settings → { max_purchase_sol, max_purchase_usd, max_leverage, ... }
- GET /api/export/csv → CSV file download

## Tech Stack
- Single HTML file
- Tailwind CSS (CDN)
- Chart.js (CDN)
- Vanilla JavaScript, no frameworks
- Use safe DOM manipulation (createElement/textContent, no innerHTML)
- Auto-refresh stats every 30s, trades every 60s
