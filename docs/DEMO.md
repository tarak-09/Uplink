# Two-minute demo

Uplink on Telegram, in Telugu: the outage page, an alert, a spoken question and a follow-up.

## Before you record

- In `.env`: `MESSENGER=telegram`, `LANGUAGE=telugu`, and `ANONYMIZE=true` unless Freifunk München
  has said you may show their router names.
- Draw the page and open it in the browser:
  `python3 -m uplink analyze --file tests/fixtures/munich-2026-10-23.json --html outages.html`
- Start `python3 -m uplink run` in a terminal, and open the bot's chat on the phone.

| Time | Screen | Say |
|---|---|---|
| 0:00 | map.ffmuc.net | "Munich's free community Wi-Fi: about 2,100 routers, all run by volunteers. On a normal day dozens go dark. The map turns them red. It never says why." |
| 0:15 | browser: `outages.html` | "In one day, 79 routers went quiet. Uplink found six real outages among them, so it sends six alerts instead of 79. The biggest: one rooftop antenna failed, and the six antennas behind it dropped within fifteen seconds." |
| 0:40 | terminal: `python3 -m uplink analyze --file tests/fixtures/munich-2026-10-23.json --send --pick 1`, then the phone | "Here is that alert as a volunteer gets it, in Telugu: what broke, why, and what to check first. Muse Spark writes it from facts Uplink has already settled." |
| 1:00 | phone: hold the microphone and ask in Telugu, "ఇప్పుడు ఏం డౌన్ అయింది?" (what is down right now?) | "Volunteers can just ask. Muse Voice Transcribe hears the question, and Muse Spark answers from the live map." |
| 1:25 | phone: type "say that in English" | "Follow-ups just work: Uplink remembers the last answer for ten minutes. Every draft is checked, and one with an invented number is rejected; the hand-written template goes out instead." |
| 1:45 | README: the test run | "Plain Python, no dependencies, tested on the real Munich data. Built for the people who keep community networks running." |
