import json, re, sqlite3, random
from datetime import datetime, timedelta, timezone
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands, tasks

with open("config.json", encoding="utf-8") as f:
    C = json.load(f)
TOKEN = C["token"]
DB = sqlite3.connect("poisonpulse.db")
DB.execute("CREATE TABLE IF NOT EXISTS warns(id INTEGER PRIMARY KEY,user INTEGER,mod INTEGER,reason TEXT,created TEXT,active INTEGER DEFAULT 1)")
DB.execute("CREATE TABLE IF NOT EXISTS entries(message INTEGER,user INTEGER,PRIMARY KEY(message,user))")
DB.execute("CREATE TABLE IF NOT EXISTS giveaways(message INTEGER PRIMARY KEY,channel INTEGER,prize TEXT,winners INTEGER,ends TEXT,ended INTEGER DEFAULT 0)")
DB.execute("CREATE TABLE IF NOT EXISTS tickets(channel INTEGER PRIMARY KEY,owner INTEGER,kind TEXT,claimed INTEGER)")
DB.commit()
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
GREEN = discord.Color.from_rgb(46, 204, 113)
INVITE = re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)[\w-]+", re.I)

def role(member, key):
    rid = int(C.get(key, 0) or 0)
    return bool(rid and any(r.id == rid for r in getattr(member, "roles", [])))
def permitted(member, *keys):
    return any(role(member, k) for k in keys)
async def deny(i):
    await i.response.send_message("Du hast dafür keine Berechtigung.", ephemeral=True)
def branded(title, description):
    e = discord.Embed(title=title, description=description, color=GREEN)
    url = C.get("poisonpulse_image_url")
    if url: e.set_thumbnail(url=url)
    eid = int(C.get("poisonpulse_emoji_id", 0) or 0)
    emoji = bot.get_emoji(eid) if eid else None
    if emoji: e.title = str(emoji) + " " + title
    return e
async def log(text):
    ch = bot.get_channel(int(C.get("log_channel_id", 0) or 0))
    if ch: await ch.send(text[:1900])
def duration(s):
    m = re.fullmatch(r"(\d+)([smhdw])", s.lower())
    if not m: raise ValueError("Dauer z. B. 30m, 2h, 1d oder 1w.")
    return timedelta(**{"s":"seconds","m":"minutes","h":"hours","d":"days","w":"weeks"}[m[2]] and {"seconds":int(m[1])} if m[2]=="s" else {"minutes":int(m[1])} if m[2]=="m" else {"hours":int(m[1])} if m[2]=="h" else {"days":int(m[1])} if m[2]=="d" else {"weeks":int(m[1])})

async def create_ticket(guild, user, kind, category_key, details):
    cat = guild.get_channel(int(C.get(category_key, 0) or 0))
    if not isinstance(cat, discord.CategoryChannel): raise RuntimeError("Ticket-Kategorie nicht konfiguriert.")
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True), guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True)}
    keys = ["ticketperms_role_id", "adminperms_role_id"] if kind == "team" else ["ticketperms_role_id", "adminperms_role_id", "clanmodperms_role_id"]
    for key in keys:
        rid = int(C.get(key, 0) or 0); r = guild.get_role(rid) if rid else None
        if r: overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if kind == "clan":
        rid = int(C.get("clanmodperms_role_id", 0) or 0); r = guild.get_role(rid) if rid else None
        if r: overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    ch = await guild.create_text_channel(f"{kind}-{user.name[:18]}", category=cat, overwrites=overwrites)
    DB.execute("INSERT OR REPLACE INTO tickets VALUES(?,?,?,NULL)", (ch.id,user.id,kind)); DB.commit()
    await ch.send(user.mention, embed=branded(f"{kind.title()} Bewerbung / Ticket", details), view=TicketView(kind))
    await log(f"Ticket erstellt: {ch.id} | {kind} | User-ID: {user.id}")
    return ch

async def close_ticket(ch, actor):
    row = DB.execute("SELECT owner,kind FROM tickets WHERE channel=?", (ch.id,)).fetchone()
    if not row: return
    lines = [f"Transcript {ch.name} | Owner-ID {row[0]} | geschlossen von {actor}"]
    async for m in ch.history(limit=None, oldest_first=True):
        lines.append(f"[{m.created_at.isoformat()}] {m.author} ({m.author.id}): {m.content}")
    import io
    dest = bot.get_channel(int(C.get("log_channel_id",0) or 0))
    if dest: await dest.send(f"Transcript für {ch.name}", file=discord.File(io.BytesIO("\n".join(lines).encode()), filename=f"{ch.id}.txt"))
    DB.execute("DELETE FROM tickets WHERE channel=?", (ch.id,)); DB.commit()
    await ch.delete(reason=f"Geschlossen von {actor}")

class TicketView(discord.ui.View):
    def __init__(self, kind="support"):
        super().__init__(timeout=None); self.kind=kind
    @discord.ui.button(label="Claimen", style=discord.ButtonStyle.success, custom_id="pp:claim")
    async def claim(self,i,b):
        if not permitted(i.user,"ticketperms_role_id","clanmodperms_role_id","adminperms_role_id"): return await deny(i)
        DB.execute("UPDATE tickets SET claimed=? WHERE channel=?",(i.user.id,i.channel_id)); DB.commit()
        await i.response.send_message(f"Ticket übernommen von {i.user.mention}."); await log(f"Ticket geclaimt {i.channel_id} von {i.user.id}")
    @discord.ui.button(label="Schließen", style=discord.ButtonStyle.danger, custom_id="pp:close")
    async def close(self,i,b):
        if not permitted(i.user,"ticketperms_role_id","clanmodperms_role_id","adminperms_role_id"): return await deny(i)
        await i.response.send_message("Ticket wird geschlossen."); await close_ticket(i.channel,i.user)

class SupportView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Ticket erstellen",style=discord.ButtonStyle.success,custom_id="pp:support")
    async def open(self,i,b):
        class Choice(discord.ui.Select):
            def __init__(self):
                super().__init__(placeholder="Thema auswählen",options=[discord.SelectOption(label=x) for x in ["Support","Bug","Frage","Sonstiges"]])
            async def callback(self,si):
                await si.response.defer(ephemeral=True)
                ch=await create_ticket(si.guild,si.user,"support","support_ticket_category_id","Thema: "+self.values[0])
                await si.followup.send(f"Ticket: {ch.mention}",ephemeral=True)
        v=discord.ui.View(timeout=120); v.add_item(Choice())
        await i.response.send_message("Wähle dein Anliegen.",view=v,ephemeral=True)

class ApplyPanel(discord.ui.View):
    def __init__(self,kind): super().__init__(timeout=None); self.kind=kind
    @discord.ui.button(label="Bewerben",style=discord.ButtonStyle.success,custom_id="pp:apply")
    async def apply(self,i,b): await i.response.send_modal(ApplyModal(self.kind))
class ApplyModal(discord.ui.Modal):
    def __init__(self,kind):
        super().__init__(title="Team-Bewerbung" if kind=="team" else "Clan-Bewerbung"); self.kind=kind
        labels=["Minecraft-Name","Alter","Erfahrung","Warum Team?","Wie viel Zeit?"] if kind=="team" else ["Minecraft-Name","Alter","Playtime auf HugoSMP.net","Warum Clan?"]
        self.fields=[]
        for label in labels:
            x=discord.ui.TextInput(label=label,style=discord.TextStyle.paragraph,max_length=500); self.fields.append(x); self.add_item(x)
    async def on_submit(self,i):
        await i.response.defer(ephemeral=True)
        details="\n".join(f"**{x.label}:** {x.value}" for x in self.fields)
        key="team_ticket_category_id" if self.kind=="team" else "clan_ticket_category_id"
        ch=await create_ticket(i.guild,i.user,self.kind,key,details)
        await ch.send(view=DecisionView(self.kind))
        await i.followup.send(f"Bewerbung erstellt: {ch.mention}",ephemeral=True)

class DecisionView(discord.ui.View):
    def __init__(self,kind): super().__init__(timeout=None); self.kind=kind
    def ok(self,u): return permitted(u,"clanmodperms_role_id","adminperms_role_id") if self.kind=="clan" else permitted(u,"adminperms_role_id","ticketperms_role_id")
    @discord.ui.button(label="Annehmen",style=discord.ButtonStyle.success,custom_id="pp:accept")
    async def accept(self,i,b):
        if not self.ok(i.user): return await deny(i)
        row=DB.execute("SELECT owner FROM tickets WHERE channel=?",(i.channel_id,)).fetchone()
        if not row: return await i.response.send_message("Bewerbung nicht gefunden.",ephemeral=True)
        member=i.guild.get_member(row[0]); key="team_role_id" if self.kind=="team" else "clan_mitglied_role_id"
        r=i.guild.get_role(int(C.get(key,0) or 0))
        if member and r: await member.add_roles(r,reason="Bewerbung angenommen")
        await i.response.send_message("Bewerbung angenommen. Ticket wird geschlossen.")
        await log(f"{self.kind} angenommen | User-ID {row[0]} | Mod-ID {i.user.id}")
        await close_ticket(i.channel,i.user)
    @discord.ui.button(label="Ablehnen",style=discord.ButtonStyle.danger,custom_id="pp:reject")
    async def reject(self,i,b):
        if not self.ok(i.user): return await deny(i)
        await i.response.send_modal(RejectModal(self.kind))
class RejectModal(discord.ui.Modal):
    def __init__(self,kind):
        super().__init__(title="Ablehnungsgrund"); self.kind=kind
        self.reason=discord.ui.TextInput(label="Grund",style=discord.TextStyle.paragraph,max_length=1000); self.add_item(self.reason)
    async def on_submit(self,i):
        row=DB.execute("SELECT owner FROM tickets WHERE channel=?",(i.channel_id,)).fetchone()
        if not row: return await i.response.send_message("Bewerbung nicht gefunden.",ephemeral=True)
        member=i.guild.get_member(row[0])
        if member:
            try: await member.send(f"Deine {self.kind}-Bewerbung wurde abgelehnt. Grund: {self.reason.value}")
            except discord.Forbidden: pass
        await i.response.send_message("Bewerbung abgelehnt. Ticket wird geschlossen.")
        await log(f"{self.kind} abgelehnt | User-ID {row[0]} | Mod-ID {i.user.id} | Grund: {self.reason.value}")
        await close_ticket(i.channel,i.user)

class GiveawayView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Teilnehmen",style=discord.ButtonStyle.success,custom_id="pp:giveaway:join")
    async def join(self,i,b):
        row=DB.execute("SELECT ended FROM giveaways WHERE message=?",(i.message.id,)).fetchone()
        if not row or row[0]: return await i.response.send_message("Giveaway beendet.",ephemeral=True)
        try: DB.execute("INSERT INTO entries VALUES(?,?)",(i.message.id,i.user.id)); DB.commit(); await i.response.send_message("Du nimmst teil.",ephemeral=True)
        except sqlite3.IntegrityError: await i.response.send_message("Du nimmst bereits teil.",ephemeral=True)

@tasks.loop(seconds=20)
async def draw():
    now=datetime.now(timezone.utc).isoformat()
    for row in DB.execute("SELECT * FROM giveaways WHERE ended=0 AND ends<=?",(now,)).fetchall():
        users=[x[0] for x in DB.execute("SELECT user FROM entries WHERE message=?",(row[0],))]
        winners=random.sample(users,min(len(users),row[3])) if users else []
        ch=bot.get_channel(row[1])
        if ch: await ch.send(("Gewinner: "+", ".join(f"<@{u}>" for u in winners)) if winners else "Giveaway beendet. Keine Teilnehmer.")
        DB.execute("UPDATE giveaways SET ended=1 WHERE message=?",(row[0],)); DB.commit()
        await log(f"Giveaway beendet: {row[2]} | Gewinner-IDs: {winners}")

def is_mod(i): return permitted(i.user,"modperms_role_id","adminperms_role_id")
async def case(action,user,i,reason):
    await log(f"{action} | User: {user} ({user.id}) | Mod: {i.user} ({i.user.id}) | Grund: {reason} | Zeit: {datetime.now(timezone.utc).isoformat()}")

@bot.event
async def on_ready():
    for v in [TicketView(),SupportView(),ApplyPanel("team"),ApplyPanel("clan"),DecisionView("team"),DecisionView("clan"),GiveawayView()]: bot.add_view(v)
    if not draw.is_running(): draw.start()
    await bot.tree.sync()
    print(f"Bot online: {bot.user}")

@bot.event
async def on_member_join(m):
    ch=bot.get_channel(int(C.get("welcome_channel_id",0) or 0))
    if ch: await ch.send(embed=branded("Willkommen bei PoisonPulse",f"Willkommen {m.mention} auf HugoSMP.net! Schön, dass du dabei bist. Bei Fragen hilft dir unser Support.",))
    await log(f"Beitritt: {m} ({m.id})")
@bot.event
async def on_member_remove(m): await log(f"Verlassen: {m} ({m.id})")
@bot.event
async def on_message(m):
    if m.guild and not m.author.bot and INVITE.search(m.content) and not permitted(m.author,"modperms_role_id","adminperms_role_id"):
        try: await m.delete()
        except discord.Forbidden: pass
        try: await m.author.send("Deine Nachricht wurde entfernt, da Discord-Einladungslinks nicht erlaubt sind. Dies zählt als Verwarnung.")
        except discord.Forbidden: pass
        DB.execute("INSERT INTO warns(user,mod,reason,created) VALUES(?,?,?,?)",(m.author.id,bot.user.id,"Discord-Invite-Link",datetime.now(timezone.utc).isoformat())); DB.commit()
        await log(f"Invite entfernt | User-ID {m.author.id} | Channel-ID {m.channel.id}")
    await bot.process_commands(m)

@bot.tree.command(name="setup_panels",description="Veröffentlicht alle Panels")
async def setup_panels(i):
    if not permitted(i.user,"adminperms_role_id"): return await deny(i)
    await i.response.defer(ephemeral=True)
    ch=bot.get_channel(int(C.get("welcome_channel_id",0) or 0))
    if ch: await ch.send(embed=branded("Support","Erstelle ein Ticket für Support, Bug, Frage oder Sonstiges."),view=SupportView())
    for key,kind,title,desc in [("team_bewerbung_channel_id","team","Team-Bewerbung","Bewirb dich für das Team. Fülle das Formular aus."),("clan_bewerbung_channel_id","clan","Clan-Bewerbung","Bewirb dich für den Clan. Eintrittsgeld und Voraussetzungen erfährst du beim Serverteam.")]:
        dest=bot.get_channel(int(C.get(key,0) or 0))
        if dest: await dest.send(embed=branded(title,desc),view=ApplyPanel(kind))
    await i.followup.send("Panels veröffentlicht.",ephemeral=True); await log(f"Panels eingerichtet von {i.user.id}")

@bot.tree.command(name="giveaway",description="Startet ein Giveaway")
async def giveaway(i:discord.Interaction,preis:str,dauer:str,winner:app_commands.Range[int,1,20]):
    if not is_mod(i): return await deny(i)
    try: end=datetime.now(timezone.utc)+duration(dauer)
    except ValueError as e: return await i.response.send_message(str(e),ephemeral=True)
    ch=bot.get_channel(int(C.get("giveaway_channel_id",0) or 0))
    if not ch: return await i.response.send_message("Giveaway-Channel nicht konfiguriert.",ephemeral=True)
    e=branded("Giveaway",f"**Preis:** {preis}\n**Dauer:** {dauer}\n**Gewinner:** {winner}\n**Ende:** <t:{int(end.timestamp())}:R>")
    await i.response.defer(ephemeral=True); msg=await ch.send(embed=e,view=GiveawayView())
    DB.execute("INSERT INTO giveaways VALUES(?,?,?,?,?,?,0)",(msg.id,ch.id,preis,winner,end.isoformat())); DB.commit()
    await i.followup.send("Giveaway gestartet.",ephemeral=True); await log(f"Giveaway gestartet: {preis} | Mod-ID {i.user.id}")
@bot.tree.command(name="greroll",description="Zieht einen neuen Giveaway-Gewinner")
async def greroll(i:discord.Interaction,message_id:str):
    if not is_mod(i): return await deny(i)
    try: mid=int(message_id)
    except ValueError: return await i.response.send_message("Ungültige Nachrichten-ID.",ephemeral=True)
    row=DB.execute("SELECT prize,ended FROM giveaways WHERE message=?",(mid,)).fetchone()
    if not row or not row[1]: return await i.response.send_message("Giveaway nicht gefunden oder noch aktiv.",ephemeral=True)
    users=[x[0] for x in DB.execute("SELECT user FROM entries WHERE message=?",(mid,))]
    if not users: return await i.response.send_message("Keine Teilnehmer.",ephemeral=True)
    uid=random.choice(users); await i.response.send_message(f"Neuer Gewinner: <@{uid}>")
    await log(f"Giveaway-Reroll: {row[0]} | Gewinner-ID {uid} | Mod-ID {i.user.id}")

@bot.tree.command(name="warn",description="Verwarnt einen User")
async def warn(i:discord.Interaction,user:discord.Member,grund:str):
    if not is_mod(i): return await deny(i)
    DB.execute("INSERT INTO warns(user,mod,reason,created) VALUES(?,?,?,?)",(user.id,i.user.id,grund,datetime.now(timezone.utc).isoformat())); DB.commit()
    await i.response.send_message(f"{user.mention} wurde verwarnt. Grund: {grund}"); await case("Warn",user,i,grund)
@bot.tree.command(name="timeout",description="Gibt einem User Timeout")
async def timeout(i:discord.Interaction,user:discord.Member,dauer:str,grund:str):
    if not is_mod(i): return await deny(i)
    try: d=duration(dauer)
    except ValueError as e: return await i.response.send_message(str(e),ephemeral=True)
    if d>timedelta(days=28): return await i.response.send_message("Maximal 28 Tage.",ephemeral=True)
    await user.timeout(d,reason=grund); await i.response.send_message(f"{user.mention} erhielt Timeout: {dauer}."); await case("Timeout",user,i,grund+" | "+dauer)
@bot.tree.command(name="kick",description="Kickt einen User")
async def kick(i:discord.Interaction,user:discord.Member,grund:str):
    if not is_mod(i): return await deny(i)
    await user.kick(reason=grund); await i.response.send_message(f"{user} wurde gekickt."); await case("Kick",user,i,grund)
@bot.tree.command(name="ban",description="Bannt einen User")
async def ban(i:discord.Interaction,user:discord.Member,grund:str):
    if not is_mod(i): return await deny(i)
    await user.ban(reason=grund); await i.response.send_message(f"{user} wurde gebannt."); await case("Ban",user,i,grund)
@bot.tree.command(name="unban",description="Entbannt anhand der User-ID")
async def unban(i:discord.Interaction,user_id:str,grund:str):
    if not is_mod(i): return await deny(i)
    try: uid=int(user_id); entry=await i.guild.fetch_ban(discord.Object(id=uid)); await i.guild.unban(entry.user,reason=grund)
    except (ValueError,discord.NotFound): return await i.response.send_message("Gebannter User nicht gefunden.",ephemeral=True)
    await i.response.send_message(f"{entry.user} wurde entbannt."); await case("Unban",entry.user,i,grund)
@bot.tree.command(name="clear",description="Löscht Nachrichten")
async def clear(i:discord.Interaction,anzahl:app_commands.Range[int,1,100]):
    if not is_mod(i): return await deny(i)
    await i.response.defer(ephemeral=True); deleted=await i.channel.purge(limit=anzahl)
    await i.followup.send(f"{len(deleted)} Nachrichten gelöscht.",ephemeral=True); await log(f"Clear: {len(deleted)} Nachrichten | Mod-ID {i.user.id} | Channel-ID {i.channel_id}")
@bot.tree.command(name="warns",description="Zeigt Verwarnungen")
async def warns(i:discord.Interaction,user:discord.Member):
    if not is_mod(i): return await deny(i)
    rows=DB.execute("SELECT id,reason,created FROM warns WHERE user=? AND active=1 ORDER BY id DESC",(user.id,)).fetchall()
    e=discord.Embed(title=f"Verwarnungen: {user}",description="\n".join(f"#{r[0]} – {r[1]} | {r[2]}" for r in rows) or "Keine aktiven Verwarnungen.",color=GREEN)
    await i.response.send_message(embed=e,ephemeral=True)
@bot.tree.command(name="unwarn",description="Entfernt eine Verwarnung per Case-ID")
async def unwarn(i:discord.Interaction,case_id:int,grund:str):
    if not is_mod(i): return await deny(i)
    row=DB.execute("SELECT user FROM warns WHERE id=? AND active=1",(case_id,)).fetchone()
    if not row: return await i.response.send_message("Warn-Case nicht gefunden.",ephemeral=True)
    DB.execute("UPDATE warns SET active=0 WHERE id=?",(case_id,)); DB.commit()
    await i.response.send_message(f"Verwarnung #{case_id} entfernt."); await log(f"Unwarn Case {case_id} | User-ID {row[0]} | Mod-ID {i.user.id} | Grund: {grund}")

if __name__=="__main__": bot.run(TOKEN)
