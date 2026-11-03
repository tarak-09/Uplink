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

## When something fails

| What you see | What it means | What to do |
|---|---|---|
| code 131030 | your number is not on the recipient list | add it under API Setup > To and confirm the code |
| `delivery_failed` with 131047 in the log | the 24-hour window is closed | send "hi" to the test number |
| code 190 | the access token expired | generate a new one, or use a system user token |
| code 100 | a parameter was rejected | `WHATSAPP_PHONE_NUMBER_ID` must be the ID, not the number |
| code 132001 | template name or language is wrong, or not approved | check WhatsApp Manager |
| "Verify and save" fails | Meta could not reach Uplink | is `uplink run` running, the tunnel address current, the path `/webhook`, the verify token identical? |
| messages arrive, no reply | Uplink ignored them | it only answers `WHATSAPP_TO`; "rejected a webhook call" in the log means a wrong app secret |
| "Voice notes need ffmpeg and a Model API key" | the laptop is missing one of them | install ffmpeg, set `MODEL_API_KEY` |
