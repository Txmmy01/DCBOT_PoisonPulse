# PoisonPulse Bot für HugoSMP.net

## Installation
1. Python 3.10+ installieren.
2. `pip install -r requirements.txt`
3. `config.example.json` nach `config.json` kopieren und Token sowie alle IDs eintragen.
4. Discord Developer Portal: Server Members Intent und Message Content Intent aktivieren.
5. `python bot.py` starten.

`config.json` darf nicht veröffentlicht werden. Das Bot-Profilbild wird separat im Discord Developer Portal gesetzt. `poisonpulse_emoji_id` bindet das Custom-Emoji in Branding-Embeds ein; `poisonpulse_image_url` ist ein optionales Thumbnail.

## Funktionen
- Welcome- und Leave-Logs
- Support-Tickets mit Auswahl, Claim und Transcript
- Getrennte Team- und Clan-Bewerbungsformulare mit Annahme/Ablehnung
- Giveaways mit automatischer Ziehung und `/greroll`
- `/warn`, `/timeout`, `/kick`, `/ban`, `/unban`, `/clear`, `/warns`, `/unwarn`
- Discord-Invite-Filter

Dauern: `30m`, `2h`, `1d`, `1w`. Daten werden in `poisonpulse.db` gespeichert.

Der Bot benötigt in Discord passende Rechte für Nachrichten, Kanäle, Rollen, Moderation und Nachrichtenverlauf. Die höchste benötigte Bot-Rolle muss über den Rollen liegen, die er vergeben oder moderieren soll.