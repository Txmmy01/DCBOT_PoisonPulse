# PoisonPulse Bot für HugoSMP.net

## Installation
1. Python 3.10 oder neuer installieren.
2. `pip install -r requirements.txt`
3. `config.example.json` als `config.json` speichern.
4. Alle Channel- und Rollen-IDs sowie den Bot-Token in `config.json` eintragen.
5. Im Discord Developer Portal die Intents **Server Members** und **Message Content** aktivieren.
6. Bot einladen mit den Rechten zum Verwalten von Channels/Rollen, Nachrichten, Nachrichtenverlauf, Anhängen, Timeout, Kick und Ban.
7. `python bot.py` starten. Danach als Admin `/setup_panels` ausführen.

## Konfiguration
Die IDs werden ausschließlich aus `config.json` gelesen. Für den Clan-Bewerbungstext außerdem `clan_entry_fee` und `clan_requirements` ausfüllen. `server_info_text` wird in der Willkommensnachricht angezeigt. `poisonpulse_emoji_id` ist die ID des einzigen Branding-Emojis. `poisonpulse_image_url` ist optional.

Das Bot-Profilbild wird separat im Discord Developer Portal als Avatar gesetzt. `guild_id` ist die Server-ID und sorgt für schnellere Slash-Command-Synchronisierung.

**Wichtig:** Den Bot-Token niemals veröffentlichen. `config.json` und `poisonpulse.db` gehören nicht ins Repository.

## Befehle
- Admin: `/setup_panels`, `/config_set`, `/channel_create`, `/channel_delete`, `/channel_rename`, `/role_create`, `/role_delete`, `/role_add`, `/role_remove`
- Moderation: `/warn`, `/timeout`, `/kick`, `/ban`, `/unban`, `/clear`, `/warns`, `/unwarn`
- Giveaways: `/giveaway`, `/greroll`

Dauern werden als `30m`, `2h`, `1d` oder `1w` angegeben. Warnungen, Tickets, Giveaways und Teilnahmen werden in SQLite gespeichert.

## Rechte
- Support-Tickets: Ticket-Perms und Admins.
- Team-Tickets: Ticket-Perms und Admins; Annahme/Ablehnung ebenfalls nur diese Rollen.
- Clan-Tickets: Clan-Mods, Ticket-Perms und Admins sehen/claimen/schließen. Clan-Bewerbungen annehmen/ablehnen darf ausschließlich Clan-Mod.
- Moderation: Mod-Perms und Admins.
- Team-/Clan-Zielrollen müssen in der Discord-Rollenhierarchie unter der Bot-Rolle liegen, damit der Bot sie vergeben kann.

Der Bot muss vor dem Livebetrieb auf dem Zielserver mit Testkonten für jede Berechtigungsgruppe getestet werden. Discord-Serverrechte und Rollen-Hierarchie können fehlende Bot-Rechte nicht umgehen.
