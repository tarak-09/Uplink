# Uplink: notes for Claude Code

Uplink watches the Freifunk München community Wi-Fi map, works out which router failures share a
cause, and sends the volunteers a WhatsApp message (English, Telugu or Hindi) saying what broke, why,
and what to check first. Muse Spark writes the messages, Muse Voice Transcribe hears voice notes,
and WhatsApp delivers them (for the demo, a Telegram bot does). README.md explains the design.

## Today

- Hackathon: Meta Global AI Developer Hackathon. Submission deadline **Wednesday Nov 4, 8:00 PM
  Central**. Goal: submit Tuesday evening and keep Wednesday as a buffer.
- The code is finished and tested. Today is about running it on this laptop against the real
  services, recording a two-minute demo (docs/DEMO.md) and submitting.
- Prefer fixing setup problems over adding features. Add nothing new unless the user asks.
- For the demo, messages go through a Telegram bot: `WHATSAPP_PROVIDER=telegram`. Meta locked the new
  WhatsApp Business Account (error 131031) until it reviews it, and Twilio's trial number only sends
  templates (error 21654). Details under item 3 below.

## Ground rules

- Python 3.10+ standard library only. Do not add dependencies.
- Keep the architecture. Make the smallest change that works, and add or adjust a test with it.
- After any code change: `python3 -m unittest discover -s tests -t .` (about 10 s). Before calling a
  change done: `python3 scripts/test_many.py` (the suite 10 times, shuffled, fresh interpreters,
  warnings as errors). All runs must pass.
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
+ `writer.py` (what messages say, fallback texts, Muse Spark with checks), `muse.py` (Meta Model API),
`whatsapp.py` (Cloud API), `twilio.py` (Twilio sandbox), `telegram.py` (Telegram bot and its poller),
`voice.py` (ffmpeg), `monitor.py` (watch loop), `server.py` (webhook and reply queue), `assistant.py`
(questions), `store.py` (data folder), `report.py`, `config.py`, `app.py` (wiring), `cli.py`.
`tests/support.py` holds the mesh builder and the fake map, Meta, WhatsApp, Twilio and Telegram servers.

## Checks against the real services

The code was written without network access to Meta, WhatsApp or the live map. The tests use fakes
built from each service's documentation. Check each on first contact, in this order:

1. **Live map**: verified on 3 Nov. `python3 -m uplink check` printed 2,087 nodes.
2. **Muse Spark**: verified against the real API on 3 Nov (`POST {MODEL_BASE_URL}/chat/completions`,
   bearer `MODEL_API_KEY`, body `model`, `messages` with developer/user roles (assistant too, for
   follow-ups), `max_completion_tokens`, `reasoning_effort`; reply text at `choices[0].message.content`,
   and `finish_reason`). Follow-ups too: with the chat's last exchange as earlier turns, "can you say that
   in Telugu?" translated the last answer, numbers and "Munich time" intact, and "why is the sky blue?"
   was declined.
   `python3 -m uplink analyze --file tests/fixtures/munich-2026-10-23.json --send --dry-run --pick 1`
   ends with "Alert written by muse" in English and Telugu. If it says "template (...)", the note in
   parentheses says why. Reasoning counts toward `max_completion_tokens` (about 830-950 tokens per alert
   at `low`), so the cap is 4096 and a reply cut off with `finish_reason: "length"` falls back to the template.
3. **Messages**: we use `WHATSAPP_PROVIDER=telegram` for the demo. Meta locked the new WhatsApp Business
   Account (error 131031, "Business Account locked") until it reviews it, and Twilio's trial number only
   sends templates. Setup: "No WhatsApp at all? Use a Telegram bot" in docs/WHATSAPP_SETUP.md.
   - **Telegram bot** (@Uplink_hackathon_bot), verified on 3 Nov (`POST {TELEGRAM_BASE_URL}/bot<token>/sendMessage`,
     JSON `chat_id`, `text` as HTML with `parse_mode: "HTML"`, `reply_parameters`; reply `{"ok": true,
     "result": {"message_id": ...}}`; `getUpdates` with `offset`, `timeout: 30`, `allowed_updates:
     ["message"]`; voice notes by `getFile`, then `GET {TELEGRAM_BASE_URL}/file/bot<token>/<file_path>`).
     `telegram-chat-id` found the chat, `send-test` and a Muse Spark alert (HTML, bold) were accepted,
     and `uplink run` answered `/start` (help text) and "what is down right now?" (Muse Spark) in the
     chat. Not yet seen live: a voice note through Telegram, and an alert from `uplink run` itself.
   - **Twilio sandbox**, tried on 3 Nov and refused: this account's trial number (+1 737 258 3478, set as
     `TWILIO_WHATSAPP_FROM`; not the classic +1 415 523 8886) answers free-form `Body` sends with 21654
     "ContentSid Required", even right after the join. Request:
     `POST {TWILIO_BASE_URL}/2010-04-01/Accounts/{SID}/Messages.json`, Basic auth, form fields `From`,
     `To`, `Body` as `whatsapp:+<digits>`; reply `sid`. For replies, the sandbox's "When a message comes
     in" is the tunnel address plus `/twilio` (not `/webhook`).
   - **Meta Cloud API** (Graph API `v26.0` unless `WHATSAPP_API_VERSION` says otherwise), back with
     `WHATSAPP_PROVIDER=cloud` once Meta unlocks the account: skip `send-test --template` there too,
     because this account got Meta's newer sample templates and `hello_world` may not exist. Use
     `send-test` after the user has replied "hi" to the test number.
4. **Muse Voice Transcribe**: verified against the real API on 3 Nov (`POST {MODEL_BASE_URL}/asr/transcribe`,
   multipart: a `request` JSON part with `model`, `audioEncoding: "WAV"`, `mode: "PUSH_TO_TALK"`,
   `languageBias`, `keywords`, plus an `audio` WAV part). A spoken "What is down right now?" as an
   OGG/Opus voice note came back word for word through `python3 -m uplink ask --audio <file>`, answered
   by Muse Spark in about 8 s. ffmpeg 9.0.2 is installed with Chocolatey (that needs an Administrator
   window; VS Code's terminal is not one).

If a real response differs from what the code expects, first update the matching fake in
`tests/support.py` to behave like the real service, then fix the code, then run the tests. If Muse
Spark drafts keep failing the number check, tune the prompt in `writer.py`; keep the check.

## Known gotchas

- WhatsApp setup steps and an error table are in `docs/WHATSAPP_SETUP.md`; walk the user through it.
- `WHATSAPP_PHONE_NUMBER_ID` is the ID shown under API Setup, not the phone number.
- Temporary access tokens expire after about 24 hours (error 190): generate a new one and restart.
- Free text is only delivered within 24 hours of the user's last message to the test number. A
  `delivery_failed` event with code 131047 in `data/events.jsonl` means the user should send "hi".
- Twilio sandbox: error 63015 means the user's number has not joined, or its three days ran out (send
  the join code to +1 415 523 8886 again); 63016 means no message from the user in 24 hours (send
  "hi"); 20003 means a wrong `TWILIO_ACCOUNT_SID` or `TWILIO_AUTH_TOKEN`.
- Telegram: only one program may fetch a bot's messages, so a 409 "Conflict" in the log means a second
  `uplink serve`/`run` (or `telegram-chat-id`) is running. "chat not found" means a wrong
  `TELEGRAM_CHAT_ID`, or nobody pressed Start. The token is part of every Bot API URL, so Uplink scrubs
  it from errors and logs; never print those URLs. Messages sent to the new bot before anything had
  used its token were never delivered; if `telegram-chat-id` finds nothing, send a new message and rerun.
- The webhook answers only `WHATSAPP_TO`. "rejected a webhook call" in the log means a wrong
  `WHATSAPP_APP_SECRET` (Cloud API); "rejected a Twilio call" means Twilio signed a different address:
  set `TWILIO_WEBHOOK_URL` to the exact address typed into the Twilio Console.
- For the public demo video use `ANONYMIZE=true` unless Freifunk München has agreed otherwise.
