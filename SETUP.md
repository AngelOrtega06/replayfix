# Setup

## Windows: extract the complete project

Save `ReplayFix_GitHub_Ready.zip` in Downloads. In PowerShell:

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\ReplayFix_GitHub_Ready.zip" -DestinationPath "$env:USERPROFILE\Downloads\ReplayFix_GitHub"
Set-Location "$env:USERPROFILE\Downloads\ReplayFix_GitHub"
py .\replayfix.py --version
```

The version should be `ReplayFix controller 0.3.0`. The ZIP has the repository
files directly at its root. If the destination already exists, choose a fresh
folder and use that same folder in `Set-Location`.

## Inspect the saved result without inference

Requires Python 3.10 or later. No pip packages are needed.

```powershell
py .\replayfix_dashboard.py --runs-dir .\examples
```

The browser opens http://127.0.0.1:8765. The included run shows a baseline of
1/4, a first repair of 3/4, and a second repair of 4/4. Viewing this saved result
needs no Docker or API key. Keep PowerShell open; use Ctrl+C to stop the server.

## Run a new repair

Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)
and start its Linux-container engine. Python runs locally; Nemotron inference
runs on Nebius Token Factory. A local NVIDIA GPU is not needed.

```powershell
docker pull python:3.12-slim
py .\replayfix.py --check
```

The check must run four tests with three expected failures and print
`Setup ready`. It reproduces the original bug and makes no API request.

Then start the interactive dashboard:

```powershell
py .\replayfix_dashboard.py --enable-run
```

Enter your Nebius Token Factory key at the hidden terminal prompt. An existing
`NEBIUS_API_KEY` environment variable is also supported. The application does
not load a `.env` file and does not save the key. Click **Run repair** to start.

New runs go into `runs/`. The bundled sample in `examples/` stays separate.
The **Stop** button waits for the current bounded operation to finish, then
prevents further operations. API/network errors stop the run without an
automatic retry. A timed-out request can still consume provider credits.

Each run makes up to three inference requests, capped at 4,096 output tokens
per request. Input tokens also count. Use `--attempts 1` for a one-request limit.
The default model is `nvidia/Nemotron-3_5-Lightning`. For this ID, controller
0.3.0 requests non-thinking output and temperature 0.2. The prompt and
generation settings are in `replayfix.py`; no Playground prompt setup is needed.

## Other commands

| Purpose | PowerShell command |
| --- | --- |
| Run the CLI repair loop | `py .\replayfix.py` |
| Limit a dashboard run to one model request | `py .\replayfix_dashboard.py --enable-run --attempts 1` |
| View your local run history | `py .\replayfix_dashboard.py` |
| Use another port | `py .\replayfix_dashboard.py --port 8766` |
| Avoid opening the browser automatically | `py .\replayfix_dashboard.py --no-open` |
| Run 13 offline controller checks | `py -m unittest discover -s tests -v` |

Both the CLI and dashboard support `--model` with an exact provider model ID.
Availability and model-specific settings depend on the service. On macOS or
Linux, substitute `python3` for `py` and use ordinary platform paths.

## Outputs and verification

Each timestamped run contains `report.json` and the final text returned for
each attempted model request. Only a runner result with all four required
tests passing produces `verified_handler.py` and `repair.diff`. Reports retain
failed and rejected attempts, test output, usage, and generation settings.

Generated functions are parsed on the host and executed only in Docker. The
container has no network, runs as an unprivileged user, uses a read-only root
filesystem and mount, and has CPU, memory, process, and execution-time limits.
The API key is excluded from the Docker subprocess environment and browser.

## Troubleshooting

| Problem | Action |
| --- | --- |
| Port 8765 is busy | Stop the earlier dashboard with Ctrl+C or use `--port 8766` |
| `--enable-run` is unrecognized | Run `replayfix_dashboard.py` from this extracted project folder |
| Docker is not ready | Open Docker Desktop and wait for the Linux engine |
| Python image is missing | Run `docker pull python:3.12-slim` |
| No new run appears | Confirm that the dashboard is showing `runs/`, not the saved `examples/` folder |
| HTTP 401 | Check the Token Factory API key |
| HTTP 402 or 429 | Check credits, quota, and rate limits |
| HTTP 400 or 404 | Check the exact model ID and supported settings in the provider's View code output |
| Not verified | Inspect each recorded rejection or test failure; do not treat it as success |

Official model-service setup: https://docs.tokenfactory.nebius.com/quickstart
