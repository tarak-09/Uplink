# Testing Uplink on your own WhatsApp

You need a Meta developer account (log in with Facebook), your phone with WhatsApp, and about
fifteen minutes. Meta lends you a free test business number, so you do not need a business
or a second SIM.

## 1. Create the app and get the test number

1. Open [developers.facebook.com](https://developers.facebook.com/apps), choose **Create App**.
2. Pick the use case **Connect with customers through WhatsApp**, then choose or create a
   business portfolio.
3. Click **Start using the API**. You land on **WhatsApp > API Setup**.
4. **From** already shows a test number. Copy the **Phone number ID** shown with it (a long
   number, not the phone number itself) into `.env` as `WHATSAPP_PHONE_NUMBER_ID`.
5. **To**: add your own WhatsApp number and type in the code WhatsApp sends you. The test
   number can only message the few numbers on this list. Put the number, digits with country
   code (`14155550123`), into `.env` as `WHATSAPP_TO`.
6. Click **Generate access token** and put it into `.env` as `WHATSAPP_TOKEN`. It lasts about
   24 hours; when sending fails with code 190, generate a new one.

## 2. Your first message

```bash
python3 -m uplink send-test --template
```

Meta's "Hello World" template should arrive within seconds. Templates are delivered at any time.

Now open WhatsApp on your phone and send **hi** to the test number. That opens a 24-hour window
in which the number may send you ordinary messages, which is what alerts are. Then:

```bash
python3 -m uplink send-test
```

If the template arrives but this text does not, the window is closed: send "hi" again.

## 3. A real alert on your phone

```bash
python3 -m uplink analyze --file tests/fixtures/munich-2026-10-23.json --send --pick 1
```

This sends the real rooftop cascade from 22 October, written by Muse Spark in your `LANGUAGE`
(`english`, `telugu` or `hindi`). Without `MODEL_API_KEY` you get the hand-written template instead.

## 4. Live alerts

```bash
python3 -m uplink watch
```

Leave it running and keep the laptop awake. It logs one line per minute and sends a message when
several nodes fail together, plus a short "back online" message when they recover. A single
router switched off does not page you, unless you list it in `WATCH_NODES`.

## 5. Questions and voice notes

For Uplink to answer your messages, Meta has to reach your laptop over HTTPS. A tunnel does that.

1. Install a tunnel: `brew install cloudflared` (macOS), `winget install Cloudflare.cloudflared`
   (Windows), or use ngrok.
2. Add to `.env`:
   - `WHATSAPP_APP_SECRET`: **App settings > Basic > App secret > Show**. Uplink uses it to reject
     requests that did not come from Meta.
   - `WHATSAPP_VERIFY_TOKEN`: any phrase you make up.
3. Start alerts and the webhook together:
   ```bash
   python3 -m uplink run
   ```
4. In a second terminal:
   ```bash
   cloudflared tunnel --url http://localhost:8080
   ```
   It prints an address like `https://quiet-river-1234.trycloudflare.com`.
5. In the app dashboard open **WhatsApp > Configuration**, edit the webhook:
   - Callback URL: the tunnel address followed by `/webhook`
   - Verify token: your phrase from step 2

   Click **Verify and save**; Uplink answers the check by itself.
6. Under **Webhook fields**, subscribe to **messages**.
7. On your phone, message the test number:
   - `status` gives the quick summary
   - "what is down right now?" is answered by Muse Spark from Uplink's latest state
   - hold the microphone and ask in Telugu or Hindi: the reply starts with what Uplink heard

A quick tunnel gets a new address every time it starts; paste the new one into the dashboard.

## Keeping it running for the demo (optional)

- **A token that does not expire**: Business settings > Users > System users > Add. Assign the app
  and the WhatsApp account to the user, then Generate token with `whatsapp_business_messaging` and
  `whatsapp_business_management`.
- **Alerts outside the 24-hour window**: in WhatsApp Manager > Message templates, create a template
  in the Utility category with the body `Uplink alert: {{1}}` in English. Once it is approved, set
  `WHATSAPP_ALERT_TEMPLATE` to its name and `WHATSAPP_TEMPLATE_LANGUAGE=en`. Uplink then sends every
  alert through it (line breaks become " · ", as templates require).

## Meta locked the account? Use Twilio's sandbox

Some brand-new WhatsApp Business Accounts are locked by Meta's automatic checks: messages are
accepted, then fail with error **131031, "Business Account locked"** under **Check test webhooks**.
A review can take days. Twilio's WhatsApp sandbox sends through Twilio's own approved account, so
it works in the meantime, and alerts still arrive on your WhatsApp.

1. Sign up at [twilio.com/try-twilio](https://www.twilio.com/try-twilio); the free trial is enough.
2. In the Twilio Console open **Messaging > Try it out > Send a WhatsApp message**. Send the
   `join ...` code it shows from your WhatsApp to **+1 415 523 8886** and wait for the confirmation.
3. On the Console home page, under **Account Info**, copy the **Account SID** and **Auth Token**:
   ```
   WHATSAPP_PROVIDER=twilio
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...
   WHATSAPP_TO=14155550123
   ```
4. `python3 -m uplink send-test`: the message arrives from +1 415 523 8886.
5. For texts and voice notes, run `python3 -m uplink run` and the tunnel as in step 5 above, then in
   the Twilio Console open the sandbox settings and set **When a message comes in** to the tunnel
   address followed by `/twilio`, method **POST**, and save. No app secret or verify token is needed:
   Uplink checks Twilio's signature with your Auth Token.

The sandbox forgets your number after three days (send the join code again) and, like the test
number, only delivers ordinary messages within 24 hours of your last message to it. It cannot send
templates, so `send-test --template` and `WHATSAPP_ALERT_TEMPLATE` do not apply.

If Meta unlocks your account later, set `WHATSAPP_PROVIDER=cloud` to switch back.

## No WhatsApp at all? Use a Telegram bot

When Meta has locked the account and Twilio's trial number only sends its own templates (error
**21654, "ContentSid Required"**), a Telegram bot can stand in. It needs no business account, no
webhook and no tunnel: Uplink fetches the bot's messages itself.

1. In Telegram, message **@BotFather**, send `/newbot`, and choose a name and a username ending in
   `bot`. BotFather answers with the bot's token, which works like a password.
2. Open your new bot (BotFather links to it) and press **Start**.
3. Add the token to `.env`, then find your chat id:
   ```
   WHATSAPP_PROVIDER=telegram
   TELEGRAM_BOT_TOKEN=123456789:AAE...
   ```
   ```bash
   python3 -m uplink telegram-chat-id
   ```
   It lists the chats that messaged the bot. Add yours as `TELEGRAM_CHAT_ID=...`.
4. `python3 -m uplink send-test`: the test message arrives in the bot's chat.
5. `python3 -m uplink run` sends the alerts there and answers texts and voice notes from that chat
   only. Telegram has no 24-hour window and no templates, so `send-test --template` and
   `WHATSAPP_ALERT_TEMPLATE` do not apply.

A group works too: add the bot and use the group's id, which starts with `-`. In a group the bot
only sees commands such as `/status` unless you turn off privacy mode (`/setprivacy` in @BotFather).
Only one program can fetch a bot's messages at a time, so stop `uplink run` before running
`telegram-chat-id` again. Set `WHATSAPP_PROVIDER` back to `cloud` or `twilio` to return to WhatsApp.

## When something fails

| What you see | What it means | What to do |
|---|---|---|
| code 131030 | your number is not on the recipient list | add it under API Setup > To and confirm the code |
| `failed` with 131031 under Check test webhooks | Meta locked the new business account | request a review, and use Twilio's sandbox meanwhile (above) |
| `delivery_failed` with 131047 in the log | the 24-hour window is closed | send "hi" to the test number |
| code 190 | the access token expired | generate a new one, or use a system user token |
| code 100 | a parameter was rejected | `WHATSAPP_PHONE_NUMBER_ID` must be the ID, not the number |
| code 132001 | template name or language is wrong, or not approved | check WhatsApp Manager |
| "Verify and save" fails | Meta could not reach Uplink | is `uplink run` running, the tunnel address current, the path `/webhook`, the verify token identical? |
| messages arrive, no reply | Uplink ignored them | it only answers `WHATSAPP_TO` (with Telegram, `TELEGRAM_CHAT_ID`); "rejected a webhook call" in the log means a wrong app secret |
| "Voice notes need ffmpeg and a Model API key" | the laptop is missing one of them | install ffmpeg, set `MODEL_API_KEY` |
| Twilio code 63015 | your number has not joined the sandbox, or its three days ran out | send the join code to +1 415 523 8886 |
| Twilio code 63016 | more than 24 hours since you messaged the sandbox | send it "hi" |
| Twilio code 20003 | wrong Account SID or Auth Token | copy them again from the Console home page |
| "rejected a Twilio call" in the log | the signature did not match the address | set `TWILIO_WEBHOOK_URL` to the exact address you typed into the Twilio Console |
| Twilio code 21654 | this Twilio number sends only templates | use a Telegram bot (above) |
| Telegram code 401 | wrong bot token | copy `TELEGRAM_BOT_TOKEN` again from @BotFather |
| Telegram "chat not found" | wrong `TELEGRAM_CHAT_ID`, or nobody pressed Start | press Start in the bot's chat, then run `uplink telegram-chat-id` |
| Telegram code 409 in the log | another program is fetching the bot's messages | stop the second `uplink serve` or `run` |
