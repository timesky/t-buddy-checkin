<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# buddy-web-checkin

**Daily activity automation for WorkBuddy** — check-in, rewards, lottery and cat trips,
in one idempotent daily ledger, running quietly in the background.

[![WorkBuddy](https://img.shields.io/badge/WorkBuddy-%E2%89%A5%205.11.0-6f6fe5)](docs/compatibility.md)
[![macOS](https://img.shields.io/badge/platform-macOS%2012%2B-lightgrey)](docs/compatibility.md)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.11-3776ab)](docs/compatibility.md)
[![Node](https://img.shields.io/badge/node-%E2%89%A5%2022-339933)](docs/compatibility.md)
[![tests](https://img.shields.io/badge/tests-159%20passing-brightgreen)](docs/architecture.md#测试)

[中文](README.md) · [Docs index (中文)](docs/README.md) · [SKILL.md](SKILL.md)

---

Everything runs over **CDP**: the tool attaches to the Growth Center page embedded in the
WorkBuddy desktop client and performs DOM-level actions. It never activates a window, never
moves your cursor and never grabs the keyboard — you can keep using your machine.

No third-party Python packages (standard library only: `tomllib`, `sqlite3`, `zoneinfo`).
On the npm side it needs `agent-browser` (required) and optionally `agent-desktop`.

> **Language note**: the WorkBuddy client UI must be **Simplified Chinese** — more than 50
> selectors are matched by Chinese labels (`成长计划`, `立即领取`, `兑换`, `抽奖(N)`, …).
> Detailed docs are currently Chinese-only; issues in English are welcome.

## What you get

- **Five tasks, one ledger**: check-in → exchange → makeup → lottery → trip. Each task ends in
  `done`, `impossible` or `pending`; the daemon only goes quiet when **all five** are terminal,
  otherwise it retries with 60 s → 120 s → 300 s → 600 s backoff.
- **Verifiable completion**: never inferred from UI copy. `done` requires server state or page
  structure (today present in `checkin_dates`, reward marked as exchanged, zero lottery tickets
  left, trip attempts exhausted).
- **No hardcoded timers**: trip duration is read back from the page's live countdown, then the
  reward is collected and the next trip dispatched at exactly that time.
- **Self-healing ledger**: on every restart each task is re-verified against real state, so a
  wrongly written `done` flips back to `pending` and is redone immediately.
- **Adaptive debug port**: follows the port the client actually reports in `DevToolsActivePort`
  instead of assuming 9222; `ports negotiate` picks a free one when it is taken.

## Requirements

| WorkBuddy client | Status |
|---|---|
| **≥ 5.11.0** (tested against 5.11.3) | ✅ Supported |
| 5.10.x | ❌ Not published, not tested |
| **≤ 5.9.x** | ❌ Unsupported: the Growth Center was still a **web activity page**; the client ships neither the embedded Growth Center nor the floating “Buddy加油站” card |

> 5.11 is currently a **staged rollout**: the public download page still serves 5.9.3, so
> installing from the website gives you the one version that cannot work. Two supported upgrade
> paths (in-app *Check for updates*, or the official CDN installers) are documented in
> **[docs/compatibility.md](docs/compatibility.md)**.

Other hard requirements: macOS 12+, Python ≥ 3.11, Node **≥ 22**, `agent-browser`, a signed-in
client whose main window stays open, and a Simplified Chinese UI. Full list, including what
breaks when a requirement is missing: [docs/compatibility.md §3](docs/compatibility.md#3-其余硬依赖).

## Quick start

```bash
git clone git@github.com:timesky/t-buddy-checkin.git
cd buddy-web-checkin
cd tools && npm install && cd ..          # agent-browser (required)
cp config.example.toml config.toml        # defaults work as-is

./run.sh install-launchd                  # daemon login item + port persistence
./run.sh ports status                     # nothing listening? run ./scripts/launch-workbuddy-debug.sh

./run.sh deps check                       # all six checks must pass
./run.sh inspect                          # expect [iframe] growth-center + check-in state
BUDDY_FREE_MODEL=1 ./run.sh once          # one real end-to-end run
```

Step-by-step expectations, auto-start and uninstall: **[docs/install.md](docs/install.md)**.
Why the debug port needs special handling: **[docs/ports.md](docs/ports.md)**.

## Commands

```bash
BUDDY_FREE_MODEL=1 ./run.sh <command>
```

| Command | Purpose | Command | Purpose |
|---|---|---|---|
| `deps check` | dependency health check | `inspect` | read-only diagnostics |
| `status` | today's ledger + backoff | `once` | run all five tasks once |
| `checkin-now` | check-in only | `daemon` | resident, silent once all done |
| `exchange-now` | redeem only | `install-launchd` | install / reinstall auto-start |
| `makeup-now` | missed-day makeup | `ports status` | port overview + ownership |
| `lottery-now` | lottery only | `ports negotiate` | pick a free port (`--apply` writes) |
| `travel-now` | dispatch one trip | `model-check` | free-model policy |

Every command except `deps`, `inspect`, `install-launchd`, `model-check` and `ports` requires
`BUDDY_FREE_MODEL=1` (an explicit free-model confirmation gate, already baked into the plist).
Full table, ledger semantics and backoff: **[docs/usage.md](docs/usage.md)**.

## Documentation

| Doc | Contents |
|---|---|
| [docs/compatibility.md](docs/compatibility.md) | Version floor, why 5.9.x cannot work, official upgrade/download links, hard requirements |
| [docs/install.md](docs/install.md) | Five-step install, expected output, auto-start, uninstall |
| [docs/usage.md](docs/usage.md) | Commands, the five tasks, daily ledger, backoff and silence rules |
| [docs/ports.md](docs/ports.md) | Port discovery order, ownership checks, switching ports |
| [docs/checkin-semantics.md](docs/checkin-semantics.md) | Where check-in lives, what counts as done, how missed days are made up |
| [docs/architecture.md](docs/architecture.md) | Data flow, Electron constraints, module map, tests |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Symptom → cause table, cross-machine checklist |
| [docs/changelog.md](docs/changelog.md) | Behaviour changes and the traps behind them |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, test conventions, commit rules |

## Safety boundaries

- **No credential handling**: no auto login, no passwords, no CAPTCHA bypass, no token reading or export.
- **No over-spending**: redemption clicks only unlocked tiers, `抽奖(N)` only when N > 0, makeup
  cards and date cells are never consumed automatically.
- **No task grinding**: “go complete” buttons are never clicked.
- **No silent fallback**: if CDP is unreachable it fails loudly with the fix, instead of faking success.
- **Tidy up**: only pages the tool opened are closed; anything you opened stays untouched.

> **Disclaimer**: this project drives your own WorkBuddy client through its local debugging
> port. It does not crack, proxy or modify server-side data. Make sure automation is allowed by
> the WorkBuddy terms and the event rules; any account or points consequences are yours.

## Development

```bash
python3 -m unittest discover -s tests -q     # 159 tests, all CDP calls mocked
BUDDY_FREE_MODEL=1 ./run.sh inspect          # read-only diagnostics on a real machine
```

Architecture, module responsibilities and test coverage: [docs/architecture.md](docs/architecture.md).
Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

## License

No license file yet — all rights reserved until one is added. If you want to reuse this code,
please open an issue describing your use case first.
