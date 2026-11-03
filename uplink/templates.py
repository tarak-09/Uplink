"""Hand-written messages in English, Telugu and Hindi.

They are used when no model is configured, when the model is unreachable, and when a model
draft fails the writer's checks, so an alert always goes out and always says the right thing.
"""

from __future__ import annotations

from typing import Any, Mapping

TEXTS: dict[str, dict[str, str]] = {
    "english": {
        "headline.cascade.one": "{root} is down, and the node behind it went offline too",
        "headline.cascade.many": "{root} is down, and the {behind} nodes behind it went offline too",
        "headline.shared_neighbor": "{count} nodes behind {anchor} dropped at the same time",
        "headline.site": "{count} nodes at one location went offline together",
        "headline.gateway": "Gateway {root} is down",
        "headline.single": "{root} is offline",
        "since": "Since {started} · {network} · nodes offline: {count}",
        "people": "People affected: about {people}",
        "cause.cascade": "Likely cause: power or uplink failure at {root}",
        "cause.shared_neighbor": "Likely cause: the radio links into {anchor} ({anchor} is still online)",
        "cause.site": "Likely cause: a power or internet cut at that location",
        "cause.gateway": "Likely cause: the gateway server or its VPN service",
        "cause.single": "Likely cause: power or internet at {root}",
        "check.cascade": "Check first: power and cabling at {root}",
        "check.shared_neighbor": "Check first: the radio and antenna at {anchor}",
        "check.site": "Check first: power and the internet line there",
        "check.gateway": "Check first: the gateway server",
        "check.single": "Check first: power at {root}",
        "resolved.headline": "Back online: {root}",
        "resolved.body": "The outage lasted {duration}. Nodes back: {back} of {total}.",
        "resolved.still": "Still down: {still}",
        "resolved.firmware": "{root} came back on new firmware ({firmware}), so this was probably an update.",
        "stale": "*Uplink can't see the network*\nThe {network} map has not updated since {since}. Alerts are paused until it does.",
        "network_wide": "*{count} nodes went offline at once*\nThat looks like a problem with the map's monitoring or a central gateway rather than separate outages. Since {started}.",
        "status.title": "{network} right now",
        "status.nodes": "Nodes online: {online} of {total}",
        "status.people": "People connected: {people}",
        "status.none": "No ongoing outages.",
        "status.open": "Ongoing outages: {count}",
        "status.item": "• since {started}: {root} (nodes offline: {nodes})",
        "status.lone": "Single nodes offline recently: {count}",
        "help": 'Ask me anything about the network, for example "what is down right now?", or send a voice note. Send "status" for a quick summary.',
        "no_data": "Uplink has not seen the network yet. Start it with: uplink watch",
        "no_ai": "(AI answers are unavailable right now, so here is the summary.)",
        "voice_failed": "Sorry, I couldn't understand that voice note. Please type your question.",
        "voice_unavailable": "Voice notes need ffmpeg and a Model API key on the Uplink server. Please type your question.",
        "heard": '🎙️ I heard: "{transcript}"',
        "minute.one": "1 min",
        "minute.many": "{n} min",
        "hour.one": "1 h",
        "hour.many": "{n} h",
    },
    "telugu": {
        "headline.cascade.one": "{root} ఆఫ్‌లైన్ అయింది, దాని వెనుక ఉన్న మరో నోడ్ కూడా ఆఫ్‌లైన్ అయింది",
        "headline.cascade.many": "{root} ఆఫ్‌లైన్ అయింది, దాని వెనుక ఉన్న {behind} నోడ్‌లు కూడా ఆఫ్‌లైన్ అయ్యాయి",
        "headline.shared_neighbor": "{anchor} వెనుక ఉన్న {count} నోడ్‌లు ఒకేసారి ఆఫ్‌లైన్ అయ్యాయి",
        "headline.site": "ఒకే చోట ఉన్న {count} నోడ్‌లు కలిసి ఆఫ్‌లైన్ అయ్యాయి",
        "headline.gateway": "గేట్‌వే {root} పనిచేయడం లేదు",
        "headline.single": "{root} ఆఫ్‌లైన్‌లో ఉంది",
        "since": "{started} నుండి · {network} · ఆఫ్‌లైన్ నోడ్‌లు: {count}",
        "people": "ప్రభావితమైన వినియోగదారులు: సుమారు {people}",
        "cause.cascade": "కారణం కావచ్చు: {root} వద్ద విద్యుత్ లేదా అప్‌లింక్ సమస్య",
        "cause.shared_neighbor": "కారణం కావచ్చు: {anchor} కి వచ్చే రేడియో లింక్‌లు ({anchor} ఇంకా ఆన్‌లైన్‌లోనే ఉంది)",
        "cause.site": "కారణం కావచ్చు: ఆ ప్రదేశంలో విద్యుత్ లేదా ఇంటర్నెట్ అంతరాయం",
        "cause.gateway": "కారణం కావచ్చు: గేట్‌వే సర్వర్ లేదా దాని VPN సేవ",
        "cause.single": "కారణం కావచ్చు: {root} వద్ద విద్యుత్ లేదా ఇంటర్నెట్ సమస్య",
        "check.cascade": "ముందుగా చూడండి: {root} వద్ద విద్యుత్ సరఫరా, కేబుల్స్",
        "check.shared_neighbor": "ముందుగా చూడండి: {anchor} వద్ద రేడియో, యాంటెనా",
        "check.site": "ముందుగా చూడండి: అక్కడి విద్యుత్, ఇంటర్నెట్ లైన్",
        "check.gateway": "ముందుగా చూడండి: గేట్‌వే సర్వర్",
        "check.single": "ముందుగా చూడండి: {root} వద్ద విద్యుత్ సరఫరా",
        "resolved.headline": "తిరిగి ఆన్‌లైన్: {root}",
        "resolved.body": "అంతరాయం {duration} కొనసాగింది. తిరిగి వచ్చిన నోడ్‌లు: {total} లో {back}.",
        "resolved.still": "ఇంకా ఆఫ్‌లైన్: {still}",
        "resolved.firmware": "{root} కొత్త ఫర్మ్‌వేర్‌తో ({firmware}) తిరిగి వచ్చింది, కాబట్టి ఇది బహుశా అప్‌డేట్ వల్ల జరిగింది.",
        "stale": "*Uplink నెట్‌వర్క్‌ను చూడలేకపోతోంది*\n{network} మ్యాప్ {since} నుండి అప్‌డేట్ కాలేదు. అది మళ్లీ అప్‌డేట్ అయ్యే వరకు హెచ్చరికలు ఆగి ఉంటాయి.",
        "network_wide": "*{count} నోడ్‌లు ఒకేసారి ఆఫ్‌లైన్ అయ్యాయి*\nఇది వేర్వేరు అంతరాయాలు కాకుండా, మ్యాప్ మానిటరింగ్ లేదా ప్రధాన గేట్‌వే సమస్యలా కనిపిస్తోంది. {started} నుండి.",
        "status.title": "ప్రస్తుత స్థితి: {network}",
        "status.nodes": "ఆన్‌లైన్ నోడ్‌లు: {total} లో {online}",
        "status.people": "కనెక్ట్ అయిన వినియోగదారులు: {people}",
        "status.none": "ప్రస్తుతం అంతరాయాలు ఏవీ లేవు.",
        "status.open": "కొనసాగుతున్న అంతరాయాలు: {count}",
        "status.item": "• {started} నుండి: {root} (ఆఫ్‌లైన్ నోడ్‌లు: {nodes})",
        "status.lone": "ఇటీవల ఒంటరిగా ఆఫ్‌లైన్ అయిన నోడ్‌లు: {count}",
        "help": 'నెట్‌వర్క్ గురించి ఏదైనా అడగండి, ఉదాహరణకు "ఇప్పుడు ఏమి డౌన్ అయింది?", లేదా వాయిస్ నోట్ పంపండి. త్వరిత సారాంశం కోసం "status" అని పంపండి.',
        "no_data": "Uplink ఇంకా నెట్‌వర్క్‌ను చూడలేదు. దీన్ని ఇలా ప్రారంభించండి: uplink watch",
        "no_ai": "(ప్రస్తుతం AI సమాధానాలు అందుబాటులో లేవు, కాబట్టి ఇదిగో సారాంశం.)",
        "voice_failed": "క్షమించండి, ఆ వాయిస్ నోట్ నాకు అర్థం కాలేదు. దయచేసి మీ ప్రశ్నను టైప్ చేయండి.",
        "voice_unavailable": "వాయిస్ నోట్‌ల కోసం Uplink సర్వర్‌లో ffmpeg మరియు Model API కీ అవసరం. దయచేసి మీ ప్రశ్నను టైప్ చేయండి.",
        "heard": '🎙️ నేను విన్నది: "{transcript}"',
        "minute.one": "1 నిమిషం",
        "minute.many": "{n} నిమిషాలు",
        "hour.one": "1 గంట",
        "hour.many": "{n} గంటలు",
    },
    "hindi": {
        "headline.cascade.one": "{root} बंद हो गया, और उसके पीछे का एक और नोड भी ऑफ़लाइन हो गया",
        "headline.cascade.many": "{root} बंद हो गया, और उसके पीछे के {behind} नोड भी ऑफ़लाइन हो गए",
        "headline.shared_neighbor": "{anchor} के पीछे के {count} नोड एक साथ ऑफ़लाइन हो गए",
        "headline.site": "एक ही जगह के {count} नोड एक साथ ऑफ़लाइन हो गए",
        "headline.gateway": "गेटवे {root} काम नहीं कर रहा है",
        "headline.single": "{root} ऑफ़लाइन है",
        "since": "{started} से · {network} · ऑफ़लाइन नोड: {count}",
        "people": "प्रभावित लोग: लगभग {people}",
        "cause.cascade": "संभावित कारण: {root} पर बिजली या अपलिंक की समस्या",
        "cause.shared_neighbor": "संभावित कारण: {anchor} तक के रेडियो लिंक ({anchor} खुद अभी ऑनलाइन है)",
        "cause.site": "संभावित कारण: उस जगह बिजली या इंटरनेट कटना",
        "cause.gateway": "संभावित कारण: गेटवे सर्वर या उसकी VPN सेवा",
        "cause.single": "संभावित कारण: {root} पर बिजली या इंटरनेट की समस्या",
        "check.cascade": "पहले जाँचें: {root} पर बिजली और केबल",
        "check.shared_neighbor": "पहले जाँचें: {anchor} का रेडियो और एंटीना",
        "check.site": "पहले जाँचें: वहाँ की बिजली और इंटरनेट लाइन",
        "check.gateway": "पहले जाँचें: गेटवे सर्वर",
        "check.single": "पहले जाँचें: {root} पर बिजली",
        "resolved.headline": "फिर से ऑनलाइन: {root}",
        "resolved.body": "रुकावट {duration} तक रही। वापस आए नोड: {total} में से {back}।",
        "resolved.still": "अभी भी ऑफ़लाइन: {still}",
        "resolved.firmware": "{root} नए फ़र्मवेयर ({firmware}) के साथ लौटा, इसलिए यह शायद अपडेट था।",
        "stale": "*Uplink नेटवर्क नहीं देख पा रहा है*\n{network} का मैप {since} से अपडेट नहीं हुआ है। उसके ठीक होने तक अलर्ट रुके रहेंगे।",
        "network_wide": "*{count} नोड एक साथ ऑफ़लाइन हो गए*\nयह अलग-अलग रुकावटों के बजाय मैप की मॉनिटरिंग या किसी मुख्य गेटवे की समस्या लगती है। {started} से।",
        "status.title": "अभी की स्थिति: {network}",
        "status.nodes": "ऑनलाइन नोड: {total} में से {online}",
        "status.people": "जुड़े हुए लोग: {people}",
        "status.none": "अभी कोई रुकावट नहीं है।",
        "status.open": "जारी रुकावटें: {count}",
        "status.item": "• {started} से: {root} (ऑफ़लाइन नोड: {nodes})",
        "status.lone": "हाल में अकेले ऑफ़लाइन हुए नोड: {count}",
        "help": 'नेटवर्क के बारे में कुछ भी पूछें, जैसे "अभी क्या बंद है?", या वॉइस नोट भेजें। छोटे सारांश के लिए "status" भेजें।',
        "no_data": "Uplink ने अभी तक नेटवर्क नहीं देखा है। इसे शुरू करें: uplink watch",
        "no_ai": "(अभी AI जवाब उपलब्ध नहीं हैं, इसलिए यह सारांश है।)",
        "voice_failed": "माफ़ कीजिए, वह वॉइस नोट समझ नहीं आया। कृपया अपना सवाल टाइप करें।",
        "voice_unavailable": "वॉइस नोट के लिए Uplink सर्वर पर ffmpeg और Model API key चाहिए। कृपया अपना सवाल टाइप करें।",
        "heard": '🎙️ मैंने सुना: "{transcript}"',
        "minute.one": "1 मिनट",
        "minute.many": "{n} मिनट",
        "hour.one": "1 घंटा",
        "hour.many": "{n} घंटे",
    },
}


def text(language: str, key: str, **values: Any) -> str:
    table = TEXTS.get(language, TEXTS["english"])
    return table.get(key, TEXTS["english"][key]).format(**values)


def detect_language(message: str) -> str | None:
    """Telugu or Hindi if the message is written in that script, otherwise None."""
    if any("ఀ" <= ch <= "౿" for ch in message):
        return "telugu"
    if any("ऀ" <= ch <= "ॿ" for ch in message):
        return "hindi"
    return None


def duration(minutes: int, language: str) -> str:
    hours, rest = divmod(max(0, minutes), 60)
    parts = []
    if hours:
        parts.append(text(language, "hour.one" if hours == 1 else "hour.many", n=hours))
    if rest or not hours:
        parts.append(text(language, "minute.one" if rest == 1 else "minute.many", n=rest))
    return " ".join(parts)


def render(facts: Mapping[str, Any], language: str) -> str:
    event = facts["event"]
    if event == "outage":
        return _outage(facts, language)
    if event == "resolved":
        return _resolved(facts, language)
    if event == "feed_stale":
        return text(language, "stale", network=facts["network"], since=facts["since"])
    if event == "network_wide":
        return text(language, "network_wide", count=facts["nodes_offline"], started=facts["started"])
    raise ValueError(f"no template for event {event!r}")


def render_status(facts: Mapping[str, Any], language: str) -> str:
    lines = [
        f"*{text(language, 'status.title', network=facts['network'])}*",
        text(language, "status.nodes", online=facts["nodes_online"], total=facts["nodes_total"]),
    ]
    if facts.get("people_online"):
        lines.append(text(language, "status.people", people=facts["people_online"]))
    outages = facts.get("open_outages", [])
    if not outages:
        lines.append(text(language, "status.none"))
    else:
        lines.append(text(language, "status.open", count=len(outages)))
        lines += [
            text(language, "status.item", started=o["started"], root=o["root"], nodes=o["nodes"]) for o in outages[:5]
        ]
    if facts.get("lone_nodes_offline"):
        lines.append(text(language, "status.lone", count=facts["lone_nodes_offline"]))
    return "\n".join(lines)


def _outage(facts: Mapping[str, Any], language: str) -> str:
    kind = facts["kind"]
    anchor = facts.get("still_online_above")
    if kind == "shared_neighbor" and not anchor:
        kind = "site"
    values = {
        "root": facts["root"],
        "anchor": anchor or "",
        "count": facts["nodes_offline"],
        "behind": facts.get("nodes_behind_root", 0),
        "started": facts["started"],
        "network": facts["network"],
        "people": facts.get("people_affected", 0),
    }
    headline_key = f"headline.{kind}"
    if kind == "cascade":
        headline_key += ".one" if values["behind"] == 1 else ".many"
    lines = [f"*{text(language, headline_key, **values)}*", text(language, "since", **values)]
    if values["people"]:
        lines.append(text(language, "people", **values))
    lines.append(text(language, f"cause.{kind}", **values))
    lines.append(text(language, f"check.{kind}", **values))
    return "\n".join(lines)


def _resolved(facts: Mapping[str, Any], language: str) -> str:
    lines = [
        f"*{text(language, 'resolved.headline', root=facts['root'])}*",
        text(
            language,
            "resolved.body",
            duration=duration(facts["duration_minutes"], language),
            back=facts["nodes_back"],
            total=facts["nodes_total"],
        ),
    ]
    if facts.get("still_down"):
        lines.append(text(language, "resolved.still", still=", ".join(facts["still_down"])))
    if facts.get("came_back_on_new_firmware"):
        lines.append(
            text(language, "resolved.firmware", root=facts["root"], firmware=facts["came_back_on_new_firmware"])
        )
    return "\n".join(lines)
