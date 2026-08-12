# Emporia Energy Report

Pulls per-circuit energy data from an Emporia Vue account with
[pyemvue](https://github.com/magico13/PyEmVue) and prints it as a table. For every
channel on every device it reports:

| Column | Meaning |
| --- | --- |
| `Now (W)` | current power draw, averaged over the most recent minute |
| `Today (kWh)` | local midnight to now |
| `Last 7d (kWh)` | rolling 7-day window, ending with today's partial day |
| `Last 30d (kWh)` | rolling 30-day window, ending with today's partial day |
| `MTD (kWh)` | local 1st of the month to now |

With `--sendemail` the same table plus charts is mailed through Gmail SMTP.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env
# then edit .env with your Emporia credentials
```

macOS / Linux: `python3 -m venv .venv && source .venv/bin/activate`, `cp .env.example .env`.

## Usage

```powershell
# print the table
python Emporia_Energy.py

# also write chart PNGs to .\reports
python Emporia_Energy.py --charts

# print the table and email it with the charts inline
python Emporia_Energy.py --sendemail

# machine-readable output, including the daily series
python Emporia_Energy.py --json > usage.json
```

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--sendemail` | off | additionally email the report and charts via Gmail SMTP |
| `--charts` | off | render chart PNGs without emailing |
| `--generate-circuit-map` | — | write a combine map for this account and exit |
| `--circuit-map PATH` | `CIRCUIT_MAP_FILE` | combine map to apply |
| `--no-combine` | off | report every channel separately, ignoring the map |
| `--force` | off | let `--generate-circuit-map` overwrite an existing map |
| `--days N` | `HISTORY_DAYS` (30) | days of daily history to pull |
| `--top N` | `CHART_TOP_N` (15) | circuits per ranked bar chart, highest first |
| `--top-lines N` | `CHART_LINE_TOP_N` (5) | circuits in the daily line chart, max 8 |
| `--output-dir DIR` | `OUTPUT_DIR` (`reports`) | where PNGs and CSVs land |
| `--csv PATH` | off | also write the summary table to CSV |
| `--json` | off | print JSON to stdout instead of the table |
| `--env-file PATH` | `.env` | use a different env file |
| `--quiet` | off | suppress progress messages on stderr |

Progress and warnings go to stderr, so `--json` and the table can be piped
cleanly.

## Configuration

Everything lives in `.env` — see `.env.example` for the annotated list. The
essentials:

- `EMPORIA_USERNAME` / `EMPORIA_PASSWORD` — your Emporia app login.
- `EMPORIA_TOKEN_FILE` — auth tokens are cached here so later runs skip the
  password round-trip. Delete it after a password change.
- `TIMEZONE` — IANA name (e.g. `America/Chicago`) used for day and month
  boundaries. Leave blank to use the machine's local time. Set it to whatever
  timezone your Emporia account uses, otherwise "today" can be off by a day.
  Windows ships no IANA timezone database, so this needs the `tzdata` package
  from `requirements.txt`; without it the script warns and falls back to local
  time.
- `SMTP_USER` / `SMTP_APP_PASSWORD` / `EMAIL_TO` — required for `--sendemail`.

### Gmail app password

Gmail rejects your normal account password over SMTP. Create an app password
instead: **Google Account → Security → 2-Step Verification → App passwords**,
then put the 16-character value in `SMTP_APP_PASSWORD`. Port `587` uses
STARTTLS; set `SMTP_PORT=465` to use implicit SSL instead.

## Combining circuits

A 240 V circuit (compressor, water heater, dryer, well pump) is monitored with
one sensor per leg, so Emporia reports it as two channels — usually named
`Foo 1` / `Foo 2`, sometimes both just `Foo`. `circuit_map.json` sums each pair
back into a single reported circuit.

Generate one from your own panel:

```powershell
python Emporia_Energy.py --generate-circuit-map
```

`circuit_map.example.json` is a committed template showing every member form;
copy it to `circuit_map.json` if you would rather write the map by hand. The
generated `circuit_map.json` is gitignored, since it holds your own device gids
and circuit names.

That groups any channels whose names match apart from a trailing `1` or `2`
(plus any that share a name outright), writes the file, and exits — it makes only
two API calls, so it is quick. It refuses to clobber an existing map without
`--force`. The result is a plain map of combined name to member channels:

```json
{
  "_Up Comp": "Up Comp 1 + Up Comp 2",
  "Up Comp": ["18130:5", "18130:6"],
  "Water Heater": ["18130:11", "18130:12"]
}
```

- Members are `"<device_gid>:<channel>"`. A bare channel number (`"5"`) or a
  channel name (`"Water Heater"`, which matches **both** same-named legs) also
  works.
- Keys starting with `//` or `_` are comments — the generator uses them to record
  which names were merged.
- Edit freely: delete an entry to split that circuit back out, rename a key to
  change the reported label, or hand-write groups of three or more channels.
- An entry matching fewer than two channels is reported as a warning and left
  uncombined, so a typo degrades rather than silently dropping data.

Combining is arithmetic on the same channels, so the reported totals do not
change — `Sum of circuits + balance` still reconciles with the mains total. Run
with `--no-combine` to see the raw channels for a single run.

## Charts

`--charts` and `--sendemail` write PNGs (timestamped) into `OUTPUT_DIR`:

1. **Current power by circuit** — ranked horizontal bars, watts.
2. **kWh today by circuit** — ranked horizontal bars.
3. **kWh last 30 days by circuit** — ranked horizontal bars.
4. **Whole-home kWh per day** — one column per day over the history window;
   today's still-accumulating column is drawn in a lighter shade.
5. **Daily kWh by circuit** — one line per circuit over the same window, for the
   top `--top-lines` consumers (default 5). Each line runs solid through the last
   complete day and dashed into today's partial one.

The first four charts each show a single series in one hue, sorted high to low,
with values labelled directly on the bars — no legend, no second axis. Circuits
that would round to zero for a window are dropped from that chart (the title
reports how many bars were actually drawn), and each is limited to the top
`--top` circuits.

The line chart is the one multi-series chart, so it carries a legend, and hues
are assigned from a fixed order that has been validated for colorblind
separation against the chart surface. That order holds only up to **8 series**,
which is a hard cap: ask for more and it plots 8 and warns rather than inventing
extra hues that nobody could tell apart — the bar charts still cover every
circuit. At four series or fewer each line is also labelled at its right end,
with a colored end-dot carrying the identity and the label text in ink (a light
hue like yellow is unreadable as text).

In email the PNGs are embedded inline (`cid:` references) with a plain-text
fallback for clients that block images, and the summary CSV is attached unless
`EMAIL_ATTACH_CSV=false`.

## How the numbers are derived

- **Current watts** — one `get_device_list_usage` call at `1MIN` scale; the
  kWh returned for that minute is multiplied by 60,000. Set
  `INSTANT_SCALE=second` for a closer-to-instantaneous reading (multiplied by
  3,600,000), at the cost of more empty values on individual circuits.
- **Daily kWh** — one `get_device_list_usage` call per day at `1D` scale,
  covering every channel at once. Each request targets local noon of that day
  so it lands inside the right daily bucket regardless of UTC offset; today's
  request uses the current instant. The buckets are Emporia's own daily
  aggregates, so the figures match the app.
- **Windows** — `Today` is the current day's bucket; `Last 7d` / `Last 30d` sum
  the trailing 7 and 30 buckets **including today's partial day**; `MTD` sums
  every bucket in the current calendar month. The history window is
  automatically extended past `HISTORY_DAYS` when needed so month-to-date is
  always complete (e.g. on the 31st).
- `Sum of circuits + balance` adds up every non-total row. Because Emporia's
  `Balance` channel *is* the unmetered remainder (mains minus the monitored
  circuits), that sum reconciles with the mains total exactly — a useful sanity
  check that nothing was double-counted or dropped. On a device without a
  `Balance` channel the row is labelled `Sum of circuits` and falls short of the
  total by whatever is unmonitored.
- A 240 V circuit is monitored with one sensor per leg, so it appears as two
  channels. `circuit_map.json` sums those pairs back into one row — see
  [Combining circuits](#combining-circuits). Without a map both legs are listed
  separately, with the channel number appended (`Water Heater (ch 11)` /
  `(ch 12)`) so the rows stay distinguishable.

A run makes roughly `days + devices + 3` API calls (≈35 for a single Vue over 30
days) and takes about 15–30 seconds. `REQUEST_DELAY_SECONDS` paces them; raise it
if the Emporia cloud starts returning errors. Transient failures are retried with
backoff, and a day that still fails is reported as a warning and skipped rather
than aborting the run.

`--` in any cell means the API returned no value for that channel and window.

## Notes and limitations

- The Emporia cloud API is unofficial and undocumented; pyemvue can break when
  Emporia changes it.
- Accounts with two-factor authentication are not supported by pyemvue's login
  flow — this needs an account where email + password alone authenticate.
- Data is fetched from Emporia's cloud, not the device, so the account must be
  online and the numbers are only as fresh as the device's last upload.
- The daily aggregates come back on the account's own timezone boundaries. If
  `TIMEZONE` disagrees with the account setting, day boundaries can shift by a
  day.

## Files

| File | Purpose |
| --- | --- |
| `Emporia_Energy.py` | the report script |
| `.env.example` | template for `.env` — copy and fill in |
| `circuit_map.example.json` | template for the combine map (committed) |
| `circuit_map.json` | your panel's combine map (generated, gitignored) |
| `requirements.txt` | pinned dependencies |
| `reports/` | generated charts and CSVs (created on demand) |
| `emporia_tokens.json` | cached auth tokens (created on demand) |

`.env`, `emporia_tokens.json`, `circuit_map.json`, `reports/`, and any exported
CSVs contain credentials or personal usage data. The included `.gitignore`
covers all of them — check `git status` before your first commit.
