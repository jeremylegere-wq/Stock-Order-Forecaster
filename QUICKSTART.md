# Quick start

## Open it
- **Windows:** double-click **Start Dashboard.bat**
- **Mac:** double-click **Start Dashboard.command** (first time: right-click, Open)
- **Linux:** run `./start_dashboard.sh`

The first run installs what it needs (a minute or two, needs internet). Python 3.11+ must be installed first
(<https://www.python.org/downloads/>; on Windows tick "Add python.exe to PATH"). Keep the window open while you work.

The folder ships with a **made-up demo shop** so you can click around straight away. See "Use your own data" in the README.

## Each quarter (about 10 minutes)
1. Export "Sales by Product" for the quarter from your POS.
2. Dashboard, **Data & sync** tab: pick the year and quarter, drop the file on the sales box.
3. Export your stock levels and drop them on the stock box (tick **Confirmed** if the figures come from a stocktake).
4. Open **Order sheet** to see what to order. **Download Excel** to share it.

Everything is saved in `data/stock.db`, and a full set of CSV files is written to your backup folder (`backup_dir` in `config.toml`) after every load.
