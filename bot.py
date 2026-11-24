import asyncio
import io
import json
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

CONFIG_FILE = Path("config.json")
if not CONFIG_FILE.exists():
    raise SystemExit("config.json fehlt. config.example.json kopieren und ausfüllen.")

def load_config():
    with CONFIG_FILE.open(encoding="utf-8") as file:
        return json.load(file)

CONFIG = load_config()
TOKEN = CONFIG.get("token", "")
GREEN = discord.Color.from_rgb(46, 204, 113)
INVITE_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg/|discord(?:app)?\.com/invite/)[A-Za-z0-9-]+", re.I)
DB = sqlite3.connect("poisonpulse.db")
DB.row_factory = sqlite3.Row
DB.executescript("""
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    claimed_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS giveaways (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    prize TEXT NOT NULL,
    winner_count INTEGER NOT NULL,
    ends_at TEXT NOT NULL,
    ended INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
""")
DB.commit()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def config_id(key):
    try:
        return int(CONFIG.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0

def has_role(member, key):
    rid = config_id(key)
    return bool(rid and any(role.id == rid for role in getattr(member, "roles", [])))

def has_any(member, *keys):
    return any(has_role(member, key) for key in keys)

def is_admin(member):
    return has_role(member, "adminperms_role_id")

def is_mod(member):
    return has_any(member, "modperms_role_id", "adminperms_role_id")

def no_permission():
    return "Du hast dafür keine Berechtigung."

async def deny(interaction):
    if interaction.response.is_done():
        await interaction.followup.send(no_permission(), ephemeral=True)
    else:
        await interaction.response.send_message(no_permission(), ephemeral=True)

def make_embed(title, description="", branding=False):
    result = discord.Embed(title=title, description=description, color=GREEN, timestamp=datetime.now(timezone.utc))
    if branding:
        emoji_id = config_id("poisonpulse_emoji_id")
        emoji = bot.get_emoji(emoji_id) if emoji_id else None
        if emoji:
            result.title = f"{emoji} {title}"
        image_url = CONFIG.get("poisonpulse_image_url", "")
        if image_url:
            result.set_thumbnail(url=image_url)
    return result

async def write_log(message=None, file=None):
    channel = bot.get_channel(config_id("log_channel_id"))
    if channel:
        await channel.send(content=message, file=file)

def parse_duration(value):
    match = re.fullmatch(r"(\d+)([smhdw])", value.strip().lower())
    if not match:
        raise ValueError("Ungültige Dauer. Beispiele: 30m, 2h, 1d, 1w.")
    amount = int(match.group(1))
    unit = match.group(2)
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return timedelta(**{units[unit]: amount})

def ticket_staff_keys(kind):
    if kind == "clan":
        return ("clanmodperms_role_id", "ticketperms_role_id", "adminperms_role_id")
    if kind == "team":
        return ("ticketperms_role_id", "adminperms_role_id")
    return ("ticketperms_role_id", "adminperms_role_id")

def can_manage_ticket(member, kind):
    return has_any(member, *ticket_staff_keys(kind))

def ticket_overwrites(guild, user, kind):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
    }
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
    for key in ticket_staff_keys(kind):
        role_id = config_id(key)
        role = guild.get_role(role_id) if role_id else None
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    return overwrites

async def create_ticket(guild, user, kind, category_key, details):
    category = guild.get_channel(config_id(category_key))
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Die konfigurierte Ticket-Kategorie wurde nicht gefunden.")
    existing = DB.execute("SELECT channel_id FROM tickets WHERE owner_id=? AND kind=?", (user.id, kind)).fetchall()
    for row in existing:
        if guild.get_channel(row["channel_id"]):
            raise RuntimeError("Du hast bereits ein offenes Ticket dieser Art.")
        DB.execute("DELETE FROM tickets WHERE channel_id=?", (row["channel_id"],))
    DB.commit()
    channel = await guild.create_text_channel(
        name=f"{kind}-{user.id}",
        category=category,
        overwrites=ticket_overwrites(guild, user, kind),
        reason=f"{kind} ticket opened by {user.id}",
    )
    DB.execute(
        "INSERT INTO tickets(channel_id,owner_id,kind,created_at) VALUES(?,?,?,?)",
        (channel.id, user.id, kind, datetime.now(timezone.utc).isoformat()),
    )
    DB.commit()
    details_embed = make_embed(f"{kind.title()} – Ticket", details, branding=kind in ("support", "team", "clan"))
    await channel.send(content=user.mention, embed=details_embed, view=TicketView(kind))
    await write_log(f"Ticket erstellt | Typ: {kind} | Channel-ID: {channel.id} | User-ID: {user.id}")
    return channel

async def close_ticket(channel, actor):
    row = DB.execute("SELECT owner_id,kind FROM tickets WHERE channel_id=?", (channel.id,)).fetchone()
    if not row:
        return False
    lines = [f"Transcript: {channel.name}", f"Ticket-Ersteller-ID: {row['owner_id']}", f"Ticket-Typ: {row['kind']}", f"Geschlossen durch: {actor} ({actor.id})"]
    async for message in channel.history(limit=None, oldest_first=True):
        text = message.content or ""
        if message.attachments:
            text += " | Anhänge: " + ", ".join(item.url for item in message.attachments)
        if message.embeds:
            text += " | Embeds: " + "; ".join(item.title or "Embed" for item in message.embeds)
        lines.append(f"[{message.created_at.isoformat()}] {message.author} ({message.author.id}): {text}")
    transcript = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"transcript-{channel.id}.txt")
    await write_log(f"Ticket geschlossen | Channel-ID: {channel.id} | Typ: {row['kind']} | User-ID: {row['owner_id']} | Mod-ID: {actor.id}", file=transcript)
    DB.execute("DELETE FROM tickets WHERE channel_id=?", (channel.id,))
    DB.commit()
    await channel.delete(reason=f"Ticket closed by {actor.id}")
    return True

class TicketView(discord.ui.View):
    def __init__(self, kind):
        super().__init__(timeout=None)
        claim = discord.ui.Button(label="Claimen", style=discord.ButtonStyle.success, custom_id=f"pp:ticket:{kind}:claim")
        close = discord.ui.Button(label="Schließen", style=discord.ButtonStyle.danger, custom_id=f"pp:ticket:{kind}:close")
        claim.callback = self.claim_callback
        close.callback = self.close_callback
        self.add_item(claim)
        self.add_item(close)
        self.kind = kind

    async def claim_callback(self, interaction):
        if not can_manage_ticket(interaction.user, self.kind):
            return await deny(interaction)
        DB.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (interaction.user.id, interaction.channel_id))
        DB.commit()
        await interaction.response.send_message(f"Ticket übernommen von {interaction.user.mention}.")
        await write_log(f"Ticket geclaimt | Channel-ID: {interaction.channel_id} | Mod-ID: {interaction.user.id}")

    async def close_callback(self, interaction):
        if not can_manage_ticket(interaction.user, self.kind):
            return await deny(interaction)
        await interaction.response.send_message("Ticket wird geschlossen.")
        await close_ticket(interaction.channel, interaction.user)

class SupportPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        button = discord.ui.Button(label="Ticket erstellen", style=discord.ButtonStyle.success, custom_id="pp:panel:support")
        button.callback = self.open_ticket
        self.add_item(button)

    async def open_ticket(self, interaction):
        options = [discord.SelectOption(label=name) for name in ("Support", "Bug", "Frage", "Sonstiges")]
        select = discord.ui.Select(placeholder="Thema auswählen", options=options, min_values=1, max_values=1)
        async def selected(select_interaction):
            try:
                await select_interaction.response.defer(ephemeral=True)
                channel = await create_ticket(select_interaction.guild, select_interaction.user, "support", "support_ticket_category_id", f"Anliegen: {select.values[0]}")
                await select_interaction.followup.send(f"Dein Ticket: {channel.mention}", ephemeral=True)
            except (RuntimeError, discord.Forbidden) as error:
                await select_interaction.followup.send(str(error), ephemeral=True)
        select.callback = selected
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message("Wähle dein Anliegen.", view=view, ephemeral=True)

class ApplicationPanel(discord.ui.View):
    def __init__(self, kind):
        super().__init__(timeout=None)
        self.kind = kind
        button = discord.ui.Button(label="Bewerben", style=discord.ButtonStyle.success, custom_id=f"pp:panel:apply:{kind}")
        button.callback = self.open_modal
        self.add_item(button)

    async def open_modal(self, interaction):
        await interaction.response.send_modal(ApplicationModal(self.kind))

class ApplicationModal(discord.ui.Modal):
    def __init__(self, kind):
        super().__init__(title="Team-Bewerbung" if kind == "team" else "Clan-Bewerbung", timeout=300)
        self.kind = kind
        labels = ["Minecraft-Name", "Alter", "Erfahrung", "Warum Team?", "Wie viel Zeit?"] if kind == "team" else ["Minecraft-Name", "Alter", "Playtime auf HugoSMP.net", "Warum Clan?"]
        self.fields = []
        for label in labels:
            field = discord.ui.TextInput(label=label, max_length=500, style=discord.TextStyle.paragraph)
            self.fields.append(field)
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        details = "\n".join(f"**{field.label}:** {field.value}" for field in self.fields)
        category_key = "team_ticket_category_id" if self.kind == "team" else "clan_ticket_category_id"
        try:
            channel = await create_ticket(interaction.guild, interaction.user, self.kind, category_key, details)
            await channel.send(view=DecisionView(self.kind))
            await interaction.followup.send(f"Deine Bewerbung wurde erstellt: {channel.mention}", ephemeral=True)
        except (RuntimeError, discord.Forbidden) as error:
            await interaction.followup.send(str(error), ephemeral=True)

class DecisionView(discord.ui.View):
    def __init__(self, kind):
        super().__init__(timeout=None)
        self.kind = kind
        accept = discord.ui.Button(label="Annehmen", style=discord.ButtonStyle.success, custom_id=f"pp:decision:{kind}:accept")
        reject = discord.ui.Button(label="Ablehnen", style=discord.ButtonStyle.danger, custom_id=f"pp:decision:{kind}:reject")
        accept.callback = self.accept
        reject.callback = self.reject
        self.add_item(accept)
        self.add_item(reject)

    def authorized(self, member):
        if self.kind == "clan":
            return has_role(member, "clanmodperms_role_id")
        return has_any(member, "adminperms_role_id", "ticketperms_role_id")

    async def accept(self, interaction):
        if not self.authorized(interaction.user):
            return await deny(interaction)
        row = DB.execute("SELECT owner_id FROM tickets WHERE channel_id=?", (interaction.channel_id,)).fetchone()
        if not row:
            return await interaction.response.send_message("Bewerbung nicht gefunden.", ephemeral=True)
        member = interaction.guild.get_member(row["owner_id"])
        role_key = "team_role_id" if self.kind == "team" else "clan_mitglied_role_id"
        role_id = config_id(role_key)
        target_role = interaction.guild.get_role(role_id) if role_id else None
        if not member or not target_role:
            return await interaction.response.send_message("Mitglied oder Zielrolle wurde nicht gefunden. Konfiguration prüfen.", ephemeral=True)
        await member.add_roles(target_role, reason=f"{self.kind} application accepted by {interaction.user.id}")
        await interaction.response.send_message("Bewerbung angenommen. Rolle wurde vergeben; Ticket wird geschlossen.")
        await write_log(f"{self.kind.title()}-Bewerbung angenommen | User-ID: {member.id} | Mod-ID: {interaction.user.id}")
        await close_ticket(interaction.channel, interaction.user)

    async def reject(self, interaction):
        if not self.authorized(interaction.user):
            return await deny(interaction)
        await interaction.response.send_modal(RejectModal(self.kind))

class RejectModal(discord.ui.Modal):
    def __init__(self, kind):
        super().__init__(title="Ablehnungsgrund", timeout=300)
        self.kind = kind
        self.reason = discord.ui.TextInput(label="Grund", max_length=1000, style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, interaction):
        row = DB.execute("SELECT owner_id FROM tickets WHERE channel_id=?", (interaction.channel_id,)).fetchone()
        if not row:
            return await interaction.response.send_message("Bewerbung nicht gefunden.", ephemeral=True)
        member = interaction.guild.get_member(row["owner_id"])
        if member:
            try:
                await member.send(f"Deine {self.kind}-Bewerbung wurde abgelehnt. Grund: {self.reason.value}")
            except discord.Forbidden:
                pass
        await interaction.response.send_message("Bewerbung abgelehnt. Ticket wird geschlossen.")
        await write_log(f"{self.kind.title()}-Bewerbung abgelehnt | User-ID: {row['owner_id']} | Mod-ID: {interaction.user.id} | Grund: {self.reason.value}")
        await close_ticket(interaction.channel, interaction.user)

class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        button = discord.ui.Button(label="Teilnehmen", style=discord.ButtonStyle.success, custom_id="pp:giveaway:enter")
        button.callback = self.enter
        self.add_item(button)

    async def enter(self, interaction):
        row = DB.execute("SELECT ended FROM giveaways WHERE message_id=?", (interaction.message.id,)).fetchone()
        if not row or row["ended"]:
            return await interaction.response.send_message("Dieses Giveaway ist beendet.", ephemeral=True)
        try:
            DB.execute("INSERT INTO giveaway_entries(message_id,user_id) VALUES(?,?)", (interaction.message.id, interaction.user.id))
            DB.commit()
            await interaction.response.send_message("Du nimmst am Giveaway teil.", ephemeral=True)
            await write_log(f"Giveaway-Teilnahme | Giveaway-ID: {interaction.message.id} | User-ID: {interaction.user.id}")
        except sqlite3.IntegrityError:
            await interaction.response.send_message("Du nimmst bereits teil.", ephemeral=True)

@tasks.loop(seconds=20)
async def giveaway_watcher():
    now = datetime.now(timezone.utc).isoformat()
    due = DB.execute("SELECT * FROM giveaways WHERE ended=0 AND ends_at<=?", (now,)).fetchall()
    for giveaway in due:
        entries = [row["user_id"] for row in DB.execute("SELECT user_id FROM giveaway_entries WHERE message_id=?", (giveaway["message_id"],)).fetchall()]
        winners = random.sample(entries, min(len(entries), giveaway["winner_count"])) if entries else []
        channel = bot.get_channel(giveaway["channel_id"])
        if channel:
            if winners:
                await channel.send(f"Giveaway beendet: **{giveaway['prize']}**\nGewinner: " + ", ".join(f"<@{uid}>" for uid in winners))
            else:
                await channel.send(f"Giveaway beendet: **{giveaway['prize']}**. Es gab keine Teilnehmer.")
        DB.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (giveaway["message_id"],))
        DB.commit()
        await write_log(f"Giveaway beendet | Preis: {giveaway['prize']} | Gewinner-IDs: {', '.join(map(str, winners)) or 'Keine'}")

@bot.event
async def on_ready():
    for view in (SupportPanel(), ApplicationPanel("team"), ApplicationPanel("clan"), TicketView("support"), TicketView("team"), TicketView("clan"), DecisionView("team"), DecisionView("clan"), GiveawayView()):
        bot.add_view(view)
    if not giveaway_watcher.is_running():
        giveaway_watcher.start()
    guild_id = config_id("guild_id")
    if guild_id:
        guild_ref = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild_ref)
        await bot.tree.sync(guild=guild_ref)
    else:
        await bot.tree.sync()
    print(f"Angemeldet als {bot.user}")

@bot.event
async def on_member_join(member):
    channel = bot.get_channel(config_id("welcome_channel_id"))
    if channel:
        await channel.send(embed=make_embed("Willkommen bei PoisonPulse", f"Willkommen {member.mention} auf **HugoSMP.net**! {CONFIG.get('server_info_text', 'Schön, dass du dabei bist.')} Bei Fragen kannst du dich an den Support wenden.", True))
    await write_log(f"User beigetreten | User: {member} | ID: {member.id}")

@bot.event
async def on_member_remove(member):
    await write_log(f"User verlassen | User: {member} | ID: {member.id}")

@bot.event
async def on_message(message):
    if message.guild and not message.author.bot and INVITE_PATTERN.search(message.content):
        if not has_any(message.author, "modperms_role_id", "adminperms_role_id"):
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            try:
                await message.author.send("Deine Nachricht wurde entfernt, da Discord-Einladungslinks auf diesem Server nicht erlaubt sind. Dies zählt als Verwarnung.")
            except discord.Forbidden:
                pass
            DB.execute(
                "INSERT INTO warnings(user_id,moderator_id,reason,created_at) VALUES(?,?,?,?)",
                (message.author.id, bot.user.id, "Discord-Invite-Link", datetime.now(timezone.utc).isoformat()),
            )
            DB.commit()
            await write_log(f"Discord-Invite entfernt | User-ID: {message.author.id} | Channel-ID: {message.channel.id}")
    await bot.process_commands(message)

@bot.event
async def on_app_command_completion(interaction, command):
    await write_log(f"Slash-Command ausgeführt | Command: /{command.qualified_name} | User-ID: {interaction.user.id} | Channel-ID: {interaction.channel_id}")

@bot.tree.command(name="setup_panels", description="Veröffentlicht Support-, Team- und Clan-Panels")
async def setup_panels(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    panels = [
        ("welcome_channel_id", "Support", "Erstelle ein Ticket für Support, Bug, Frage oder Sonstiges.", SupportPanel()),
        ("team_bewerbung_channel_id", "Team-Bewerbung", "Bewirb dich für das Team. Fülle das Formular vollständig aus.", ApplicationPanel("team")),
        ("clan_bewerbung_channel_id", "Clan-Bewerbung", f"Eintrittsgeld: {CONFIG.get('clan_entry_fee', 'Bitte beim Team erfragen')}\nVoraussetzungen: {CONFIG.get('clan_requirements', 'Bitte beim Team erfragen')}\n\nBewirb dich über den Button.", ApplicationPanel("clan")),
    ]
    posted = []
    for key, title, description, view in panels:
        channel = bot.get_channel(config_id(key))
        if channel:
            await channel.send(embed=make_embed(title, description, True), view=view)
            posted.append(str(channel.id))
    await interaction.followup.send("Panels veröffentlicht.", ephemeral=True)
    await write_log(f"Panels veröffentlicht | Admin-ID: {interaction.user.id} | Channel-IDs: {', '.join(posted)}")

@bot.tree.command(name="giveaway", description="Startet ein Giveaway")
@app_commands.describe(preis="Gewinn", dauer="Dauer, z. B. 30m, 2h, 1d", winner="Anzahl der Gewinner")
async def giveaway(interaction: discord.Interaction, preis: str, dauer: str, winner: app_commands.Range[int, 1, 20]):
    if not is_mod(interaction.user):
        return await deny(interaction)
    try:
        end = datetime.now(timezone.utc) + parse_duration(dauer)
    except ValueError as error:
        return await interaction.response.send_message(str(error), ephemeral=True)
    channel = bot.get_channel(config_id("giveaway_channel_id"))
    if not channel:
        return await interaction.response.send_message("Giveaway-Channel ist nicht konfiguriert.", ephemeral=True)
    description = f"**Preis:** {preis}\n**Dauer:** {dauer}\n**Gewinner:** {winner}\n**Ende:** <t:{int(end.timestamp())}:R>"
    await interaction.response.defer(ephemeral=True)
    message = await channel.send(embed=make_embed("Giveaway", description, True), view=GiveawayView())
    DB.execute("INSERT INTO giveaways(message_id,channel_id,host_id,prize,winner_count,ends_at) VALUES(?,?,?,?,?,?)", (message.id, channel.id, interaction.user.id, preis, winner, end.isoformat()))
    DB.commit()
    await interaction.followup.send("Giveaway gestartet.", ephemeral=True)
    await write_log(f"Giveaway gestartet | Preis: {preis} | Dauer: {dauer} | Gewinner: {winner} | Mod-ID: {interaction.user.id}")

@bot.tree.command(name="greroll", description="Zieht einen neuen Gewinner für ein beendetes Giveaway")
@app_commands.describe(message_id="Nachrichten-ID des Giveaways")
async def greroll(interaction: discord.Interaction, message_id: str):
    if not is_mod(interaction.user):
        return await deny(interaction)
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Ungültige Nachrichten-ID.", ephemeral=True)
    row = DB.execute("SELECT * FROM giveaways WHERE message_id=?", (mid,)).fetchone()
    if not row or not row["ended"]:
        return await interaction.response.send_message("Giveaway nicht gefunden oder noch nicht beendet.", ephemeral=True)
    entries = [item["user_id"] for item in DB.execute("SELECT user_id FROM giveaway_entries WHERE message_id=?", (mid,)).fetchall()]
    if not entries:
        return await interaction.response.send_message("Keine Teilnehmer vorhanden.", ephemeral=True)
    winners = random.sample(entries, min(row["winner_count"], len(entries)))
    await interaction.response.send_message("Neue Gewinner: " + ", ".join(f"<@{uid}>" for uid in winners))
    await write_log(f"Giveaway-Reroll | Preis: {row['prize']} | Gewinner-IDs: {winners} | Mod-ID: {interaction.user.id}")

@bot.tree.command(name="warn", description="Verwarnt einen User")
async def warn(interaction: discord.Interaction, user: discord.Member, grund: str):
    if not is_mod(interaction.user): return await deny(interaction)
    DB.execute("INSERT INTO warnings(user_id,moderator_id,reason,created_at) VALUES(?,?,?,?)", (user.id, interaction.user.id, grund, datetime.now(timezone.utc).isoformat()))
    DB.commit()
    await interaction.response.send_message(f"{user.mention} wurde verwarnt. Grund: {grund}")
    await write_log(f"Warn | User-ID: {user.id} | Mod-ID: {interaction.user.id} | Grund: {grund} | Zeit: {datetime.now(timezone.utc).isoformat()}")

@bot.tree.command(name="timeout", description="Gibt einem User einen Timeout")
async def timeout(interaction: discord.Interaction, user: discord.Member, dauer: str, grund: str):
    if not is_mod(interaction.user): return await deny(interaction)
    try: delta = parse_duration(dauer)
    except ValueError as error: return await interaction.response.send_message(str(error), ephemeral=True)
    if delta > timedelta(days=28): return await interaction.response.send_message("Ein Timeout darf maximal 28 Tage dauern.", ephemeral=True)
    await user.timeout(delta, reason=grund)
    await interaction.response.send_message(f"{user.mention} erhielt einen Timeout für {dauer}.")
    await write_log(f"Timeout | User-ID: {user.id} | Mod-ID: {interaction.user.id} | Grund: {grund} | Dauer: {dauer} | Zeit: {datetime.now(timezone.utc).isoformat()}")

@bot.tree.command(name="kick", description="Kickt einen User")
async def kick(interaction: discord.Interaction, user: discord.Member, grund: str):
    if not is_mod(interaction.user): return await deny(interaction)
    await user.kick(reason=grund)
    await interaction.response.send_message(f"{user} wurde gekickt.")
    await write_log(f"Kick | User-ID: {user.id} | Mod-ID: {interaction.user.id} | Grund: {grund} | Zeit: {datetime.now(timezone.utc).isoformat()}")

@bot.tree.command(name="ban", description="Bannt einen User")
async def ban(interaction: discord.Interaction, user: discord.Member, grund: str):
    if not is_mod(interaction.user): return await deny(interaction)
    await user.ban(reason=grund)
    await interaction.response.send_message(f"{user} wurde gebannt.")
    await write_log(f"Ban | User-ID: {user.id} | Mod-ID: {interaction.user.id} | Grund: {grund} | Zeit: {datetime.now(timezone.utc).isoformat()}")

@bot.tree.command(name="unban", description="Entbannt einen User anhand seiner ID")
async def unban(interaction: discord.Interaction, user_id: str, grund: str):
    if not is_mod(interaction.user): return await deny(interaction)
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user, reason=grund)
    except (ValueError, discord.NotFound):
        return await interaction.response.send_message("Gebannter User nicht gefunden.", ephemeral=True)
    await interaction.response.send_message(f"{user} wurde entbannt.")
    await write_log(f"Unban | User-ID: {user.id} | Mod-ID: {interaction.user.id} | Grund: {grund} | Zeit: {datetime.now(timezone.utc).isoformat()}")

@bot.tree.command(name="clear", description="Löscht Nachrichten")
async def clear(interaction: discord.Interaction, anzahl: app_commands.Range[int, 1, 100]):
    if not is_mod(interaction.user): return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=anzahl)
    await interaction.followup.send(f"{len(deleted)} Nachrichten gelöscht.", ephemeral=True)
    await write_log(f"Clear | Anzahl: {len(deleted)} | Mod-ID: {interaction.user.id} | Channel-ID: {interaction.channel_id} | Zeit: {datetime.now(timezone.utc).isoformat()}")

@bot.tree.command(name="warns", description="Zeigt aktive Verwarnungen eines Users")
async def warns(interaction: discord.Interaction, user: discord.Member):
    if not is_mod(interaction.user): return await deny(interaction)
    rows = DB.execute("SELECT id,reason,created_at,moderator_id FROM warnings WHERE user_id=? AND active=1 ORDER BY id DESC", (user.id,)).fetchall()
    description = "\n".join(f"#{row['id']} – {row['reason']} | Mod-ID: {row['moderator_id']} | {row['created_at']}" for row in rows) or "Keine aktiven Verwarnungen."
    await interaction.response.send_message(embed=make_embed(f"Verwarnungen: {user}", description), ephemeral=True)

@bot.tree.command(name="unwarn", description="Entfernt eine Verwarnung anhand der Case-ID")
async def unwarn(interaction: discord.Interaction, case_id: int, grund: str):
    if not is_mod(interaction.user): return await deny(interaction)
    row = DB.execute("SELECT user_id FROM warnings WHERE id=? AND active=1", (case_id,)).fetchone()
    if not row: return await interaction.response.send_message("Aktiver Warn-Case nicht gefunden.", ephemeral=True)
    DB.execute("UPDATE warnings SET active=0 WHERE id=?", (case_id,))
    DB.commit()
    await interaction.response.send_message(f"Verwarnung #{case_id} wurde entfernt.")
    await write_log(f"Unwarn | Case-ID: {case_id} | User-ID: {row['user_id']} | Mod-ID: {interaction.user.id} | Grund: {grund}")

@bot.tree.command(name="config_set", description="Ändert einen Eintrag in der Bot-Konfiguration")
@app_commands.describe(key="Config-Schlüssel", value="Neue ID oder Wert")
async def config_set(interaction: discord.Interaction, key: str, value: str):
    if not is_admin(interaction.user): return await deny(interaction)
    if key not in CONFIG or key == "token":
        return await interaction.response.send_message("Dieser Config-Schlüssel kann hier nicht geändert werden.", ephemeral=True)
    id_keys = {name for name in CONFIG if name.endswith("_id")}
    if key in id_keys:
        try: CONFIG[key] = int(value)
        except ValueError: return await interaction.response.send_message("Für diesen Eintrag muss eine numerische ID angegeben werden.", ephemeral=True)
    else:
        CONFIG[key] = value
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_FILE)
    await interaction.response.send_message(f"Konfiguration aktualisiert: {key}.", ephemeral=True)
    await write_log(f"Config geändert | Schlüssel: {key} | Admin-ID: {interaction.user.id}")

@bot.tree.command(name="channel_create", description="Erstellt einen Text-Channel")
async def channel_create(interaction: discord.Interaction, name: str, category_id: str = ""):
    if not is_admin(interaction.user): return await deny(interaction)
    category = None
    if category_id:
        try: category = interaction.guild.get_channel(int(category_id))
        except ValueError: return await interaction.response.send_message("Ungültige Kategorie-ID.", ephemeral=True)
        if not isinstance(category, discord.CategoryChannel): return await interaction.response.send_message("Kategorie nicht gefunden.", ephemeral=True)
    channel = await interaction.guild.create_text_channel(name=name, category=category, reason=f"Created by admin {interaction.user.id}")
    await interaction.response.send_message(f"Channel erstellt: {channel.mention}", ephemeral=True)
    await write_log(f"Channel erstellt | Channel-ID: {channel.id} | Admin-ID: {interaction.user.id}")

@bot.tree.command(name="role_create", description="Erstellt eine Rolle")
async def role_create(interaction: discord.Interaction, name: str):
    if not is_admin(interaction.user): return await deny(interaction)
    role = await interaction.guild.create_role(name=name, reason=f"Created by admin {interaction.user.id}")
    await interaction.response.send_message(f"Rolle erstellt: {role.mention}", ephemeral=True)
    await write_log(f"Rolle erstellt | Rollen-ID: {role.id} | Admin-ID: {interaction.user.id}")

@bot.tree.command(name="channel_delete", description="Löscht einen Channel anhand seiner ID")
async def channel_delete(interaction: discord.Interaction, channel_id: str):
    if not is_admin(interaction.user): return await deny(interaction)
    try: target = interaction.guild.get_channel(int(channel_id))
    except ValueError: return await interaction.response.send_message("Ungültige Channel-ID.", ephemeral=True)
    if not target: return await interaction.response.send_message("Channel nicht gefunden.", ephemeral=True)
    deleted_id = target.id
    await target.delete(reason=f"Deleted by admin {interaction.user.id}")
    await interaction.response.send_message(f"Channel {deleted_id} wurde gelöscht.", ephemeral=True)
    await write_log(f"Channel gelöscht | Channel-ID: {deleted_id} | Admin-ID: {interaction.user.id}")

@bot.tree.command(name="channel_rename", description="Benennt einen Channel anhand seiner ID um")
async def channel_rename(interaction: discord.Interaction, channel_id: str, neuer_name: str):
    if not is_admin(interaction.user): return await deny(interaction)
    try: target = interaction.guild.get_channel(int(channel_id))
    except ValueError: return await interaction.response.send_message("Ungültige Channel-ID.", ephemeral=True)
    if not target: return await interaction.response.send_message("Channel nicht gefunden.", ephemeral=True)
    old_name = target.name
    await target.edit(name=neuer_name, reason=f"Renamed by admin {interaction.user.id}")
    await interaction.response.send_message(f"Channel umbenannt: {old_name} zu {neuer_name}.", ephemeral=True)
    await write_log(f"Channel umbenannt | Channel-ID: {target.id} | Admin-ID: {interaction.user.id}")

@bot.tree.command(name="role_delete", description="Löscht eine Rolle anhand ihrer ID")
async def role_delete(interaction: discord.Interaction, role_id: str):
    if not is_admin(interaction.user): return await deny(interaction)
    try: target = interaction.guild.get_role(int(role_id))
    except ValueError: return await interaction.response.send_message("Ungültige Rollen-ID.", ephemeral=True)
    if not target: return await interaction.response.send_message("Rolle nicht gefunden.", ephemeral=True)
    deleted_id = target.id
    await target.delete(reason=f"Deleted by admin {interaction.user.id}")
    await interaction.response.send_message(f"Rolle {deleted_id} wurde gelöscht.", ephemeral=True)
    await write_log(f"Rolle gelöscht | Rollen-ID: {deleted_id} | Admin-ID: {interaction.user.id}")

@bot.tree.command(name="role_add", description="Gibt einem User eine Rolle anhand der Rollen-ID")
async def role_add(interaction: discord.Interaction, user: discord.Member, role_id: str):
    if not is_admin(interaction.user): return await deny(interaction)
    try: target = interaction.guild.get_role(int(role_id))
    except ValueError: return await interaction.response.send_message("Ungültige Rollen-ID.", ephemeral=True)
    if not target: return await interaction.response.send_message("Rolle nicht gefunden.", ephemeral=True)
    await user.add_roles(target, reason=f"Assigned by admin {interaction.user.id}")
    await interaction.response.send_message(f"Rolle {target.id} wurde {user.mention} gegeben.", ephemeral=True)
    await write_log(f"Rolle vergeben | Rollen-ID: {target.id} | User-ID: {user.id} | Admin-ID: {interaction.user.id}")

@bot.tree.command(name="role_remove", description="Entfernt eine Rolle von einem User anhand der Rollen-ID")
async def role_remove(interaction: discord.Interaction, user: discord.Member, role_id: str):
    if not is_admin(interaction.user): return await deny(interaction)
    try: target = interaction.guild.get_role(int(role_id))
    except ValueError: return await interaction.response.send_message("Ungültige Rollen-ID.", ephemeral=True)
    if not target: return await interaction.response.send_message("Rolle nicht gefunden.", ephemeral=True)
    await user.remove_roles(target, reason=f"Removed by admin {interaction.user.id}")
    await interaction.response.send_message(f"Rolle {target.id} wurde {user.mention} entfernt.", ephemeral=True)
    await write_log(f"Rolle entfernt | Rollen-ID: {target.id} | User-ID: {user.id} | Admin-ID: {interaction.user.id}")

if __name__ == "__main__":
    if not TOKEN or TOKEN == "DEIN_DISCORD_BOT_TOKEN":
        raise SystemExit("Bitte den Bot-Token in config.json eintragen.")
    bot.run(TOKEN)
