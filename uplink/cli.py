"""The `uplink` command."""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Sequence

from . import __version__, templates
from .app import (
    make_assistant,
    make_model,
    make_monitor,
    make_sender,
    make_telegram_bot,
    make_telegram_poller,
    make_transport,
    make_webhook,
    make_whatsapp,
    make_writer,
)
from .config import ConfigError, Settings, load_settings
from .correlate import Situation, correlate
from .facts import Namer, clock, outage_facts
from .feed import FeedError, iter_recordings, open_feed, read_snapshot
from .incidents import Incident
from .mesh import Snapshot, dependency_points
from .monitor import SendError
from .muse import ModelAPIError
from .report import build_report, format_report, percent_fewer
from .server import ASR_KEYWORDS, ASR_LANGUAGES, WebhookApp, make_server
from .store import Store, StoreError
from .telegram import recent_chats
from .voice import AudioError, ffmpeg_available, to_wav
from .whatsapp import WhatsAppError

TEST_MESSAGE = "Uplink test: if you can read this, outage alerts will reach you here."
Handler = Callable[[argparse.Namespace, Settings], int]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose or args.command in {"watch", "serve", "run"} else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        settings = load_settings(env_file=args.env_file, data_dir=args.data_dir)
        handler: Handler = args.handler
        return handler(args, settings)
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # output piped into `head` and closed early: not an error
        _silence_stdout()
        return 0
    except (FeedError, StoreError, ModelAPIError, WhatsAppError, AudioError, SendError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", type=Path, default=Path(".env"), help="settings file (default: .env)")
    common.add_argument("--data-dir", type=Path, help="where Uplink keeps its state (default: ./data)")
    common.add_argument("-v", "--verbose", action="store_true", help="log more")

    parser = argparse.ArgumentParser(
        prog="uplink", description="Explains community mesh outages on WhatsApp, in your language."
    )
    parser.add_argument("--version", action="version", version=f"uplink {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, parents=[common], help=help_text, description=help_text)
        sub.set_defaults(handler=handler)
        return sub

    command("check", cmd_check, "fetch the map once and check your setup")

    analyze = command("analyze", cmd_analyze, "explain the outages visible in one snapshot")
    analyze.add_argument("--file", type=Path, help="a saved meshviewer.json instead of the live map")
    analyze.add_argument("--hours", type=float, default=24, help="how far back to look (default: 24)")
    analyze.add_argument("--json", action="store_true", help="print the facts as JSON")
    analyze.add_argument("--send", action="store_true", help="send the alert for one outage")
    analyze.add_argument("--pick", type=int, default=1, help="which outage to send, as numbered (default: 1)")
    analyze.add_argument("--dry-run", action="store_true", help="print the alert instead of sending it")

    watch = command("watch", cmd_watch, "watch the map and send alerts")
    watch.add_argument("--once", action="store_true", help="poll a single time and exit")
    watch.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")

    replay = command("replay", cmd_replay, "run recorded snapshots through Uplink")
    replay.add_argument("folder", type=Path, help="a recordings folder, e.g. data/recordings")
    replay.add_argument("--send", action="store_true", help="really send the alerts")

    command("serve", cmd_serve, "answer WhatsApp (or Telegram) messages and voice notes")

    run = command("run", cmd_run, "watch and serve in one process")
    run.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")

    ask = command("ask", cmd_ask, "ask Uplink a question from the terminal")
    ask.add_argument("question", nargs="?", default="", help='for example "what is down right now?"')
    ask.add_argument("--audio", type=Path, help="a voice note to transcribe and answer")

    send_test = command("send-test", cmd_send_test, "check that WhatsApp (or Telegram) messages reach you")
    send_test.add_argument("--template", action="store_true", help="send the hello_world template instead of text")
    send_test.add_argument("--text", default=TEST_MESSAGE, help="the text to send")

    command("telegram-chat-id", cmd_telegram_chat_id, "list the chats that messaged your Telegram bot")

    report = command("report", cmd_report, "numbers from the live run")
    report.add_argument("--json", action="store_true", help="print JSON")
    return parser


# -- commands ---------------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    started = time.monotonic()
    snapshot = _fetch(settings)
    elapsed = time.monotonic() - started
    stale = timedelta(seconds=settings.stale_after)
    name = Namer(snapshot.nodes, settings.anonymize)
    online = [n for n in snapshot.nodes.values() if n.online]
    quiet = [n for n in online if not snapshot.is_reachable(n.id, stale)]
    gateways = sum(1 for n in snapshot.nodes.values() if n.is_gateway)
    print(f"Feed       {settings.feed_url}")
    print(f"           map time {snapshot.taken_at:%Y-%m-%d %H:%M %z}, fetched in {elapsed:.1f} s")
    print(
        f"Network    {len(snapshot.nodes)} nodes, {snapshot.reachable_count(stale)} reachable, "
        f"{snapshot.clients_online(stale)} people connected, {gateways} gateway segments"
    )
    if quiet:
        print(f"           {len(quiet)} nodes still marked online have been silent for over {settings.stale_after} s")
    for i, point in enumerate(dependency_points(snapshot, stale, limit=3)):
        label = "Key nodes" if i == 0 else ""
        behind = f"{_plural(point.nodes_behind, 'node')} and {_plural(point.clients_behind, 'person', 'people')}"
        print(f"{label:<10} {name(point.node.id)} carries {behind}")
    print()
    for label, status in _setup_status(settings):
        print(f"{label:<12}{status}")
    return 0


def cmd_analyze(args: argparse.Namespace, settings: Settings) -> int:
    snapshot = read_snapshot(args.file) if args.file else _fetch(settings)
    incidents = _incidents_in(snapshot, settings, timedelta(hours=args.hours))
    notable = sorted(
        (i for i in incidents if i.size > 1 or i.kind == "gateway"),
        key=lambda i: (-i.size, -i.started_at.timestamp()),
    )
    if args.json:
        print(
            json.dumps(
                [{"id": i.id, **outage_facts(i, snapshot, settings)} for i in notable], ensure_ascii=False, indent=1
            )
        )
    else:
        _print_analysis(snapshot, settings, incidents, notable, args.hours)
    if not args.send:
        return 0
    if not 1 <= args.pick <= len(notable):
        print(f"There is no outage number {args.pick} to send.", file=sys.stderr)
        return 1
    return _send_alert(notable[args.pick - 1], snapshot, settings, dry_run=args.dry_run)


def cmd_watch(args: argparse.Namespace, settings: Settings) -> int:
    monitor = make_monitor(settings, dry_run=args.dry_run)
    if args.once:
        result = monitor.poll()
        detail = f" ({result.detail})" if result.detail else ""
        print(f"{result.status}: {result.open_incidents} open incidents, {result.sent} messages sent{detail}")
        return 0 if result.status != "failed" else 1
    monitor.run(threading.Event())
    return 0


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    if not args.folder.is_dir():
        raise ConfigError(f"{args.folder} is not a folder")
    with tempfile.TemporaryDirectory(prefix="uplink-replay-") as scratch:
        replay_settings = dataclasses.replace(settings, data_dir=Path(scratch), record_snapshots=False)
        store = Store(Path(scratch))
        monitor = make_monitor(replay_settings, dry_run=not args.send, store=store)
        count = 0
        for snapshot in iter_recordings(args.folder):
            monitor.observe(snapshot)
            count += 1
        print(f"Replayed {count} snapshots.\n")
        print(format_report(build_report(store.events())))
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    _require_webhook_settings(settings)
    app = make_webhook(settings)
    if settings.uses_telegram:
        stop = threading.Event()
        try:
            poller = _start_telegram(settings, app, stop)
            while poller.is_alive():
                poller.join(0.5)  # short waits, so Ctrl+C stops it at once, on Windows too
        finally:
            stop.set()
            app.close()
        return 0
    server = make_server(app, settings.webhook_host, settings.webhook_port)
    app.start()
    print(_webhook_banner(settings))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.close()
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    _require_webhook_settings(settings)
    monitor = make_monitor(settings, dry_run=args.dry_run)
    app = make_webhook(settings)
    if settings.uses_telegram:
        stop = threading.Event()
        try:
            _start_telegram(settings, app, stop)
            monitor.run(threading.Event())
        finally:
            stop.set()
            app.close()
        return 0
    server = make_server(app, settings.webhook_host, settings.webhook_port)
    app.start()
    listener = threading.Thread(target=server.serve_forever, name="uplink-webhook", daemon=True)
    listener.start()
    print(_webhook_banner(settings))
    try:
        monitor.run(threading.Event())
    finally:
        server.shutdown()
        server.server_close()
        app.close()
    return 0


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings.data_dir)
    model = make_model(settings, make_transport(settings))
    question = args.question
    if args.audio:
        if model is None:
            raise ConfigError("transcribing a voice note needs MODEL_API_KEY")
        wav = to_wav(args.audio.read_bytes(), ffmpeg=settings.ffmpeg)
        transcript = model.transcribe(wav, languages=ASR_LANGUAGES[settings.language], keywords=ASR_KEYWORDS)
        print(f"Heard: {transcript.text}\n")
        question = transcript.text
    if not question:
        raise ConfigError('ask a question, for example: uplink ask "what is down right now?"')
    draft = make_assistant(settings, store, model).reply(question)
    print(draft.text)
    if draft.note:
        print(f"\n[{draft.source}: {draft.note}]", file=sys.stderr)
    return 0


def cmd_send_test(args: argparse.Namespace, settings: Settings) -> int:
    whatsapp = make_whatsapp(settings, make_transport(settings))
    to = settings.telegram_chat_id if settings.uses_telegram else settings.whatsapp_to
    if whatsapp is None or not to:
        missing = ", ".join(settings.missing_for_alerts())
        raise ConfigError(f"set {missing} (see docs/WHATSAPP_SETUP.md)")
    service = "Telegram" if settings.uses_telegram else "WhatsApp"
    try:
        if args.template:
            ref = whatsapp.send_template(to, "hello_world", "en_US")
        else:
            ref = whatsapp.send_text(to, args.text)
    except WhatsAppError as exc:
        print(f"{service} refused the message: {exc}")
        if exc.hint:
            print(f"What to do: {exc.hint}")
        return 1
    print(f"{service} accepted the message (id {ref}).")
    if settings.uses_telegram:
        print("It is in the bot's chat now. Alerts arrive the same way, at any time of day.")
    elif settings.uses_twilio:
        print(
            "If it does not arrive within a minute: from WhatsApp, send your join code to +1 415 523 8886\n"
            "(the sandbox forgets you after three days), or any message if you joined earlier, then try again."
        )
    else:
        print(
            "If it does not arrive within a minute, WhatsApp dropped it after accepting it. To see why, open the\n"
            "app dashboard, WhatsApp > Try it out > Check test webhooks, and read the newest status. The usual\n"
            'reason is that your number has not messaged the test number in 24 hours: send it "hi" and try again.'
        )
    return 0


def cmd_telegram_chat_id(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.telegram_bot_token:
        raise ConfigError("set TELEGRAM_BOT_TOKEN first: @BotFather shows it when you create the bot")
    bot = make_telegram_bot(settings, make_transport(settings))
    try:
        chats = recent_chats(bot.get_updates())  # no offset: nothing is confirmed, `serve` still sees them all
    except WhatsAppError as exc:
        print(f"Telegram refused: {exc}")
        if exc.hint:
            print(f"What to do: {exc.hint}")
        return 1
    if not chats:
        print(
            "No messages yet. In Telegram, open your bot, press Start (or send it any message), then run this\n"
            "again. Stop uplink serve or run first: they fetch the bot's messages themselves."
        )
        return 1
    print("Chats that messaged your bot recently:")
    for chat in chats:
        print(f"  {chat.id:<16} {chat.kind:<11} {chat.name}")
    if len(chats) == 1:
        print(f"\nPut this line in .env:  TELEGRAM_CHAT_ID={chats[0].id}")
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    report = build_report(Store(settings.data_dir).events())
    print(json.dumps(report, indent=1) if args.json else format_report(report))
    return 0


# -- helpers ----------------------------------------------------------------------------------


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def _silence_stdout() -> None:
    """Point stdout at /dev/null so the interpreter's final flush does not fail again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, io.UnsupportedOperation):
        pass


def _fetch(settings: Settings) -> Snapshot:
    snapshot = open_feed(settings.feed_url, make_transport(settings)).fetch()
    if snapshot is None:
        raise FeedError("the map returned no data")
    return snapshot


def _incidents_in(snapshot: Snapshot, settings: Settings, lookback: timedelta) -> list[Incident]:
    down = snapshot.down_since(timedelta(seconds=settings.stale_after))
    recent = [node_id for node_id, dropped in down.items() if snapshot.taken_at - dropped <= lookback]
    outages = correlate(recent, Situation(snapshot.nodes, down, snapshot.links))
    return [Incident.from_outage(o, snapshot.nodes, detected_at=snapshot.taken_at) for o in outages]


def _print_analysis(
    snapshot: Snapshot, settings: Settings, incidents: list[Incident], notable: list[Incident], hours: float
) -> None:
    stale = timedelta(seconds=settings.stale_after)
    went_quiet = sum(i.size for i in incidents)
    print(
        f"{settings.network_name} · map time {snapshot.taken_at:%Y-%m-%d %H:%M} · "
        f"{len(snapshot.nodes)} nodes, {snapshot.reachable_count(stale)} reachable"
    )
    print(
        f"In the last {hours:g} h, {went_quiet} nodes went quiet: {len(notable)} outages involving several "
        f"nodes, and {len(incidents) - len(notable)} single nodes."
    )
    print(
        f"Uplink would send {_plural(len(notable), 'alert')} instead of {went_quiet} "
        f"({percent_fewer(len(notable), went_quiet)}% fewer).\n"
    )
    for number, incident in enumerate(notable, 1):
        facts = outage_facts(incident, snapshot, settings)
        headline = templates.render(facts, "english").splitlines()[0].strip("*")
        within = f", all within {facts['failed_within_seconds']} s" if "failed_within_seconds" in facts else ""
        print(f"{number}. {clock(incident.started_at, snapshot.taken_at)}  [{incident.kind}] {headline}{within}")
        for line in facts.get("evidence", []):
            print(f"     · {line}")
        print(f"     check first: {facts['check_first']}\n")


def _send_alert(incident: Incident, snapshot: Snapshot, settings: Settings, *, dry_run: bool) -> int:
    transport = make_transport(settings)
    writer = make_writer(settings, make_model(settings, transport))
    draft = writer.write(outage_facts(incident, snapshot, settings))
    sender = make_sender(settings, make_whatsapp(settings, transport), dry_run=dry_run)
    ref = sender.send(draft.text)
    note = f" ({draft.note})" if draft.note else ""
    print(f"Alert written by {draft.source}{note}; sent as {ref}.")
    return 0


def _setup_status(settings: Settings) -> list[tuple[str, str]]:
    provider = "Twilio sandbox" if settings.uses_twilio else "Cloud API"
    alerts_missing = settings.missing_for_alerts()
    webhook_missing = [name for name in settings.missing_for_webhook() if name not in alerts_missing]
    if settings.uses_telegram:
        messages = (
            "Telegram",
            f"ready, alerts go to chat {settings.telegram_chat_id}"
            if not alerts_missing
            else "missing " + ", ".join(alerts_missing),
        )
        replies = ("Webhook", "not needed: uplink serve and run fetch messages from Telegram")
    else:
        messages = (
            "WhatsApp",
            f"ready through the {provider}, alerts go to +{settings.whatsapp_to}"
            if not alerts_missing
            else f"{provider}: missing " + ", ".join(alerts_missing),
        )
        replies = ("Webhook", "ready" if not webhook_missing else "missing " + ", ".join(webhook_missing))
    return [
        (
            "Muse Spark",
            f"ready ({settings.model_name})"
            if settings.has_model
            else "not set up: add MODEL_API_KEY (alerts use templates until then)",
        ),
        messages,
        replies,
        (
            "Voice notes",
            "ffmpeg found" if ffmpeg_available(settings.ffmpeg) else f"ffmpeg not found ({settings.ffmpeg})",
        ),
        ("Language", f"{settings.language}, names {'anonymized' if settings.anonymize else 'shown'}"),
    ]


def _require_webhook_settings(settings: Settings) -> None:
    missing = settings.missing_for_webhook()
    if missing:
        needs = "answering Telegram messages" if settings.uses_telegram else "the webhook"
        raise ConfigError(f"{needs} needs " + ", ".join(missing))


def _start_telegram(settings: Settings, app: WebhookApp, stop: threading.Event) -> threading.Thread:
    """Start answering Telegram messages: the reply worker, and a thread that fetches the messages."""
    poller = make_telegram_poller(settings, app)
    thread = threading.Thread(target=poller.run, args=(stop,), name="uplink-telegram", daemon=True)
    app.start()
    thread.start()
    print(
        f"Fetching Telegram messages for chat {settings.telegram_chat_id}; no webhook or tunnel needed.\n"
        'Send your bot "status", a question, or a voice note.'
    )
    return thread


def _webhook_banner(settings: Settings) -> str:
    port = settings.webhook_port
    path, who = ("/twilio", "Twilio") if settings.uses_twilio else ("/webhook", "Meta")
    return (
        f"Webhook listening on http://{settings.webhook_host}:{port}{path}\n"
        f"Make it reachable for {who} with:  cloudflared tunnel --url http://localhost:{port}\n"
        f"then use the address it prints, followed by {path}, as the webhook URL."
    )
