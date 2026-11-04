"""Assembles Uplink's parts from Settings, so the CLI and the tests wire them the same way."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import __version__
from .assistant import Assistant
from .config import Settings
from .feed import Feed, open_feed
from .monitor import ConsoleSender, MessengerSender, Monitor, Sender
from .muse import MetaModelAPI
from .server import WebhookApp
from .store import Store
from .telegram import TelegramBot, TelegramPoller
from .transport import Transport
from .whatsapp import Messenger, WhatsAppClient
from .writer import Writer


def make_transport(settings: Settings) -> Transport:
    contact = f"; contact: {settings.contact}" if settings.contact else ""
    return Transport(user_agent=f"Uplink/{__version__} (community mesh outage alerts{contact})")


def make_model(settings: Settings, transport: Transport) -> MetaModelAPI | None:
    if not settings.has_model:
        return None
    return MetaModelAPI(
        api_key=settings.model_api_key,
        transport=transport,
        base_url=settings.model_base_url,
        model=settings.model_name,
        asr_model=settings.asr_model,
        reasoning_effort=settings.reasoning_effort,
    )


def make_telegram_bot(settings: Settings, transport: Transport) -> TelegramBot:
    return TelegramBot(token=settings.telegram_bot_token, transport=transport, base_url=settings.telegram_base_url)


def make_messenger(settings: Settings, transport: Transport) -> Messenger | None:
    """The configured messenger (WhatsApp or Telegram), or None while its credentials are missing."""
    if settings.uses_telegram:
        return make_telegram_bot(settings, transport) if settings.telegram_bot_token else None
    if not (settings.whatsapp_token and settings.whatsapp_phone_number_id):
        return None
    return WhatsAppClient(
        token=settings.whatsapp_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        transport=transport,
        api_version=settings.whatsapp_api_version,
        base_url=settings.graph_base_url,
    )


def make_writer(settings: Settings, model: MetaModelAPI | None) -> Writer:
    return Writer(model, language=settings.language, network=settings.network_name)


def make_sender(settings: Settings, messenger: Messenger | None, *, dry_run: bool) -> Sender:
    if settings.uses_telegram:
        if dry_run or messenger is None or not settings.telegram_chat_id:
            return ConsoleSender()
        return MessengerSender(messenger, settings.telegram_chat_id, service_window=False)
    if dry_run or messenger is None or not settings.whatsapp_to:
        return ConsoleSender()
    return MessengerSender(
        messenger,
        settings.whatsapp_to,
        template=settings.whatsapp_alert_template,
        template_language=settings.whatsapp_template_language,
    )


def make_monitor(
    settings: Settings,
    *,
    dry_run: bool = False,
    feed: Feed | None = None,
    store: Store | None = None,
    transport: Transport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Monitor:
    transport = transport or make_transport(settings)
    return Monitor(
        settings=settings,
        feed=feed or open_feed(settings.feed_url, transport),
        store=store or Store(settings.data_dir),
        writer=make_writer(settings, make_model(settings, transport)),
        sender=make_sender(settings, make_messenger(settings, transport), dry_run=dry_run),
        clock=clock,
    )


def make_assistant(settings: Settings, store: Store, model: MetaModelAPI | None) -> Assistant:
    return Assistant(store=store, settings=settings, writer=make_writer(settings, model))


def make_webhook(settings: Settings, *, store: Store | None = None, transport: Transport | None = None) -> WebhookApp:
    transport = transport or make_transport(settings)
    messenger = make_messenger(settings, transport)
    if messenger is None:
        raise ValueError("the webhook needs " + ", ".join(settings.missing_for_webhook()))
    store = store or Store(settings.data_dir)
    model = make_model(settings, transport)
    return WebhookApp(
        settings=settings,
        messenger=messenger,
        assistant=make_assistant(settings, store, model),
        store=store,
        model=model,
    )


def make_telegram_poller(settings: Settings, app: WebhookApp, *, transport: Transport | None = None) -> TelegramPoller:
    """Telegram's stand-in for the webhook: messages from TELEGRAM_CHAT_ID go into `app`'s reply queue.

    It gets its own transport, because it runs in its own thread next to the reply worker.
    """
    bot = make_telegram_bot(settings, transport or make_transport(settings))
    return TelegramPoller(bot, settings.telegram_chat_id, app._enqueue)
