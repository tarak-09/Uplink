# Two-minute demo

Record with `ANONYMIZE=true` unless Freifunk München has said you may show their router names.

| Time | Screen | Say |
|---|---|---|
| 0:00 | map.ffmuc.net | "Munich's free community Wi-Fi: about 2,100 routers, all run by volunteers. On a normal day dozens go dark. The map turns them red. It never says why." |
| 0:15 | phone, WhatsApp alert in Telugu | "Uplink worked out that one rooftop antenna failed, and the six antennas that reached the internet through it dropped within fifteen seconds. It tells the volunteer what to check first, in the language they think in." |
| 0:40 | terminal: `uplink analyze` | "It explains failures with evidence from the public map: who routed through whom, which nodes share a neighbour, which ones booted in the same minute and so share power. Six access points across three gateways: so it is not the gateways." |
| 1:05 | phone: voice note in Telugu, then the reply | "Volunteers can just ask. Muse Voice Transcribe hears the question, Muse Spark answers from Uplink's live state." |
| 1:30 | README diagram, test run | "Muse Spark only rephrases facts Uplink has already settled, and every draft is checked: an invented number is rejected. Plain Python, no dependencies, tested on the real Munich data." |
| 1:45 | `uplink report` | "Here is what it saw while watching the live network. Built for my world: the people who keep community networks running." |
