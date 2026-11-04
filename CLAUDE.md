# Uplink: notes for Claude Code

Uplink watches the Freifunk München community Wi-Fi map, works out which router failures share a
cause, and sends the volunteers a message (English, Telugu or Hindi) saying what broke, why, and
what to check first. Muse Spark writes the messages, Muse Voice Transcribe hears voice notes, and
WhatsApp's Cloud API or a Telegram bot delivers them (`MESSENGER`). The demo runs on Telegram
(`MESSENGER=telegram`): Meta locked the new WhatsApp Business Account (error 131031) until it reviews
it. README.md explains the design; docs/DEMO.md is the demo script.

## Ground rules

- Python 3.10+ standard library only. Do not add dependencies (ruff, mypy, coverage and vulture are
  dev tools, never in pyproject's dependencies).
- Keep the architecture. Make the smallest change that works, and add or adjust a test with it.
- After any code change: `python3 -m unittest discover -s tests -t .` (about 10 s). Before calling a
  change done: `python3 scripts/test_many.py` (the suite 10 times, shuffled, fresh interpreters,
  warnings as errors). All runs must pass.
- CI (.github/workflows/ci.yml) also runs `ruff check .`, `ruff format --check .`, `mypy uplink`, and
  `coverage run -m unittest discover -s tests -t .` with `coverage report --include="uplink/*"
  --fail-under=90`, on Linux and Windows. Keep them passing.
- The tests use fakes of each service (`tests/support.py`), checked against the real ones. If a real
  response differs from what the code expects, first update the matching fake, then fix the code. If
  Muse Spark drafts keep failing the number check, tune the prompt in `writer.py`; keep the check.
- Secrets live in `.env`, which the user edits by hand. Never print, echo, log or commit its values,
  and never `cat` it. To check a variable exists, test for it without showing it.
- Never read or output the map's `owner`/contact fields. Never message any number except
  `WHATSAPP_TO`, or any Telegram chat except `TELEGRAM_CHAT_ID`. Never poll the public map more than
  once a minute.
- Long-running processes (`uplink watch`, `uplink run`, `cloudflared`) belong in the user's own
  terminal: give the user the command instead of running it yourself.
- Commit or push only when the user asks. `.env` and `data/` must never be committed.
- On Windows use `py` instead of `python3`. On macOS, `caffeinate -i <command>` keeps the laptop awake.

## Commands

| Command | Use |
|---|---|
| `python3 -m uplink check` | fetch the live map once, show what is configured |
| `python3 -m uplink analyze --file tests/fixtures/munich-2026-10-23.json` | explain the real 23 Oct snapshot (offline) |
| `... analyze --file tests/fixtures/munich-2026-10-23.json --html outages.html` | also draw the outage page |
| `... analyze --file tests/fixtures/munich-2026-10-23.json --send [--dry-run] --pick 1` | write (Muse Spark) and send the alert for the rooftop cascade |
| `python3 -m uplink send-test [--template]` | check delivery (WhatsApp or Telegram); prints fixes for common error codes |
| `python3 -m uplink telegram-chat-id` | list the chats that messaged the Telegram bot, to find `TELEGRAM_CHAT_ID` |
| `python3 -m uplink watch [--dry-run]` | live alerts (user's terminal) |
| `python3 -m uplink run` | live alerts plus replies: the WhatsApp webhook, or Telegram polling (user's terminal) |
| `python3 -m uplink ask "what is down?"` / `ask --audio note.ogg` | test the assistant and transcription from the terminal |
| `python3 -m uplink report` | numbers from the live run (for the README) |

## Layout

`uplink/`: `mesh.py` (data model, map reader), `feed.py` + `transport.py` (HTTP), `correlate.py`
(evidence and outage grouping), `incidents.py` (lifecycle, alert policy), `facts.py` + `templates.py`
+ `writer.py` (what messages say, fallback texts, Muse Spark with checks), `incident_page.py` (the
outage page for `analyze --html`), `muse.py` (Meta Model API), `whatsapp.py` (Cloud API, and the
`Messenger` protocol and `MessengerError` both messengers share), `telegram.py` (Telegram bot and its
poller), `voice.py` (ffmpeg), `monitor.py` (watch loop), `server.py` (webhook and reply queue),
`assistant.py` (questions), `store.py` (data folder), `report.py`, `config.py`, `app.py` (wiring),
`cli.py`. `tests/support.py` holds the mesh builder and the fake map, Meta, WhatsApp and Telegram
servers.

## Known gotchas

- WhatsApp and Telegram setup steps and an error table are in `docs/WHATSAPP_SETUP.md`; walk the
  user through it.
- `WHATSAPP_PROVIDER` was renamed `MESSENGER` (`whatsapp` or `telegram`). An `.env` that still has the
  old name stops Uplink at startup with the line to write instead.
- `WHATSAPP_PHONE_NUMBER_ID` is the ID shown under API Setup, not the phone number.
- Temporary access tokens expire after about 24 hours (error 190): generate a new one and restart.
- Free text is only delivered within 24 hours of the user's last message to the test number. A
  `delivery_failed` event with code 131047 in `data/events.jsonl` means the user should send "hi".
  New accounts may lack the `hello_world` template, so prefer `send-test` over `send-test --template`.
- Telegram: only one program may fetch a bot's messages, so a 409 "Conflict" in the log means a second
  `uplink serve`/`run` (or `telegram-chat-id`) is running. "chat not found" means a wrong
  `TELEGRAM_CHAT_ID`, or nobody pressed Start. The token is part of every Bot API URL, so Uplink scrubs
  it from errors and logs; never print those URLs. Messages sent to a new bot before anything had
  used its token were never delivered; if `telegram-chat-id` finds nothing, send a new message and rerun.
- The webhook answers only `WHATSAPP_TO`. "rejected a webhook call" in the log means a wrong
  `WHATSAPP_APP_SECRET`.
- The live map gives some routers Go's zero time (0001-01-01) as their boot time; the compact snapshot
  format keeps it, so do not reintroduce `datetime.fromtimestamp`, which fails for it on Windows.
- On Windows, `choco install ffmpeg` needs an Administrator window (VS Code's terminal is not one).
- For the public demo video use `ANONYMIZE=true` unless Freifunk München has agreed otherwise.
