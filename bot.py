# -*- coding: utf-8 -*-
from discord import member
import discord
from discord.ext import commands
from discord import app_commands
import config
import asyncio
import logging
import os
import re
import time
import typing
from typing import Optional
import aiohttp
from aiohttp import web
import json
from datetime import datetime, timedelta
import pytz
import random

def has_admin_role():
    """Permite comandos administrativos únicamente al dueño del servidor."""
    async def predicate(interaction: discord.Interaction):
        return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id
    return app_commands.check(predicate)

def has_voucher_permission():
    """Permite comandos de vouch a administradores, fundadores y owners."""
    async def predicate(interaction: discord.Interaction):
        if interaction.guild is None:
            return False
        
        # Owner siempre tiene permiso
        if interaction.user.id == interaction.guild.owner_id:
            return True
        
        # Buscar roles permitidos
        allowed_role_names = {"vendedor", "vendedores", "administrador", "administradores", "admin", "admins", "fundador", "fundadores", "founder", "creador", "creator"}
        user_role_names = {
            re.sub(r"[^a-z0-9]", "", role.name.lower())
            for role in interaction.user.roles
        }
        
        # Verificar si tiene algún rol permitido
        has_permission = any(
            allowed_name in role_name
            for role_name in user_role_names
            for allowed_name in allowed_role_names
        )
        
        return has_permission
    return app_commands.check(predicate)

# Configurar logging limpio y silencioso para errores de voz
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Suprimir completamente todos los errores de voz
logging.getLogger('discord.voice_state').setLevel(logging.CRITICAL + 1)  # Más alto que CRITICAL
logging.getLogger('discord.gateway').setLevel(logging.CRITICAL + 1)
logging.getLogger('discord.voice_client').setLevel(logging.CRITICAL + 1)
logging.getLogger('discord.player').setLevel(logging.CRITICAL + 1)
logging.getLogger('discord.opus').setLevel(logging.CRITICAL + 1)

# También suprimir warnings de discord que puedan estar relacionados con voz
logging.getLogger('discord').setLevel(logging.WARNING)

class ChannelRestrictionError(app_commands.CheckFailure):
    """Se lanza cuando un comando se usa fuera de tickets o del canal de comandos."""
    pass

class NexusCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not config.COMMANDS_RESTRICTION_ENABLED or interaction.guild is None:
            return True

        # El dueño del servidor puede usar comandos en cualquier canal
        if interaction.user.id == interaction.guild.owner_id:
            return True

        channel = interaction.channel
        if channel and getattr(channel, "category", None) and channel.category.name.upper() == "TICKETS":
            return True

        commands_channel = get_configured_channel(interaction.guild, config.COMMANDS_CHANNEL_ID, config.COMMANDS_CHANNEL_NAME)
        if commands_channel and channel and channel.id == commands_channel.id:
            return True

        raise ChannelRestrictionError(
            f"Los comandos solo pueden usarse en tickets o en {commands_channel.mention if commands_channel else '#' + config.COMMANDS_CHANNEL_NAME}."
        )

class NexusStoreBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.voice_states = True  # Añadir intent para estados de voz
        
        super().__init__(
            command_prefix=config.PREFIX,
            intents=intents,
            help_command=None,
            tree_cls=NexusCommandTree
        )
        
        # Variables para manejo de voz
        self.voice_reconnect_attempts = {}
        self.last_voice_error = {}
        
        # Variables para evitar duplicados
        self.welcome_sent = set()  # Para evitar mensajes de bienvenida duplicados
        self.invite_sent = set()   # Para evitar mensajes de invitación duplicados
        
        # Variables para anti-spam y anti-flood
        self.user_messages = {}  # Para tracking de mensajes por usuario
        self.flood_warnings = set()  # Para evitar advertencias duplicadas
        
        # Variables para sistema de blindaje RB3 Guard
        self.nuke_tracking = {}  # Para tracking de acciones anti-nuke
        self.raid_tracking = []  # Para tracking de joins anti-raid
        self.phishing_links = set()  # Base de datos de enlaces de phishing
        self.ip_logger_links = set()  # Base de datos de IP loggers
        self.invite_spam_tracking = {}  # Tracking de spam de invitaciones
        self.moderation_warnings = {}
        self.owner_mention_counts = {}
        self.recent_bans = {}
        self.faq_cooldown = {}
        self.ticket_warning_sent = set()
        self._web_runner = None
        
                
        # Inicializar bases de datos de seguridad
        self._init_security_databases()
    
    def _init_security_databases(self):
        """Inicializa las bases de datos de seguridad para el blindaje"""
        # Enlaces de phishing conocidos
        phishing_domains = [
            'discord-gift.com', 'freediscordnitro.com', 'nitro-gift.net',
            'discord-nitro.com', 'getdiscordnitro.com', 'discordnitrofree.com',
            'steam-gifts.net', 'freesteamkeys.com', 'steamgift.net',
            'bit-ly.com', 'discordl.com', 'discord-app.com', 'discordgift.net'
        ]
        
        # Servicios de IP logging conocidos
        ip_logger_domains = [
            'iplogger.org', 'iplogger.com', 'grabify.link',
            'gyazo.nl', 'iplogger.co', '2no.co', 'iplogger.info',
            'whatstheirip.com', 'ipgrabber.org', 'ipfish.com',
            'yooo.ink', 'iplogger.ru', 'iplogger.biz'
        ]
        
        # Dominios de spam de invitaciones
        invite_spam_domains = [
            'discord.gg', 'discord.com/invite', 'discord.io',
            'invite.gg', 'discord.me', 'dsc.gg', 'dis.gd'
        ]
        
        # Cargar las bases de datos
        self.phishing_links.update(phishing_domains)
        self.ip_logger_links.update(ip_logger_domains)
        self.invite_spam_domains = set(invite_spam_domains)
        
        logger.info(f"Base de datos de seguridad inicializada:")
        logger.info(f"- {len(self.phishing_links)} dominios de phishing")
        logger.info(f"- {len(self.ip_logger_links)} dominios de IP logger")
        logger.info(f"- {len(self.invite_spam_domains)} dominios de spam de invitaciones")

    async def setup_hook(self):
        app = web.Application()

        async def health_check(request):
            return web.Response(text="Nexus Store Bot OK")

        app.router.add_get("/", health_check)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(
            self._web_runner,
            "0.0.0.0",
            int(os.getenv("PORT", "10000")),
        )
        await site.start()
        logger.info("Servidor de salud HTTP iniciado")
    
    async def on_ready(self):
        logger.info(f'Bot conectado como {self.user}')
        logger.info(f'ID del Bot: {self.user.id}')
        logger.info('------')
        await self.tree.sync()
        logger.info("Comandos slash sincronizados")
    
    async def on_voice_state_update(self, member, before, after):
        """Manejar cambios de estado de voz de forma silenciosa"""
        if member == self.user:
            if before.channel and not after.channel:
                # Reconexión silenciosa
                await asyncio.sleep(10)
                try:
                    await connect_to_voice_channel()
                except:
                    pass
            elif after.channel and not before.channel:
                # Conexión silenciosa - no logging
                pass
    
    async def on_disconnect(self):
        """Manejar desconexión del bot de forma silenciosa"""
        pass
    
    async def on_resumed(self):
        """Manejar reconexión del bot de forma silenciosa"""
        # Reconectar a voz silenciosamente si estaba conectado
        try:
            await connect_to_voice_channel()
        except:
            pass

bot = NexusStoreBot()

GIVEAWAYS_FILE = "giveaways.json"

async def get_next_vouch_number(channel) -> int:
    """Guarda el contador de vouches en el topic del canal para que sobreviva a reinicios de Render."""
    import re
    current = 177
    if channel.topic:
        match = re.search(r'VOUCH_COUNT:(\d+)', channel.topic)
        if match:
            current = int(match.group(1))
    new_count = current + 1
    tag = f"VOUCH_COUNT:{new_count}"
    if channel.topic and re.search(r'VOUCH_COUNT:\d+', channel.topic):
        new_topic = re.sub(r'VOUCH_COUNT:\d+', tag, channel.topic)
    else:
        new_topic = f"{channel.topic + ' ' if channel.topic else ''}{tag}"
    try:
        await channel.edit(topic=new_topic)
    except discord.Forbidden:
        logger.warning("No tengo permisos para editar el topic del canal de vouches")
    return new_count

def parse_duration(duration_str: str) -> Optional[int]:
    """Convierte una cadena de duración (ej. 10s, 5m, 2h, 1d) a segundos."""
    import re
    pattern = r'^(\d+)([smhd])$'
    match = re.match(pattern, duration_str.lower())
    if not match:
        return None
    
    amount = int(match.group(1))
    unit = match.group(2)
    
    if unit == 's':
        return amount
    elif unit == 'm':
        return amount * 60
    elif unit == 'h':
        return amount * 3600
    elif unit == 'd':
        return amount * 86400
    return None

def load_giveaways():
    import os
    if not os.path.exists(GIVEAWAYS_FILE):
        return []
    try:
        with open(GIVEAWAYS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error cargando sorteos: {e}")
        return []

def save_giveaways(data):
    try:
        with open(GIVEAWAYS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando sorteos: {e}")

REPUTATION_FILE = "reputation.json"

def load_reputation():
    if not os.path.exists(REPUTATION_FILE):
        return {}
    try:
        with open(REPUTATION_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error cargando reputación: {e}")
        return {}

def save_reputation(data):
    try:
        with open(REPUTATION_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando reputación: {e}")

async def apply_purchase_rank(guild, member, total: int):
    """Asigna el rol de rango más alto alcanzado según el total de compras."""
    tiers = sorted(config.PURCHASE_RANK_ROLES.items())
    achieved_name = None
    for threshold, role_name in tiers:
        if total >= threshold:
            achieved_name = role_name
    if not achieved_name:
        return

    all_tier_names = {name for _, name in tiers}
    target_role = discord.utils.get(guild.roles, name=achieved_name)
    if not target_role:
        logger.warning(f"No se encontró el rol de rango '{achieved_name}' en {guild.name}")
        return

    roles_to_remove = [role for role in member.roles if role.name in all_tier_names and role.name != achieved_name]
    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Actualización de rango por compras")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Rango alcanzado por compras")
    except discord.Forbidden:
        logger.warning(f"No tengo permisos para asignar rangos por compras a {member}")

def _top_purchasers(reputation: dict, limit: int):
    return sorted(reputation.items(), key=lambda item: item[1], reverse=True)[:limit]

async def maybe_announce_leaderboard(guild, channel, reputation_before: dict, reputation_after: dict):
    if not config.LEADERBOARD_ENABLED:
        return
    limit = config.LEADERBOARD_SIZE
    before_ids = [uid for uid, _ in _top_purchasers(reputation_before, limit)]
    after_top = _top_purchasers(reputation_after, limit)
    after_ids = [uid for uid, _ in after_top]
    if before_ids == after_ids:
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, (uid, count) in enumerate(after_top, start=1):
        member = guild.get_member(int(uid))
        display = member.mention if member else f"Usuario ({uid})"
        rank_icon = medals[index - 1] if index <= 3 else f"`#{index}`"
        lines.append(f"{rank_icon} {display} — **{count}** cuentas")

    embed = discord.Embed(
        title="🏆 TOP COMPRADORES — NEXUS STOCK",
        description="\n".join(lines) if lines else "Aún no hay compras registradas.",
        color=config.COLOR_EMBED
    )
    embed.set_footer(text="Nexus Stock © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()
    await channel.send(embed=embed)

async def register_purchase(guild, user, amount: int = 1, seller: str = "Nexus"):
    """Suma compras al historial de reputación y anuncia el total en el canal correspondiente."""
    if not config.REPUTATION_ENABLED:
        return
    reputation_before = load_reputation()
    reputation = dict(reputation_before)
    key = str(user.id)
    reputation[key] = reputation.get(key, 0) + amount
    save_reputation(reputation)

    channel = get_configured_channel(guild, config.REPUTATION_CHANNEL_ID, config.REPUTATION_CHANNEL_NAME)
    if not channel:
        logger.warning(f"No se encontró el canal de reputación en {guild.name}")
        return

    # Calcular total general de compras en el servidor
    total_server_purchases = sum(reputation.values())
    
    # Obtener mención del vendedor
    seller_mention = seller
    if seller.lower() == "nexus":
        seller_mention = guild.owner.mention
    elif seller.lower() == "mayer":
        # Buscar usuario con rol de Mayer o similar
        mayer_role = discord.utils.get(guild.roles, name="Mayer")
        if mayer_role:
            mayer_member = discord.utils.find(lambda m: mayer_role in m.roles, guild.members)
            seller_mention = mayer_member.mention if mayer_member else seller
    elif seller.lower() == "noxy":
        # Buscar usuario con rol de Noxy o similar
        noxy_role = discord.utils.get(guild.roles, name="Noxy")
        if noxy_role:
            noxy_member = discord.utils.find(lambda m: noxy_role in m.roles, guild.members)
            seller_mention = noxy_member.mention if noxy_member else seller

    total = reputation[key]
    embed = discord.Embed(
        title="🛒 COMPRA REGISTRADA",
        description=(
            "> 🔴 **NEXUS STOCK — REPUTACIÓN**\n\n"
            f"🛒 **{user.mention}** acaba de comprarle una cuenta a {seller_mention}.\n\n"
            f"📦 **Cuentas compradas:** `{total_server_purchases}`\n\n"
            "⭐ Gracias por confiar en **Nexus Stock**.\n"
            "Tu compra ha sido registrada correctamente en nuestro sistema.\n\n"
            "🔴 **Nexus Stock • Trusted Stock**"
        ),
        color=config.COLOR_EMBED
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    if isinstance(user, discord.Member):
        await apply_purchase_rank(guild, user, total)

    await maybe_announce_leaderboard(guild, channel, reputation_before, reputation)

class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Participar", style=discord.ButtonStyle.primary, custom_id="giveaway_participate")
    async def participate(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_id = interaction.message.id
        giveaways = load_giveaways()
        giveaway = next((g for g in giveaways if g["message_id"] == message_id), None)
        if not giveaway:
            await interaction.response.send_message("Este sorteo no existe o no está registrado.", ephemeral=True)
            return
            
        if giveaway.get("ended", False):
            await interaction.response.send_message("Este sorteo ya ha finalizado.", ephemeral=True)
            return
            
        user_id = interaction.user.id
        if user_id in giveaway["participants"]:
            giveaway["participants"].remove(user_id)
            save_giveaways(giveaways)
            
            embed = interaction.message.embeds[0]
            embed.description = f"Reacciona con el botón de abajo para participar!\n\n**Premio:** {giveaway['prize']}\n**Ganadores:** {giveaway['winners_count']}\n**Participantes:** {len(giveaway['participants'])}\n**Finaliza:** <t:{int(giveaway['end_time'])}:R>"
            await interaction.message.edit(embed=embed)
            await interaction.response.send_message("Has salido del sorteo.", ephemeral=True)
        else:
            giveaway["participants"].append(user_id)
            save_giveaways(giveaways)
            
            embed = interaction.message.embeds[0]
            embed.description = f"Reacciona con el botón de abajo para participar!\n\n**Premio:** {giveaway['prize']}\n**Ganadores:** {giveaway['winners_count']}\n**Participantes:** {len(giveaway['participants'])}\n**Finaliza:** <t:{int(giveaway['end_time'])}:R>"
            await interaction.message.edit(embed=embed)
            await interaction.response.send_message("¡Te has registrado en el sorteo con éxito!", ephemeral=True)

async def end_giveaway(message_id: int):
    giveaways = load_giveaways()
    giveaway = next((g for g in giveaways if g["message_id"] == message_id), None)
    if not giveaway or giveaway.get("ended", False):
        return
        
    giveaway["ended"] = True
    save_giveaways(giveaways)
    
    try:
        guild = bot.get_guild(giveaway["guild_id"])
        if not guild:
            guild = await bot.fetch_guild(giveaway["guild_id"])
        channel = guild.get_channel(giveaway["channel_id"])
        if not channel:
            channel = await bot.fetch_channel(giveaway["channel_id"])
        message = await channel.fetch_message(message_id)
    except Exception as e:
        logger.error(f"Error recuperando mensaje del sorteo {message_id}: {e}")
        return
        
    participants = giveaway["participants"]
    winners_count = giveaway["winners_count"]
    prize = giveaway["prize"]
    
    if not participants:
        embed = message.embeds[0]
        embed.description = f"**Premio:** {prize}\n**Ganadores:** Ninguno (no hubo participantes)\n\nEl sorteo ha finalizado."
        embed.color=config.COLOR_EMBED
        await message.edit(embed=embed, view=None)
        await channel.send(f"El sorteo por **{prize}** ha terminado sin participantes.")
        return
        
    actual_winners_count = min(len(participants), winners_count)
    
    import random
    winners_ids = random.sample(participants, actual_winners_count)
    winners_mentions = ", ".join([f"<@{uid}>" for uid in winners_ids])
    
    embed = message.embeds[0]
    embed.description = f"**Premio:** {prize}\n**Ganadores:** {winners_mentions}\n**Participantes:** {len(participants)}\n\nEl sorteo ha finalizado."
    embed.color=config.COLOR_EMBED
    await message.edit(embed=embed, view=None)
    
    await channel.send(f"Felicidades {winners_mentions}! Has ganado el sorteo por **{prize}**.")

async def check_giveaways_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            current_time = discord.utils.utcnow().timestamp()
            giveaways = load_giveaways()
            for giveaway in giveaways:
                if not giveaway.get("ended", False) and current_time >= giveaway["end_time"]:
                    await end_giveaway(giveaway["message_id"])
        except Exception as e:
            logger.error(f"Error en el bucle de sorteos: {e}")
        await asyncio.sleep(10)

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Buscar emojis personalizados
        logo_emoji = None
        close_emoji = None
        for e in bot.emojis:
            if e.name.lower() == "logo":
                logo_emoji = e
            elif e.name == "13":
                close_emoji = e
        self.claim_ticket.emoji = logo_emoji
        self.close_ticket.emoji = close_emoji

    @discord.ui.button(label="Reclamar Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Solo staff puede reclamar
        staff_role = interaction.guild.get_role(config.SUPPORT_ROLE_ID) if config.SUPPORT_ROLE_ID else None
        admin_role = interaction.guild.get_role(config.ADMIN_ROLE_ID) if config.ADMIN_ROLE_ID else None
        is_staff = (staff_role and staff_role in interaction.user.roles) or (admin_role and admin_role in interaction.user.roles)
        if not is_staff:
            await interaction.response.send_message("❌ Solo el staff puede reclamar tickets.", ephemeral=True)
            return
        button.disabled = True
        button.label = f"Reclamado por {interaction.user.display_name}"
        button.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"✅ {interaction.user.mention} ha reclamado este ticket y te atenderá.")

    @discord.ui.button(label="Cerrar Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_role_names = {"founder", "creator", "admin", "bots", "staff"}
        user_role_names = {
            re.sub(r"[^a-z0-9]", "", role.name.lower())
            for role in interaction.user.roles
        }
        can_close_ticket = (
            interaction.user.id == interaction.guild.owner_id
            or interaction.user.guild_permissions.administrator
            or any(
                allowed_name in role_name
                for role_name in user_role_names
                for allowed_name in allowed_role_names
            )
        )
        if not can_close_ticket:
            await interaction.response.send_message("❌ Solo Owner, Founder, Creator, Admin, Bots o Staff puede cerrar tickets.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 El ticket se cerrará y este canal se eliminará en 5 segundos...")
        # Embed de cierre
        close_embed = discord.Embed(
            title="Ticket Cerrado | Nexus Store",
            description=(
                f"**Este ticket ha sido cerrado por {interaction.user.mention}.**\n\n"
                f"Gracias por contactar con el equipo de Nexus Store.\n"
                f"Si necesitas ayuda nuevamente, no dudes en abrir un nuevo ticket.\n\n"
                f"*Nexus Store — Manteniendo la comunidad segura y organizada.*"
            ),
            color=config.COLOR_EMBED
        )
        close_embed.set_footer(text="NexusStore © Todos los derechos reservados")
        close_embed.timestamp = discord.utils.utcnow()
        await interaction.channel.send(embed=close_embed)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except Exception as e:
            await interaction.followup.send(f"Error al eliminar el canal: {e}", ephemeral=True)

def _get_next_ticket_channel_name(guild, base_name: str) -> str:
    existing_names = {
        channel.name.lower()
        for channel in guild.text_channels
        if channel.category and channel.category.name.upper() == "TICKETS"
    }

    if base_name.lower() not in existing_names:
        return base_name

    number = 2
    while True:
        candidate = f"{base_name}-{number}"
        if candidate.lower() not in existing_names:
            return candidate
        number += 1

async def _create_ticket_channel(interaction: discord.Interaction, category_key: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    category_name = "TICKETS"
    category = discord.utils.get(guild.categories, name=category_name)
    if not category:
        try:
            category = await guild.create_category(category_name)
        except discord.Forbidden:
            category = None

    # Un solo ticket abierto por persona
    if category:
        owner_marker = f"ticket_owner_id:{interaction.user.id}"
        for existing_channel in category.text_channels:
            if existing_channel.topic and owner_marker in existing_channel.topic:
                await interaction.followup.send(
                    f"⚠️ Ya tienes un ticket abierto en {existing_channel.mention}. Ciérralo antes de abrir otro.",
                    ephemeral=True
                )
                return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
    }

    support_role = None
    if config.SUPPORT_ROLE_ID:
        support_role = guild.get_role(config.SUPPORT_ROLE_ID)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    prefix_map = {
        "comprar_cuenta": "Comprar",
        "reclamar_drop": "Reclamar",
        "dudas": "Soporte",
        "partners": "Partner",
        "otra_consulta": "Otro"
    }
    prefix = prefix_map.get(category_key, "Ticket")

    raw_name_parts = interaction.user.display_name.strip().split()
    sanitized_name = re.sub(r"[^A-Za-z0-9]", "", raw_name_parts[0]) if raw_name_parts else ""
    sanitized_name = sanitized_name.capitalize() if sanitized_name else "Usuario"

    base_channel_name = f"{prefix}-{sanitized_name}"
    channel_name = _get_next_ticket_channel_name(guild, base_channel_name)

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket de {interaction.user.name} - Tipo: {category_key} - ticket_owner_id:{interaction.user.id}"
        )
    except Exception as e:
        await interaction.followup.send(f"No se pudo crear el canal de ticket: {e}", ephemeral=True)
        return

    ticket_messages = {
        "reclamar_drop": (
            "🔴 **NEXUS STOCK — DROP**\n\n"
            f"> 👤 **{interaction.user.mention}** viene a reclamar su **drop**.\n"
            "🎁 Por favor, espera a que un miembro del Staff atienda tu ticket.\n\n"
            "📌 Ten preparado cualquier información necesaria para verificar tu reclamo.\n\n"
            "**Nexus Stock — Ticket System**"
        ),
        "comprar_cuenta": (
            "🔴 **NEXUS STOCK — COMPRA**\n\n"
            f"> 👤 **{interaction.user.mention}** ha abierto un ticket relacionado con una **compra**.\n"
            "🛒 Un miembro del Staff revisará tu solicitud lo antes posible.\n\n"
            "📌 Mantén toda la información relacionada con tu compra dentro del ticket.\n\n"
            "**Nexus Stock — Ticket System**"
        ),
        "dudas": (
            "🔴 **NEXUS STOCK — SOPORTE**\n\n"
            f"> 🔧 **{interaction.user.mention}** viene a solicitar **soporte**.\n"
            "📌 **Información**\n"
            "Explica detalladamente el problema que estás teniendo para que un miembro del Staff pueda ayudarte.\n\n"
            "⏳ **Estado:** Esperando atención del Staff.\n\n"
            "⚠️ Evita hacer spam o mencionar repetidamente al Staff. Serás atendido cuando esté disponible.\n\n"
            "**Nexus Stock — Ticket System**"
        ),
        "otra_consulta": (
            "🔴 **NEXUS STOCK — OTRO**\n\n"
            f"> 📩 **{interaction.user.mention}** ha abierto un ticket para una **consulta o asunto diferente**.\n"
            "📌 **Información**\n"
            "Explica claramente el motivo de tu ticket para que un miembro del Staff pueda ayudarte.\n\n"
            "⏳ **Estado:** Esperando atención del Staff.\n\n"
            "⚠️ Evita abrir tickets innecesarios o hacer spam.\n\n"
            "**Nexus Stock — Ticket System**"
        )
    }
    embed = discord.Embed(
        description=ticket_messages.get(category_key, ticket_messages["otra_consulta"]),
        color=config.COLOR_EMBED
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()

    view = TicketControlView()
    await ticket_channel.send(embed=embed, view=view)

    mention_str = interaction.user.mention
    if support_role:
        mention_str += f" {support_role.mention}"
    await ticket_channel.send(f"{mention_str} Nuevo ticket creado.")

    await interaction.followup.send(f"Tu ticket ha sido creado en {ticket_channel.mention}", ephemeral=True)

def get_custom_emoji(name: str, fallback: str):
    for emoji in bot.emojis:
        if emoji.name and emoji.name.lower() == name.lower():
            return emoji
    return fallback

@bot.tree.command(name="paymethods", description="Muestra los métodos de pago disponibles")
@has_admin_role()
async def paymethods(interaction: discord.Interaction):
    """Envía los métodos de pago con emojis personalizados y texto configurable."""
    emoji_paypal = get_custom_emoji("paypal", "💰")
    emoji_bank = get_custom_emoji("bank", "🪙")
    emoji_ltc = get_custom_emoji("ltc", "🏦")
    emoji_bitcoin = get_custom_emoji("bitcoin", "🏦")

    message_text = config.PAY_METHODS_MESSAGE.format(
        paypal=emoji_paypal,
        bank=emoji_bank,
        ltc=emoji_ltc,
        bitcoin=emoji_bitcoin,
    )

    embed = discord.Embed(
        title=config.PAY_METHODS_TITLE,
        description=message_text,
        color=config.COLOR_EMBED
    )
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()

    await interaction.response.send_message(embed=embed)

class TicketDropdown(discord.ui.Select):
    def __init__(self):
        def get_emoji(name, fallback):
            for e in bot.emojis:
                if e.name.lower() == name.lower():
                    return e
            return fallback

        emoji_compras = get_emoji("moneyspreead", "💰")
        emoji_drop = get_emoji("drop", "🎁")
        emoji_dudas = get_emoji("support", "❓")
        emoji_partners = get_emoji("partners", "🤝")

        options = [
            discord.SelectOption(
                label="COMPRAR UNA CUENTA",
                description="Abre un ticket para comprar una cuenta.",
                value="comprar_cuenta",
                emoji=emoji_compras
            ),
            discord.SelectOption(
                label="RECLAMAR UN DROP",
                description="Abre un ticket para reclamar un drop.",
                value="reclamar_drop",
                emoji=emoji_drop
            ),
            discord.SelectOption(
                label="DUDAS O PREGUNTAS",
                description="Abre un ticket si tienes dudas o preguntas.",
                value="dudas",
                emoji=emoji_dudas
            ),
            discord.SelectOption(
                label="PARTNERS",
                description="Abre un ticket para hablar de partnerships.",
                value="partners",
                emoji=emoji_partners
            )
        ]
        super().__init__(
            placeholder="Selecciona una opción...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_department_select"
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        await _create_ticket_channel(interaction, value)

class TicketDropdownView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())

class TicketPanelSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="COMPRAR CUENTA",
                description="Abrir un ticket para comprar una cuenta.",
                value="comprar_cuenta",
                emoji="🛒"
            ),
            discord.SelectOption(
                label="DUDAS O PREGUNTAS",
                description="Solicitar soporte o resolver una duda.",
                value="dudas",
                emoji="❓"
            ),
            discord.SelectOption(
                label="RECLAMAR UN DROP",
                description="Abrir un ticket para reclamar un drop.",
                value="reclamar_drop",
                emoji="🎁"
            ),
            discord.SelectOption(
                label="OTRO",
                description="Otra consulta o asunto diferente.",
                value="otra_consulta",
                emoji="🛠️"
            )
        ]
        super().__init__(
            placeholder="Selecciona el motivo de tu ticket...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_panel_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await _create_ticket_channel(interaction, self.values[0])

class TicketPanelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect())

class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect())

@bot.event
async def on_ready():
    print(f'Bot conectado como {bot.user}')
    print(f'ID del Bot: {bot.user.id}')
    print('------')
    await bot.tree.sync()
    print("Comandos slash sincronizados")
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Comandos duplicados eliminados en: {guild.name}")
    
    # Conectar a canal de voz si está configurado
    if config.VOICE_CHANNEL_ID is not None:
        await connect_to_voice_channel()
    
    # Iniciar sistema de versículos diarios
    if config.VERSE_ENABLED:
        bot.loop.create_task(schedule_daily_verse())
    
    # Iniciar limpieza periódica de sets duplicados
    bot.loop.create_task(cleanup_duplicate_sets())
    
    # Registrar vista persistente de sorteos
    bot.add_view(GiveawayView())
    bot.add_view(TicketDropdownView())
    bot.add_view(TicketOpenView())
    bot.add_view(TicketControlView())
    
    # Iniciar el bucle de verificación de sorteos
    bot.loop.create_task(check_giveaways_loop())
    if config.TICKET_INACTIVITY_ENABLED:
        bot.loop.create_task(ticket_inactivity_loop())

async def safe_disconnect_voice_client(guild) -> bool:
    """Desconexión segura del cliente de voz con cleanup completo y sin errores"""
    try:
        if guild.voice_client:
            # Desconexión silenciosa sin logging
            try:
                await asyncio.wait_for(guild.voice_client.disconnect(), timeout=3.0)
            except:
                # Silenciosamente forzar desconexión si hay cualquier error
                try:
                    guild.voice_client.stop()
                    await guild.voice_client.disconnect(force=True)
                except:
                    pass
            
            # Esperar un momento para limpieza
            await asyncio.sleep(0.5)
            return True
        return False
    except:
        # Capturar y silenciar absolutamente todos los errores
        return False

async def connect_to_voice_channel():
    """Conecta el bot al canal de voz configurado con manejo silencioso de errores"""
    if config.VOICE_CHANNEL_ID is None:
        return False
        
    try:
        # Buscar el canal de voz en todos los servidores
        for guild in bot.guilds:
            voice_channel = guild.get_channel(config.VOICE_CHANNEL_ID)
            
            if voice_channel and isinstance(voice_channel, discord.VoiceChannel):
                guild_id = guild.id
                
                # Verificar límite de reintentos por guild
                if guild_id not in bot.voice_reconnect_attempts:
                    bot.voice_reconnect_attempts[guild_id] = 0
                
                if bot.voice_reconnect_attempts[guild_id] >= 10:
                    # Resetear después de una hora
                    if time.time() - bot.last_voice_error.get(guild_id, 0) > 3600:
                        bot.voice_reconnect_attempts[guild_id] = 0
                    else:
                        return False
                
                # Si ya está conectado, verificar si está en el canal correcto
                if guild.voice_client:
                    if guild.voice_client.channel.id == voice_channel.id and guild.voice_client.is_connected():
                        return True
                    else:
                        await safe_disconnect_voice_client(guild)
                        await asyncio.sleep(1)
                
                # Intentar conectar con manejo silencioso de errores
                for attempt in range(3):  # Reducido a 3 intentos para menos errores
                    try:
                        # Limpiar cualquier conexión residual
                        if guild.voice_client:
                            await safe_disconnect_voice_client(guild)
                            await asyncio.sleep(0.5)
                        
                        # Conectar con timeout corto
                        voice_client = await asyncio.wait_for(
                            voice_channel.connect(
                                reconnect=True,
                                self_mute=False,
                                self_deaf=False
                            ),
                            timeout=10.0
                        )
                        
                        # Esperar un momento para estabilizar
                        await asyncio.sleep(2)
                        
                        # Verificación de conexión
                        if voice_client and voice_client.is_connected():
                            # Resetear contador
                            bot.voice_reconnect_attempts[guild_id] = 0
                            
                            # Iniciar mantenimiento silencioso
                            try:
                                bot.loop.create_task(play_silent_audio(voice_client))
                            except:
                                pass  # Silenciar cualquier error en el mantenimiento
                            return True
                        else:
                            if voice_client:
                                await safe_disconnect_voice_client(guild)
                        
                    except Exception:
                        # Capturar absolutamente todos los errores silenciosamente
                        bot.voice_reconnect_attempts[guild_id] += 1
                        bot.last_voice_error[guild_id] = time.time()
                        
                        # Espera exponencial silenciosa
                        if attempt < 2:
                            await asyncio.sleep(3 + (attempt * 2))
                        continue
                
                return False
        
        return False
        
    except Exception:
        # Capturar y silenciar absolutamente todos los errores
        return False

async def play_silent_audio(voice_client):
    """Mantiene la conexión activa con verificación silenciosa y reconexión automática"""
    consecutive_failures = 0
    max_failures = 2  # Reducido para menos errores
    maintenance_active = True
    
    try:
        # Mantenimiento completamente silencioso
        while maintenance_active and voice_client and consecutive_failures < max_failures:
            try:
                # Verificación silenciosa de estado
                if not voice_client or not voice_client.is_connected():
                    consecutive_failures += 1
                    if consecutive_failures < max_failures:
                        await asyncio.sleep(15)
                        continue
                    else:
                        break
                
                # Verificación silenciosa del canal
                if not voice_client.channel:
                    consecutive_failures += 1
                    await asyncio.sleep(10)
                    continue
                
                # Verificación periódica más espaciada
                await asyncio.sleep(30)  # Verificar cada 30 segundos
                
                # Reiniciar contador si está estable
                if consecutive_failures > 0:
                    consecutive_failures = 0
                    
            except Exception:
                # Capturar todos los errores silenciosamente
                consecutive_failures += 1
                if consecutive_failures < max_failures:
                    await asyncio.sleep(20)
                else:
                    break
        
        # Manejo silencioso de fallos
        if consecutive_failures >= max_failures:
            # Desconexión silenciosa
            try:
                if voice_client:
                    guild = voice_client.channel.guild
                    await safe_disconnect_voice_client(guild)
            except:
                pass
            
            # Esperar y reconectar silenciosamente
            await asyncio.sleep(45)
            try:
                bot.loop.create_task(connect_to_voice_channel())
            except:
                pass
            
    except Exception:
        # Capturar y silenciar absolutamente todos los errores críticos
        try:
            if voice_client:
                guild = voice_client.channel.guild
                await safe_disconnect_voice_client(guild)
        except:
            pass
        
        # Esperar y reconectar silenciosamente
        await asyncio.sleep(60)
        try:
            bot.loop.create_task(connect_to_voice_channel())
        except:
            pass

# Sistema de Versículos Diarios
class VerseSource:
    """Clase para manejar diferentes fuentes de versículos"""
    
    @staticmethod
    async def get_daily_verse_from_bible_api() -> Optional[dict]:
        """Obtener versículo diario desde API en español"""
        try:
            async with aiohttp.ClientSession() as session:
                # Usar API que devuelve versículos en español
                url = "https://bible-api.com/es/random"
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data and 'text' in data and 'reference' in data:
                            return {
                                "reference": data['reference'],
                                "text": data['text'],
                                "source": "Bible API Español"
                            }
        except Exception as e:
            logger.error(f"Error obteniendo versículo de Bible API Español: {e}")
        return None
    
    @staticmethod
    async def get_verse_from_elversiculodeldia() -> Optional[dict]:
        """Obtener versículo desde elversiculodeldia.com"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                url = "https://www.elversiculodeldia.com/"
                async with session.get(url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        html = await response.text()
                        # Extraer versículo del HTML (simplificado)
                        import re
                        # Buscar el versículo principal
                        verse_match = re.search(r'<div class="verse-text[^>]*>(.*?)</div>', html, re.DOTALL)
                        ref_match = re.search(r'<div class="verse-reference[^>]*>(.*?)</div>', html, re.DOTALL)
                        
                        if verse_match and ref_match:
                            text = re.sub(r'<[^>]+>', '', verse_match.group(1)).strip()
                            reference = re.sub(r'<[^>]+>', '', ref_match.group(1)).strip()
                            
                            if text and reference:
                                return {
                                    "reference": reference,
                                    "text": text,
                                    "source": "ElVersiculoDelDia.com"
                                }
        except Exception as e:
            logger.error(f"Error obteniendo versículo de ElVersiculoDelDia: {e}")
        return None
    
    @staticmethod
    async def get_random_bible_verse() -> Optional[dict]:
        """Obtener versículo aleatorio desde una lista predefinida en español"""
        verses = [
            {"reference": "Isaías 53:6", "text": "Todos nosotros nos descarriamos como ovejas, cada cual se apartó por su camino; mas Jehová cargó en él el pecado de todos nosotros.", "source": "Isaías"},
            {"reference": "Juan 3:16", "text": "Porque de tal manera amó Dios al mundo, que ha dado a su Hijo unigénito, para que todo aquel que en él cree, no se pierda, mas tenga vida eterna.", "source": "Juan"},
            {"reference": "Filipenses 4:13", "text": "Todo lo puedo en Cristo que me fortalece.", "source": "Filipenses"},
            {"reference": "Romanos 8:28", "text": "Y sabemos que a los que aman a Dios, todas las cosas les ayudan a bien, esto es, a los que conforme a su propósito son llamados.", "source": "Romanos"},
            {"reference": "Proverbios 3:5-6", "text": "Confía en Jehová con todo tu corazón, y no te apoyes en tu propia prudencia. Reconócelo en todos tus caminos, y él enderezará tus veredas.", "source": "Proverbios"},
            {"reference": "Salmos 23:1", "text": "Jehová es mi pastor; nada me faltará.", "source": "Salmos"},
            {"reference": "Mateo 11:28", "text": "Venid a mí todos los que estáis trabajados y cargados, y yo os haré descansar.", "source": "Mateo"},
            {"reference": "Jeremías 29:11", "text": "Porque yo sé los pensamientos que tengo acerca de vosotros, dice Jehová, pensamientos de paz, y no de mal, para daros el fin que esperáis.", "source": "Jeremías"},
            {"reference": "Isaías 41:10", "text": "No temas, porque yo estoy contigo; no desmayes, porque yo soy tu Dios que te esfuerzo; siempre te ayudaré, siempre te sustentaré con la diestra de mi justicia.", "source": "Isaías"},
            {"reference": "Efesios 2:8-9", "text": "Porque por gracia sois salvos por medio de la fe; y esto no de vosotros, pues es don de Dios; no por obras, para que nadie se gloríe.", "source": "Efesios"},
            {"reference": "Gálatas 5:22-23", "text": "Mas el fruto del Espíritu es amor, gozo, paz, paciencia, benignidad, fe, mansedumbre, templanza; contra tales cosas no hay ley.", "source": "Gálatas"},
            {"reference": "1 Corintios 13:4-5", "text": "El amor es sufrido, es benigno; el amor no tiene envidia, el amor no es jactancioso, no se envanece.", "source": "1 Corintios"},
            {"reference": "2 Timoteo 3:16-17", "text": "Toda la Escritura es inspirada por Dios, y útil para enseñar, para redargüir, para corregir, para instruir en justicia.", "source": "2 Timoteo"},
            {"reference": "Josué 1:9", "text": "Mira que te mando que te esfuerces y seas valiente; no temas ni desmayes, porque Jehová tu Dios estará contigo en dondequiera que vayas.", "source": "Josué"},
            {"reference": "Salmos 46:1", "text": "Dios es nuestro refugio y fortaleza, nuestro pronto auxilio en las tribulaciones.", "source": "Salmos"},
            {"reference": "Proverbios 31:30", "text": "Engañosa es la gracia, y vana la hermosura; la mujer que teme a Jehová, ésta será alabada.", "source": "Proverbios"},
            {"reference": "Mateo 6:33", "text": "Mas buscad primeramente el reino de Dios y su justicia, y todas estas cosas os serán añadidas.", "source": "Mateo"},
            {"reference": "Lucas 1:37", "text": "Porque nada hay imposible para Dios.", "source": "Lucas"},
            {"reference": "Hechos 20:35", "text": "En todo he os mostrado que trabajando así, se debe ayudar a los necesitados, y recordar las palabras del Señor Jesús, que dijo: Más bienaventurado es dar que recibir.", "source": "Hechos"},
            {"reference": "Romanos 12:2", "text": "No os conforméis a este siglo, sino transformaos por medio de la renovación de vuestro entendimiento, para que comprobéis cuál sea la buena voluntad de Dios, agradable y perfecta.", "source": "Romanos"},
            {"reference": "1 Pedro 5:7", "text": "echando toda vuestra ansiedad sobre él, porque él tiene cuidado de vosotros.", "source": "1 Pedro"},
            {"reference": "1 Juan 4:8", "text": "El que no ama, no ha conocido a Dios; porque Dios es amor.", "source": "1 Juan"},
            {"reference": "Apocalipsis 21:4", "text": "Enjugará Dios toda lágrima de los ojos de ellos; y ya no habrá muerte, ni habrá más llanto, ni clamor, ni dolor; porque las primeras cosas ya pasaron.", "source": "Apocalipsis"}
        ]
        
        return random.choice(verses)

async def get_daily_verse() -> Optional[dict]:
    """Obtener versículo diario intentando múltiples fuentes"""
    # Intentar diferentes fuentes en orden de preferencia
    sources = [
        VerseSource.get_daily_verse_from_bible_api,
        VerseSource.get_verse_from_elversiculodeldia,
        VerseSource.get_random_bible_verse
    ]
    
    for source_func in sources:
        try:
            verse = await source_func()
            if verse and verse.get("text") and verse.get("reference"):
                logger.info(f"Versículo obtenido desde {verse.get('source', 'Fuente desconocida')}")
                return verse
        except Exception as e:
            logger.warning(f"Error en fuente {source_func.__name__}: {e}")
            continue
    
    logger.error("No se pudo obtener versículo de ninguna fuente")
    return None

def create_verse_embed(verse_data: dict) -> discord.Embed:
    """Crear embed para versículo diario con estilo NexusStore"""
    embed = discord.Embed(
        title="Versículo del Día",
        description=f"**{verse_data['reference']}**\n\n*{verse_data['text']}*",
        color=config.COLOR_EMBED  # Color personalizado de NexusStore
    )
    
    # Añadir reflexión
    reflection = get_reflection_for_verse(verse_data['reference'])
    if reflection:
        embed.add_field(
            name="Reflexión",
            value=reflection,
            inline=False
        )
    
    # Añadir oración
    prayer = get_prayer_for_verse(verse_data['reference'])
    if prayer:
        embed.add_field(
            name="Oración",
            value=prayer,
            inline=False
        )
    
    # Usar la imagen del panel como thumbnail
    embed.set_thumbnail(url=config.TICKET_PANEL_IMAGE)
    embed.set_footer(text=f"Fuente: {verse_data.get('source', 'Biblia')} | NexusStore © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()
    
    return embed

def get_reflection_for_verse(reference: str) -> str:
    """Obtener reflexión personalizada según el versículo"""
    reflections = {
        "Isaías 53:6": "Este versículo nos recuerda que, aunque cada persona puede alejarse de la voluntad de Dios, Él ha provisto un camino de reconciliación a través del sacrificio de su Hijo. La imagen de las ovejas muestra nuestra vulnerabilidad y la necesidad de un pastor que nos guíe y proteja.",
        "Juan 3:16": "El amor de Dios es tan grande que dio a su único Hijo para que todos tengamos la oportunidad de la vida eterna. Este es el corazón del evangelio y la máxima expresión del amor divino.",
        "Filipenses 4:13": "Nuestra fuerza no viene de nosotros mismos, sino de Cristo. Cuando confiamos en Él, encontramos la fortaleza para superar cualquier desafío que la vida nos presente.",
        "Romanos 8:28": "Dios trabaja en todas las circunstancias de nuestra vida, incluso en las difíciles, para cumplir sus buenos propósitos. Podemos confiar que su plan es perfecto.",
        "Proverbios 3:5-6": "Confiar plenamente en Dios requiere dejar nuestra sabiduría limitada y depender de Su infinita sabiduría. Cuando Él guía nuestros pasos, nunca nos desviaremos.",
        "Salmos 23:1": "Cuando Dios es nuestro pastor, tenemos todo lo que necesitamos. Su provisión, protección y guía son completas y suficientes para cada día.",
        "Mateo 11:28": "Jesús nos invita a encontrar descanso en Él cuando estamos agobiados. No debemos cargar solos nuestras cargas, sino entregarlas a Aquel que nos da paz.",
        "Jeremías 29:11": "Dios tiene planes de esperanza y futuro para cada uno de nosotros. Aunque no entendamos el presente, podemos confiar en Su propósito perfecto.",
        "Isaías 41:10": "La presencia de Dios es nuestra garantía de fortaleza y ayuda. No necesitamos temer, porque el Creador del universo está con nosotros siempre.",
        "Efesios 2:8-9": "La salvación es un regalo gratuito de Dios, no algo que podamos ganar por nuestros méritos. Su gracia es infinita y su amor inmerecido.",
        "Gálatas 5:22-23": "El fruto del Espíritu no es algo que producimos por nosotros mismos, sino el resultado natural de vivir en comunión con Dios. Estas virtudes transforman nuestro carácter y testimonian Su obra en nosotros.",
        "1 Corintios 13:4-5": "El verdadero amor, según Dios, es desinteresado y humilde. Nos desafía a amar como Cristo nos amó, sacrificando nuestro ego y buscando el bien de los demás.",
        "2 Timoteo 3:16-17": "La Biblia no es solo un libro antiguo, sino la palabra viva de Dios que nos guía, corrige y prepara para toda buena obra. Es nuestra brújula espiritual en un mundo confundido.",
        "Josué 1:9": "Dios nos llama a la valentía no porque seamos fuertes, porque Él está con nosotros. Su presencia es nuestra garantía de victoria en cualquier batalla que enfrentemos.",
        "Salmos 46:1": "En medio de las tormentas de la vida, Dios es nuestro refugio seguro. No solo nos protege, sino que nos fortalece y está siempre disponible cuando más lo necesitamos.",
        "Proverbios 31:30": "La verdadera belleza no está en la apariencia externa, sino en el carácter forjado por el temor a Dios. La sabiduría y el temor del Señor son los adornos más valiosos.",
        "Mateo 6:33": "Cuando priorizamos el reino de Dios, todas las demás cosas encuentran su lugar correcto. La ansiedad por el futuro se disuelve cuando confiamos en Su provisión soberana.",
        "Lucas 1:37": "No hay situación demasiado difícil para Dios. Lo que parece imposible para nosotros es completamente posible para Aquel que creó el universo con su palabra.",
        "Hechos 20:35": "Jesús nos enseñó que la verdadera bendición está en dar. Cuando servimos a otros con generosidad, experimentamos la alegría y el propósito que el mundo no puede ofrecer.",
        "Romanos 12:2": "Dios no quiere que nos adaptemos a los patrones del mundo, sino que nos transforme desde dentro. La renovación de nuestra mente nos permite discernir Su voluntad perfecta.",
        "1 Pedro 5:7": "Dios se preocupa personalmente por nosotros. Cada ansiedad, cada preocupación, puede ser entregada a Aquel que nos ama infinitamente y tiene el poder para intervenir.",
        "1 Juan 4:8": "El amor no es solo un atributo de Dios, es Su esencia misma. Conocer a Dios es experimentar Su amor y ser transformados para amar como Él ama.",
        "Apocalipsis 21:4": "La esperanza cristiana va más allá de esta vida. Dios promete un futuro donde no habrá más dolor, lágrimas o sufrimiento. Esta esperanza nos sustenta en el presente."
    }
    
    return reflections.get(reference, "Medita en cómo esta palabra de Dios puede transformar tu vida hoy. Permite que el Espíritu Santo hable a tu corazón a través de estas palabras eternas.")

def get_prayer_for_verse(reference: str) -> str:
    """Obtener oración personalizada según el versículo"""
    prayers = {
        "Isaías 53:6": "Padre Celestial, gracias por cargar con nuestros pecados en la cruz a través de tu Hijo Jesús. Ayúdanos a recordar siempre que necesitamos tu guía y protección en nuestra vida diaria. Permítenos seguir tus caminos y hacer tu voluntad en todo momento. En el nombre de Jesús, Amén.",
        "Juan 3:16": "Dios omnipotente, gracias por el regalo inmerecido de la salvación a través de tu Hijo. Ayúdanos a vivir cada día en gratitud por este gran amor y a compartir esta buena nueva con otros. En el nombre de Cristo, Amén.",
        "Filipenses 4:13": "Señor Jesús, reconozco que toda mi fuerza viene de ti. Ayúdame a depender completamente de ti en cada situación y a glorificarte con mi vida. Fortaléceme para hacer tu voluntad. Amén.",
        "Romanos 8:28": "Padre celestial, confío en tu soberanía y en tu buen plan para mi vida. Ayúdame a ver tu mano obra en todas las circunstancias y a descansar en tu providencia perfecta. En el nombre de Jesús, Amén.",
        "Proverbios 3:5-6": "Señor, quiero confiar en ti con todo mi corazón. Ayúdame a no depender de mi propia sabiduría sino a buscar tu guía en cada decisión. Endereza mis caminos según tu voluntad. Amén.",
        "Salmos 23:1": "Buen Pastor, gracias por ser mi guía y mi provisión. Ayúdame a descansar en tu cuidado y a confiar que nunca me faltará nada bueno cuando sigo tus pasos. En el nombre de Jesús, Amén.",
        "Mateo 11:28": "Jesús, vengo a ti con mis cargas y fatigas. Te entrego todas mis preocupaciones y te pido que me des tu paz y descanso. Renueva mis fuerzas cada día. Amén.",
        "Jeremías 29:11": "Dios de esperanza, gracias por tus planes de bien para mi vida. Aunque a veces no entiendo tu camino, confío en que tus propósitos son perfectos. Ayúdame a esperar en ti con fe. Amén.",
        "Isaías 41:10": "Dios todopoderoso, gracias por tu promesa de estar siempre conmigo. Quita todo temor de mi corazón y llena mi vida con tu fortaleza y paz. Sé mi ayuda y sustento cada día. Amén.",
        "Efesios 2:8-9": "Padre celestial, gracias por el regalo gratuito de la salvación. Ayúdame a nunca depender de mis obras sino siempre de tu gracia. Que mi vida refleje gratitud por tu inmenso amor. En el nombre de Jesús, Amén.",
        "Gálatas 5:22-23": "Espíritu Santo, te pido que produzcas en mí tu fruto. Ayúdame a crecer en amor, gozo, paz, paciencia y todas las virtudes que honran a Dios. Que mi vida refleje tu carácter. En el nombre de Jesús, Amén.",
        "1 Corintios 13:4-5": "Dios de amor, enséñame a amar como tú amas. Quita de mi corazón todo egoísmo y orgullo. Ayúdame a ser paciente, bondoso y humilde en mis relaciones. En el nombre de Cristo, Amén.",
        "2 Timoteo 3:16-17": "Señor, gracias por tu palabra inspirada. Ayúdame a estudiarla, meditar en ella y dejar que me transforme. Prepárame para toda buena obra según tu propósito. Amén.",
        "Josué 1:9": "Dios fuerte y valiente, te pido que me llenes de valor para enfrentar los desafíos de cada día. Recuérdame que estás siempre conmigo y que tu presencia me garantiza la victoria. Amén.",
        "Salmos 46:1": "Dios mi refugio y fortaleza, gracias por ser mi auxilio en tiempos de tribulación. Cuando me sienta débil o asustado, ayúdame a correr a ti y encontrar seguridad en tu presencia. Amén.",
        "Proverbios 31:30": "Padre celestial, ayúdame a valorar la verdadera belleza que viene del temor a ti. Cultiva en mí un carácter que te honre y refleje tu sabiduría. En el nombre de Jesús, Amén.",
        "Mateo 6:33": "Señor Jesús, ayúdame a buscarte primero en todo. Que tu reino y tu justicia sean mi prioridad máxima. Confío en que todas las demás cosas serán añadidas a su debido tiempo. Amén.",
        "Lucas 1:37": "Dios todopoderoso, gracias por recordarme que contigo todo es posible. Fortalece mi fe cuando enfrento situaciones imposibles. Ayúdame a confiar en tu poder ilimitado. Amén.",
        "Hechos 20:35": "Señor generoso, ayúdame a vivir el principio de que más bendito es dar que recibir. Abre mis ojos a las necesidades de otros y dame un corazón servicial como el tuyo. Amén.",
        "Romanos 12:2": "Dios transformador, renueva mi mente según tu palabra. Ayúdame a no conformarme a este mundo sino a ser transformado para discernir tu voluntad perfecta. En el nombre de Cristo, Amén.",
        "1 Pedro 5:7": "Padre amoroso, te entrego todas mis ansiedades y preocupaciones. Gracias por tu promesa de que tienes cuidado de mí. Ayúdame a descansar en tu providencia fiel. Amén.",
        "1 Juan 4:8": "Dios de amor, ayúdame a conocerte más profundamente para poder amar como tú amas. Que tu amor fluya a través de mí hacia los demás. En el nombre de Jesús, Amén.",
        "Apocalipsis 21:4": "Dios de esperanza, gracias por la promesa de un futuro sin dolor ni lágrimas. Mientras espero ese día, ayúdame a vivir con la esperanza y la alegría de tu promesa. Amén."
    }
    
    return prayers.get(reference, "Señor, ayúdame a entender y aplicar esta verdad en mi vida hoy. Que tu palabra transforme mi corazón y mis acciones. En el nombre de Jesús, Amén.")

async def send_daily_verse():
    """Enviar versículo diario a todos los servidores"""
    if not config.VERSE_ENABLED or config.VERSE_CHANNEL_ID is None:
        return
    
    try:
        # Obtener versículo del día
        verse_data = await get_daily_verse()
        if not verse_data:
            logger.error("No se pudo obtener versículo diario")
            return
        
        # Crear embed
        embed = create_verse_embed(verse_data)
        
        # Enviar a todos los servidores donde está el bot
        sent_count = 0
        for guild in bot.guilds:
            try:
                channel = guild.get_channel(config.VERSE_CHANNEL_ID)
                if channel and isinstance(channel, discord.TextChannel):
                    await channel.send(embed=embed)
                    sent_count += 1
                    logger.info(f"Versículo enviado a {guild.name} en #{channel.name}")
                else:
                    logger.warning(f"No se encontró el canal de versículos en {guild.name}")
            except discord.Forbidden:
                logger.error(f"No tengo permisos para enviar mensajes en {guild.name}")
            except Exception as e:
                logger.error(f"Error enviando versículo a {guild.name}: {e}")
        
        logger.info(f"Versículo diario enviado a {sent_count} servidores")
        
    except Exception as e:
        logger.error(f"Error crítico enviando versículo diario: {e}")

async def schedule_daily_verse():
    """Programar envío diario de versículos"""
    while True:
        try:
            # Esperar a que el bot esté completamente listo
            await asyncio.sleep(10)
            
            # Obtener zona horaria configurada
            try:
                timezone = pytz.timezone(config.VERSE_TIMEZONE)
            except:
                timezone = pytz.timezone('America/Mexico_City')
                logger.warning(f"Zona horaria no válida, usando America/Mexico_City")
            
            # Parsear hora configurada
            try:
                hour, minute = map(int, config.VERSE_TIME.split(':'))
            except:
                hour, minute = 8, 0  # Por defecto 8:00 AM
                logger.warning(f"Hora no válida, usando 08:00")
            
            # Bucle infinito para programación diaria
            while True:
                # Obtener hora actual en la zona horaria configurada
                now = datetime.now(timezone)
                
                # Calcular próxima hora de envío
                next_send = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                # Si ya pasó la hora de hoy, programar para mañana
                if now >= next_send:
                    next_send += timedelta(days=1)
                
                # Calcular segundos hasta el próximo envío
                wait_seconds = (next_send - now).total_seconds()
                
                logger.info(f"Próximo versículo programado para: {next_send.strftime('%Y-%m-%d %H:%M:%S')} (en {wait_seconds/3600:.1f} horas)")
                
                # Esperar hasta la hora programada
                await asyncio.sleep(wait_seconds)
                
                # Enviar versículo
                await send_daily_verse()
                
                # Esperar un poco después del envío para evitar duplicados
                await asyncio.sleep(60)
                
        except Exception as e:
            logger.error(f"Error en programación de versículos: {e}")
            # Esperar 1 hora antes de reintentar
            await asyncio.sleep(3600)

async def cleanup_duplicate_sets():
    """Limpia periódicamente los sets para evitar crecimiento infinito"""
    while True:
        try:
            # Esperar 1 hora antes de cada limpieza
            await asyncio.sleep(3600)
            
            # Limpiar miembros que ya no están en los servidores
            current_members = set()
            for guild in bot.guilds:
                for member in guild.members:
                    current_members.add(f"{guild.id}_{member.id}")
            
            # Mantener solo los miembros actuales
            bot.welcome_sent.intersection_update(current_members)
            
            # Limpiar invites antiguos (mantener solo los últimos 1000)
            if len(bot.invite_sent) > 1000:
                # Convertir a lista y mantener los más recientes
                invite_list = list(bot.invite_sent)
                bot.invite_sent = set(invite_list[-1000:])
            
            logger.info(f"Limpieza completada: {len(bot.welcome_sent)} bienvenidas, {len(bot.invite_sent)} invites")
            
        except Exception as e:
            logger.error(f"Error en limpieza de sets duplicados: {e}")
            await asyncio.sleep(300)  # Esperar 5 minutos si hay error

def get_configured_channel(guild, channel_id, channel_name):
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if channel:
            return channel
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if channel:
        return channel
    normalized_name = re.sub(r"[^a-z0-9]", "", channel_name.lower())
    exact = None
    partial = None
    for candidate in guild.text_channels:
        normalized_candidate = re.sub(r"[^a-z0-9]", "", candidate.name.lower())
        if normalized_name == normalized_candidate:
            exact = candidate
            break
        if len(normalized_name) >= 6 and normalized_name in normalized_candidate and partial is None:
            partial = candidate
    return exact or partial

def _faq_normalize(text: str) -> str:
    text = (text or "").casefold()
    for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _faq_keyword_matches(content: str, keyword: str) -> bool:
    haystack = _faq_normalize(content)
    needle = _faq_normalize(keyword)
    if not haystack or not needle:
        return False
    if " " in needle:
        return needle in haystack
    return bool(re.search(rf"(^|\s){re.escape(needle)}s?(\s|$)", haystack))

async def maybe_send_faq(message) -> bool:
    if not config.FAQ_ENABLED or not message.guild:
        return False
    content = message.content or message.clean_content or ""
    if not content.strip():
        return False
    for topic in config.FAQ_TOPICS:
        if not any(_faq_keyword_matches(content, keyword) for keyword in topic["keywords"]):
            continue
        cooldown_key = (message.channel.id, topic["key"])
        now = discord.utils.utcnow().timestamp()
        last_sent = bot.faq_cooldown.get(cooldown_key, 0)
        if now - last_sent < config.FAQ_COOLDOWN_SECONDS:
            return True
        bot.faq_cooldown[cooldown_key] = now
        faq_embed = discord.Embed(
            description=topic["message"],
            color=config.COLOR_EMBED
        )
        faq_embed.set_footer(text="Nexus AI Help • NexusStore © Todos los derechos reservados")
        await message.channel.send(embed=faq_embed)
        logger.info(f"FAQ '{topic['key']}' enviado en #{getattr(message.channel, 'name', message.channel.id)}")
        return True
    return False

async def ticket_inactivity_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            category = discord.utils.get(guild.categories, name="TICKETS")
            if not category:
                continue
            for channel in category.text_channels:
                latest = [message async for message in channel.history(limit=1)]
                if not latest:
                    continue
                age_hours = (discord.utils.utcnow() - latest[0].created_at).total_seconds() / 3600
                if age_hours >= config.TICKET_CLOSE_HOURS:
                    await channel.delete(reason="Ticket cerrado por inactividad")
                    continue
                if age_hours >= config.TICKET_WARNING_HOURS and channel.id not in bot.ticket_warning_sent:
                    await channel.send("⚠️ Este ticket lleva 15 horas sin actividad y se cerrará automáticamente al cumplir 20 horas.")
                    bot.ticket_warning_sent.add(channel.id)
        await asyncio.sleep(600)

async def send_mod_log(guild, user, action, reason):
    channel = get_configured_channel(guild, config.MOD_LOG_CHANNEL_ID, config.MOD_LOG_CHANNEL_NAME)
    if not channel:
        return
    embed = discord.Embed(title="🛡️・MOD-LOGS", color=config.COLOR_EMBED)
    embed.add_field(name="👤 Usuario", value=user.mention, inline=False)
    embed.add_field(name="📄 Acción", value=action, inline=True)
    embed.add_field(name="🔎 Motivo", value=reason, inline=True)
    embed.add_field(name="🕐 Hora", value=discord.utils.format_dt(discord.utils.utcnow(), "t"), inline=True)
    await channel.send(embed=embed)

async def moderate_message(message):
    if not config.MODERATION_ENABLED or not message.guild:
        return False
    if message.author.guild_permissions.manage_messages or message.author.guild_permissions.administrator:
        return False
    content = message.content.lower()
    matched_bad_word = next((word for word in config.PROHIBITED_WORDS if word.lower() in content), None)
    matched_promotion = next((word for word in config.PROMOTION_WORDS if word.lower() in content), None)
    if matched_bad_word:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.author.timeout(timedelta(minutes=config.BAD_WORD_TIMEOUT_MINUTES), reason="Palabra prohibida")
            action = "TIMEOUT 10 MIN"
        except discord.Forbidden:
            action = "WARN (sin permiso para timeout)"
        sanction_embed = discord.Embed(
            title="🚫 NEXUS STOCK — SANCIÓN",
            description=(
                f"> **{message.author.mention}**, tu mensaje ha sido eliminado por utilizar lenguaje prohibido.\n"
                f"⚠️ **Sanción aplicada:** `{action}`\n\n"
                "Mantén el respeto dentro de la comunidad. La reincidencia puede aumentar la sanción.\n\n"
                "🔴 **Nexus Stock Staff**"
            ),
            color=config.COLOR_EMBED
        )
        sanction_embed.set_thumbnail(url=message.author.display_avatar.url)
        sanction_embed.set_footer(text="NexusStore © Todos los derechos reservados")
        sanction_embed.timestamp = discord.utils.utcnow()
        await message.channel.send(
            embed=sanction_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await send_mod_log(message.guild, message.author, action, "Palabra prohibida")
        return True
    if matched_promotion:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        key = (message.guild.id, message.author.id)
        warnings = bot.moderation_warnings.get(key, 0) + 1
        bot.moderation_warnings[key] = warnings
        if warnings >= config.PROMOTION_WARNINGS_BEFORE_BAN:
            try:
                await message.author.ban(reason="Promoción no autorizada: tercera sanción")
                action = "BAN PERMANENTE"
            except discord.Forbidden:
                action = "BAN FALLIDO (sin permiso para banear)"
        else:
            timeout_minutes = (
                config.PROMOTION_FIRST_TIMEOUT_MINUTES
                if warnings == 1
                else config.PROMOTION_SECOND_TIMEOUT_MINUTES
            )
            try:
                await message.author.timeout(
                    timedelta(minutes=timeout_minutes),
                    reason=f"Promoción no autorizada: sanción {warnings}/3"
                )
                action = f"TIMEOUT {timeout_minutes} MIN"
            except discord.Forbidden:
                action = f"TIMEOUT FALLIDO {timeout_minutes} MIN (sin permiso)"

        sanction_embed = discord.Embed(
            title="🚫 NEXUS STOCK — SANCIÓN",
            description=(
                f"> **{message.author.mention}**, tu mensaje ha sido eliminado automáticamente.\n"
                "La **venta, promoción o publicidad no autorizada** de cuentas, servidores de Discord, "
                "productos o servicios está **PROHIBIDA** dentro de Nexus Stock.\n\n"
                f"⚠️ **Sanción aplicada:** `{action}`\n\n"
                "🔴 No promociones cuentas, servidores ni servicios sin autorización del Staff.\n\n"
                "**Reincidir puede resultar en sanciones más severas.**"
            ),
            color=config.COLOR_EMBED
        )
        sanction_embed.set_thumbnail(url=message.author.display_avatar.url)
        sanction_embed.set_footer(text="NexusStore © Todos los derechos reservados")
        sanction_embed.timestamp = discord.utils.utcnow()
        await message.channel.send(
            embed=sanction_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await send_mod_log(message.guild, message.author, action, "Promoción no autorizada")
        return True
    return False

@bot.event
async def on_message(message):
    """Sistema anti-link, anti-spam y anti-flood y notificaciones de boost"""
    # Detectar notificaciones de boost del servidor
    if message.type in (
        discord.MessageType.premium_guild_subscription,
        discord.MessageType.premium_guild_tier_1,
        discord.MessageType.premium_guild_tier_2,
        discord.MessageType.premium_guild_tier_3
    ):
        try:
            boost_channel = message.guild.get_channel(config.BOOST_CHANNEL_ID)
            if boost_channel:
                embed = discord.Embed(
                    title="Nuevo Boost en el Servidor",
                    description=f"Muchas gracias a {message.author.mention} por mejorar el servidor con su Boost. ¡Tu apoyo nos ayuda a seguir creciendo!",
                    color=config.COLOR_EMBED
                )
                embed.set_thumbnail(url=message.author.display_avatar.url if message.author.display_avatar else None)
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                embed.timestamp = discord.utils.utcnow()
                await boost_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Error al enviar notificacion de boost: {e}")
        return

    # Ignorar mensajes del bot
    if message.author.bot:
        return

    if message.guild and config.REPUTATION_ENABLED and message.author.id != message.guild.owner_id:
        reputation_channel = get_configured_channel(message.guild, config.REPUTATION_CHANNEL_ID, config.REPUTATION_CHANNEL_NAME)
        if reputation_channel and message.channel.id == reputation_channel.id:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return

    # Sistema de vouches automáticos para Mayer y Noxy (solo detectar $)
    if message.guild and config.REPUTATION_ENABLED:
        # Verificar si el mensaje está en canales de vouches de amigos (excluir nexus)
        is_friend_vouch_channel = False
        for seller_name, channel_config in config.VOUCHES_CHANNELS.items():
            if seller_name == "nexus":
                continue  # Saltar canal de nexus (owner usa comando manual)
            vouch_channel = get_configured_channel(message.guild, channel_config["id"], channel_config["name"])
            if vouch_channel and message.channel.id == vouch_channel.id:
                is_friend_vouch_channel = True
                break
        
        if is_friend_vouch_channel and "$" in message.content:
            # Extraer el cliente mencionado en el vouch
            mentioned_users = message.mentions
            if mentioned_users:
                client = mentioned_users[0]  # Usar el primer usuario mencionado como cliente
                await register_purchase(message.guild, client, seller=seller_name.capitalize())
                logger.info(f"Vouch con $ detectado en canal de {seller_name}, compra registrada para {client.name} (ID: {client.id})")
            else:
                # Si no hay menciones, intentar registrar al autor del mensaje como cliente
                await register_purchase(message.guild, message.author, seller=seller_name.capitalize())
                logger.info(f"Vouch con $ detectado sin mención en canal de {seller_name}, compra registrada para autor {message.author.name} (ID: {message.author.id})")

    try:
        await maybe_send_faq(message)
    except Exception as e:
        logger.error(f"Error enviando FAQ automático: {e}")

    if message.guild and message.guild.owner_id in message.raw_mentions and message.author.id != message.guild.owner_id:
        mention_key = (message.guild.id, message.author.id)
        mention_count = bot.owner_mention_counts.get(mention_key, 0) + 1
        bot.owner_mention_counts[mention_key] = mention_count

        if mention_count in (2, 3):
            sanction_text = ""
            if mention_count == 3:
                try:
                    await message.author.timeout(
                        timedelta(minutes=5),
                        reason="Menciones repetidas al Owner"
                    )
                    sanction_text = "\n🚨 **Sanción aplicada:** `TIMEOUT 5 MIN`\n"
                    await send_mod_log(message.guild, message.author, "TIMEOUT 5 MIN", "Menciones repetidas al Owner")
                except discord.Forbidden:
                    sanction_text = "\n🚨 **Sanción aplicada:** `TIMEOUT FALLIDO (sin permisos)`\n"

            warning_embed = discord.Embed(
                title="⚠️ NEXUS STOCK — ADVERTENCIA",
                description=(
                    f"> **{message.author.mention}**, has mencionado al Owner varias veces.\n"
                    "📌 El **Owner revisará tu mensaje cuando pueda**.\n"
                    "🔔 **Mencionarlo una sola vez es más que suficiente.**\n\n"
                    "⚠️ Evita repetir las menciones innecesariamente.\n\n"
                    "🚨 **A la tercera mención:** se aplicará una **sanción de 5 minutos**.\n"
                    f"{sanction_text}\n"
                    "🔴 **Nexus Stock Staff**"
                ),
                color=config.COLOR_EMBED
            )
            warning_embed.set_thumbnail(url=message.author.display_avatar.url)
            warning_embed.set_footer(text="NexusStore © Todos los derechos reservados")
            warning_embed.timestamp = discord.utils.utcnow()
            await message.channel.send(
                embed=warning_embed,
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    if await moderate_message(message):
        return

    # Detectar respuesta de staff en canales de ticket
    if message.channel.name.startswith(("ticket-", "🎫")):
        staff_role = message.guild.get_role(config.SUPPORT_ROLE_ID) if config.SUPPORT_ROLE_ID else None
        admin_role = message.guild.get_role(config.ADMIN_ROLE_ID) if config.ADMIN_ROLE_ID else None
        is_staff = (staff_role and staff_role in message.author.roles) or (admin_role and admin_role in message.author.roles)
        if is_staff:
            staff_embed = discord.Embed(
                title="**¡NUEVA RESPUESTA DEL STAFF!**",
                description=(
                    f"Un miembro del equipo de Nexus Store ha respondido a tu solicitud.\n"
                    f"Por favor, revisa los mensajes de abajo para continuar con tu consulta o el proceso de tu pedido.\n\n"
                    f"**¿QUÉ HACER AHORA?**\n"
                    f"**Responde en este canal:** Lee con atención lo que ha indicado el staff y "
                    f"proporciona la información o capturas que te hayan solicitado.\n\n"
                    f"**¿Tu duda fue resuelta?:** Si ya finalizaste tu compra o se solucionó tu problema, "
                    f"puedes usar el botón de Cerrar Ticket para mantener el orden.\n\n"
                    f"**Estamos aquí para ayudarte. Por favor, mantén el chat activo hasta terminar el proceso.**"
                ),
                color=config.COLOR_EMBED
            )
            staff_embed.set_footer(text="NexusStore © Todos los derechos reservados")
            staff_embed.timestamp = discord.utils.utcnow()
            
            # Enviar al DM del creador del ticket (el usuario que no es staff)
            ticket_creator = None
            for target in message.channel.overwrites:
                if isinstance(target, discord.Member) and not target.bot:
                    ticket_creator = target
                    break
            
            if ticket_creator:
                try:
                    await ticket_creator.send(embed=staff_embed)
                except discord.Forbidden:
                    # Fallback si tiene los DMs cerrados
                    await message.channel.send(f"⚠️ {ticket_creator.mention}, tienes los mensajes directos desactivados, por lo que te notifico por aquí:", embed=staff_embed)
                except Exception as e:
                    logger.error(f"Error enviando DM de ticket a {ticket_creator}: {e}")
            else:
                # Si por alguna razón no se encuentra, enviarlo al canal
                await message.channel.send(embed=staff_embed)
    
    # Verificar si el sistema anti-link está activado
    if not config.ANTI_LINK_ENABLED:
        await bot.process_commands(message)
        return
    
    # Verificar si el usuario tiene permiso para enviar enlaces
    allowed_role = message.guild.get_role(config.ALLOWED_LINK_ROLE_ID)
    has_link_permission = allowed_role and allowed_role in message.author.roles
    
    # Sistema anti-flood - tracking de mensajes
    user_id = message.author.id
    current_time = discord.utils.utcnow().timestamp()
    
    # Inicializar tracking de usuario si no existe
    if user_id not in bot.user_messages:
        bot.user_messages[user_id] = []
    
    # Limpiar mensajes antiguos (más de 10 segundos)
    bot.user_messages[user_id] = [
        msg_time for msg_time in bot.user_messages[user_id] 
        if current_time - msg_time < 10
    ]
    
    # Añadir mensaje actual
    bot.user_messages[user_id].append(current_time)
    
    # Detectar flood (más de 5 mensajes en 10 segundos)
    if len(bot.user_messages[user_id]) > 5:
        try:
            await message.delete()
            
            # Evitar advertencias duplicadas de flood
            flood_key = f"flood_{user_id}_{message.channel.id}"
            if flood_key not in bot.flood_warnings:
                bot.flood_warnings.add(flood_key)
                
                warning_embed = discord.Embed(
                    title="Flood Detectado",
                    description=f"**{message.author.mention}**, estás enviando mensajes demasiado rápido.\n\nPor favor, reduce la velocidad de envío de mensajes.\n\nEste mensaje se eliminará en 5 segundos.",
                    color=config.COLOR_EMBED
                )
                warning_embed.set_footer(text="NexusStore © Todos los derechos reservados")
                warning_embed.timestamp = discord.utils.utcnow()
                
                warning_msg = await message.channel.send(embed=warning_embed)
                
                # Eliminar advertencia después de 5 segundos
                await asyncio.sleep(5)
                try:
                    await warning_msg.delete()
                    bot.flood_warnings.discard(flood_key)
                except:
                    pass
            
            logger.info(f"Flood detectado de {message.author.name} en #{message.channel.name}")
            return
            
        except discord.Forbidden:
            logger.warning(f"No tengo permisos para eliminar mensajes en #{message.channel.name}")
            return
        except Exception as e:
            logger.error(f"Error en sistema anti-flood: {e}")
            return
    
    # Detectar imágenes y adjuntos
    has_media = len(message.attachments) > 0 or len(message.embeds) > 0
    
    # Lista de patrones de enlaces a detectar
    link_patterns = [
        'http://', 'https://', 'www.', '.com', '.net', '.org', '.io', '.gg',
        '.xyz', '.tk', '.ml', '.ga', '.cf', '.gq', '.ddns.net', '.discord.gg',
        'discord.com/', 'discord.gg/', 't.me/', 'telegram.me/', 'bit.ly/',
        'tinyurl.com/', 'goo.gl/', 'cutt.ly/', 'short.link/', '.png', '.jpg',
        '.jpeg', '.gif', '.webp', '.mp4', '.avi', '.mov', '.zip', '.rar',
        '.exe', '.bat', '.scr', '.php', '.html', '.js', '.css', '.xml'
    ]
    
    # Verificar si el mensaje contiene enlaces o patrones de spam
    message_content = message.content.lower()
    contains_link = any(pattern in message_content for pattern in link_patterns)
    
    # Detectar spam repetido (mismo mensaje 3+ veces)
    if user_id in bot.user_messages and len(bot.user_messages[user_id]) >= 3:
        recent_messages = bot.user_messages[user_id][-3:]
        # Aquí podríamos implementar detección de contenido duplicado si guardamos los mensajes
    
    # También verificar si hay URLs usando regex básico
    import re
    url_pattern = r'https?://(?:[-\w.])+(?:[:\d]+)?(?:/(?:[\w/_.])*(?:\?(?:[\w&=%.])*)?(?:#(?:\w*))?)?'
    if re.search(url_pattern, message_content, re.IGNORECASE):
        contains_link = True
    
    # Sistema Anti-Phishing - Detectar enlaces maliciosos
    is_phishing = False
    is_ip_logger = False
    is_invite_spam = False
    
    if config.ANTI_PHISHING_ENABLED:
        # Verificar enlaces de phishing
        for domain in bot.phishing_links:
            if domain in message_content:
                is_phishing = True
                break
    
    if config.ANTI_IP_LOGGER_ENABLED:
        # Verificar IP loggers
        for domain in bot.ip_logger_links:
            if domain in message_content:
                is_ip_logger = True
                break
    
    if config.ANTI_INVITE_SPAM_ENABLED:
        # Verificar spam de invitaciones de otros servidores
        import re
        # Detectar invitaciones que no sean del servidor actual
        invite_pattern = r'(discord\.(gg|com/invite|io)|dsc\.gg|dis\.gd|invite\.gg)/[a-zA-Z0-9]+'
        invites = re.findall(invite_pattern, message_content)
        
        if invites:
            # Verificar si las invitaciones son de otros servidores
            for invite_code in re.findall(r'discord\.(gg|com/invite|io)/([a-zA-Z0-9]+)', message_content):
                try:
                    # Intentar resolver la invitación para ver si es de otro servidor
                    invite = await bot.fetch_invite(invite_code[1])
                    if invite.guild.id != message.guild.id:
                        is_invite_spam = True
                        break
                except:
                    # Si no podemos resolver, asumimos que es spam
                    is_invite_spam = True
                    break
    
    # Detectar patrones de spam tradicionales
    spam_patterns = [
        'free nitro', 'nitro gratis', 'regalo nitro', 'gift nitro',
        'steam gift', 'regalo steam', 'free steam', 'steam gratis',
        'discord nitro', 'nitro free', 'get nitro', 'claim nitro',
        'click here', 'haz clic', 'click agora', 'clique aqui',
        'win money', 'gana dinero', 'ganhe dinheiro', 'earn money',
        'crypto', 'bitcoin', 'ethereum', 'criptomoneda'
    ]
    
    contains_spam = any(pattern in message_content for pattern in spam_patterns)
    
    # Si se detecta cualquier tipo de amenaza y el usuario no tiene permiso
    violation_detected = (
        contains_link or has_media or contains_spam or 
        is_phishing or is_ip_logger or is_invite_spam
    ) and not has_link_permission
    
    if violation_detected:
        try:
            # Eliminar el mensaje
            await message.delete()
            
            # Determinar tipo de violación y mensaje específico
            if is_phishing:
                violation_type = "Phishing Detectado"
                description = f"**{message.author.mention}**, se ha detectado un enlace de phishing en tu mensaje.\n\nPeligro: Este enlace intenta robar tu información personal.\n\nEste tipo de contenido está estrictamente prohibido y puede resultar en baneo permanente.\n\nSi crees que esto es un error, contacta inmediatamente a un administrador."
                embed_color=config.COLOR_EMBED
            elif is_ip_logger:
                violation_type = "IP Logger Detectado"
                description = f"**{message.author.mention}**, se ha detectado un IP logger en tu mensaje.\n\nPeligro: Este enlace intenta rastrear tu ubicación y dirección IP.\n\nEste tipo de contenido está estrictamente prohibido y puede resultar en baneo permanente.\n\nSi crees que esto es un error, contacta inmediatamente a un administrador."
                embed_color=config.COLOR_EMBED
            elif is_invite_spam:
                violation_type = "Spam de Invitaciones"
                description = f"**{message.author.mention}**, se ha detectado spam de invitaciones a otros servidores.\n\nNo está permitido promocionar otros servidores de Discord.\n\nSi quieres promocionar un servidor, contacta a un administrador para obtener permiso."
                embed_color=config.COLOR_EMBED
            elif contains_spam:
                violation_type = "Spam Detectado"
                description = f"**{message.author.mention}**, se ha detectado contenido de spam en tu mensaje.\n\nEste tipo de contenido no está permitido en el servidor.\nSi crees que esto es un error, contacta a un administrador."
                embed_color=config.COLOR_EMBED
            elif has_media:
                violation_type = "Archivo/Imagen Detectado"
                description = f"**{message.author.mention}**, no tienes permiso para enviar archivos o imágenes.\n\nPara poder enviar archivos, necesitas el rol **<@&{config.ALLOWED_LINK_ROLE_ID}>**.\nSi crees que esto es un error, contacta a un administrador."
                embed_color=config.COLOR_EMBED
            else:
                violation_type = "Enlace Detectado | Nexus Store"
                description = f"**{message.author.mention}**, **No Peudes Enviar Enlaces En Este Servidor.**\nPara poder enviar enlaces, necesitas Autorizacion.\n\nSi crees que esto es un error, contacta a un administrador."
                embed_color=config.COLOR_EMBED
            
            # Enviar advertencia al usuario
            warning_embed = discord.Embed(
                title=violation_type,
                description=description,
                color=embed_color
            )
            warning_embed.set_footer(text="NexusStore © Todos los derechos reservados")
            warning_embed.timestamp = discord.utils.utcnow()
            
            # Enviar advertencia temporal que se autoelimina
            warning_msg = await message.channel.send(embed=warning_embed)
            
            # Eliminar la advertencia después de 10 segundos
            await asyncio.sleep(10)
            try:
                await warning_msg.delete()
            except:
                pass
            
            violation_type_log = "spam" if contains_spam else ("media" if has_media else "enlace")
            logger.info(f"{violation_type_log} eliminado de {message.author.name} en #{message.channel.name}")
            
        except discord.Forbidden:
            logger.warning(f"No tengo permisos para eliminar mensajes en #{message.channel.name}")
        except Exception as e:
            logger.error(f"Error en sistema anti-spam: {e}")
    else:
        await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    """Sistema anti-raid y bienvenida - protege contra ataques coordinados"""
    try:
        # Sistema Anti-Raid - Verificar edad de cuenta
        if config.ANTI_RAID_ENABLED:
            account_age = discord.utils.utcnow() - member.created_at
            min_age_days = config.MIN_ACCOUNT_AGE_DAYS
            
            if account_age.days < min_age_days:
                # Cuenta demasiado nueva, posible raid
                try:
                    await member.kick(reason=f"Cuenta demasiado nueva ({account_age.days} días < {min_age_days} días requeridos)")
                    logger.warning(f"Cuenta nueva expulsada: {member.name} ({account_age.days} días)")
                    
                    # Notificar al canal de logs si existe
                    log_channel = member.guild.get_channel(1497697455695724564)  # ID del canal de logs
                    if log_channel:
                        embed = discord.Embed(
                            title="Cuenta Nueva Expulsada",
                            description=f"**{member.name}** ha sido expulsado automáticamente.\n\n**Edad de cuenta:** {account_age.days} días\n**Requerido:** {min_age_days} días",
                            color=config.COLOR_EMBED
                        )
                        embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else None)
                        embed.set_footer(text="Sistema Anti-Raid NexusStore")
                        await log_channel.send(embed=embed)
                    return
                except discord.Forbidden:
                    logger.error(f"No pude expulsar a {member.name} - falta permisos")
            
            # Tracking de joins para detectar raid
            current_time = discord.utils.utcnow().timestamp()
            bot.raid_tracking.append((member.id, current_time))
            
            # Limpiar joins antiguos (más de la ventana de tiempo)
            bot.raid_tracking = [
                (uid, time) for uid, time in bot.raid_tracking 
                if current_time - time < config.RAID_TIME_WINDOW
            ]
            
            # Detectar posible raid
            if len(bot.raid_tracking) >= config.RAID_THRESHOLD_USERS:
                logger.warning(f"RAID DETECTADO: {len(bot.raid_tracking)} usuarios entraron en {config.RAID_TIME_WINDOW} segundos")
                
                # Activar modo raid - podría incluir lockdown automático
                log_channel = member.guild.get_channel(1497697455695764)
                if log_channel:
                    embed = discord.Embed(
                        title="RAID DETECTADO",
                        description=f"Se han detectado **{len(bot.raid_tracking)}** nuevos usuarios entrando en **{config.RAID_TIME_WINDOW}** segundos.\n\n**Posible ataque coordinado** en curso.\n\n**Acciones recomendadas:**\n• Activar modo lockdown\n• Verificar nuevos miembros\n• Considerar verificación manual",
                        color=config.COLOR_EMBED
                    )
                    embed.add_field(name="Usuarios Recientes", value=f"Últimos {config.RAID_THRESHOLD_USERS} joins", inline=True)
                    embed.add_field(name="Ventana de Tiempo", value=f"{config.RAID_TIME_WINDOW} segundos", inline=True)
                    embed.set_footer(text="Sistema Anti-Raid NexusStore")
                    await log_channel.send(embed=embed)
        
        # Continuar con la bienvenida normal si pasó la verificación anti-raid
        # Crear ID único para este miembro en este servidor
        member_key = f"{member.guild.id}_{member.id}"
        
        # Verificar si ya se envió el mensaje de bienvenida (duplicado estricto)
        if member_key in bot.welcome_sent:
            return  # Ya se procesó, evitar duplicados
        
        # Marcar como procesado inmediatamente para evitar race conditions
        bot.welcome_sent.add(member_key)
        
        # Pequeña espera para asegurar que Discord procese el evento
        await asyncio.sleep(0.5)
        
        # Intentar múltiples variaciones del nombre del rol
        role_variations = [
            config.AUTO_ROLE_NAME,
            "MIEMBROS", 
            "MIEMBRO", 
            "MEMBER", 
            "MEMBERS",
            "Miembros",
            "Miembro"
        ]
        
        role = member.guild.get_role(config.AUTO_ROLE_ID) if config.AUTO_ROLE_ID is not None else None
        
        if role is None:
            # Buscar rol por nombre con múltiples variaciones
            for variation in role_variations:
                role = discord.utils.find(
                    lambda candidate: candidate.name.strip().casefold() == variation.casefold(),
                    member.guild.roles
                )
                if role:
                    logger.info(f"Rol encontrado con variación: {variation} -> {role.name}")
                    break
        
        if role:
            try:
                await member.add_roles(role, reason="Rol automático al entrar al servidor")
                logger.info(f"Rol '{role.name}' asignado a {member.name} al unirse")
            except discord.Forbidden:
                logger.error(f"No tengo permisos para asignar el rol {role.name} a {member}. Verifica que el bot tenga el permiso 'Manage Roles'.")
        else:
            logger.error(
                f"No se encontró ningún rol automático en {member.guild.name}. Buscando: {role_variations}. Roles disponibles: {[r.name for r in member.guild.roles[:10]]}"
            )

        welcome_channel = get_configured_channel(member.guild, config.WELCOME_CHANNEL_ID, config.WELCOME_CHANNEL_NAME)
        if welcome_channel:
            rules_channel = get_configured_channel(member.guild, config.RULES_CHANNEL_ID, config.RULES_CHANNEL_NAME)
            chat_channel = get_configured_channel(member.guild, config.CHAT_CHANNEL_ID, config.CHAT_CHANNEL_NAME)
            vouches_channel = get_configured_channel(member.guild, config.VOUCHES_CHANNEL_ID, config.VOUCHES_CHANNEL_NAME)
            rules_mention = rules_channel.mention if rules_channel else "#reglas"
            chat_mention = chat_channel.mention if chat_channel else "#chat"
            vouches_mention = vouches_channel.mention if vouches_channel else "#vouches"
            embed = discord.Embed(
                title="🔴 NEXUS STOCK",
                description=(
                    f"> Bienvenido/a a **Nexus Stock**, {member.mention}.\n"
                    "> Has entrado a nuestra comunidad oficial.\n"
                    f"📌 Lee las reglas: {rules_mention}\n"
                    f"💬 Respeta a los demás miembros en {chat_mention}.\n\n"
                    "⚠️ **Recuerda:** el incumplimiento de las reglas puede resultar en una sanción.\n\n"
                    f"⭐ Después de comprar, deja tu vouch en {vouches_mention}.\n\n"
                    "**Disfruta tu estadía en Nexus Stock.**"
                ),
                color=config.COLOR_EMBED
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            embed.timestamp = discord.utils.utcnow()
            await welcome_channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True)
            )
        else:
            logger.warning(
                f"No se encontró el canal de bienvenida en {member.guild.name}. "
                f"Nombre configurado: {config.WELCOME_CHANNEL_NAME}"
            )
        
        # Mensaje de invitaciones desactivado
        # try:
        #     inviter = await get_inviter(member)
        #     
        #     # Enviar mensaje de invitación si hay un inviter y canal configurado
        #     if inviter and config.INVITE_LOG_CHANNEL_ID is not None:
        #         await send_invite_log(member, inviter)
        # except Exception as e:
        #     logger.warning(f"Error obteniendo información de invitación: {e}")
        
    except Exception as e:
        logger.error(f"Error crítico procesando entrada de {member.name}: {type(e).__name__}: {e}")

@bot.event
async def on_member_remove(member):
    goodbye_channel = get_configured_channel(member.guild, config.GOODBYE_CHANNEL_ID, config.GOODBYE_CHANNEL_NAME)
    if goodbye_channel:
        ban_data = bot.recent_bans.pop((member.guild.id, member.id), None)
        if ban_data:
            embed = discord.Embed(
                title="🔨 USUARIO BANEADO",
                description=(
                    f"> **Usuario:** `{member}`\n"
                    f"> **ID:** `{member.id}`\n"
                    f"> **Razón:** `{ban_data['reason']}`\n"
                    f"> **Moderador:** `{ban_data['moderator']}`\n\n"
                    "⛔ El usuario ha sido **baneado permanentemente** del servidor.\n\n"
                    "**NEXUS STOCK** • Sistema de Moderación"
                ),
                color=config.COLOR_EMBED
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text="Nexus Stock © Todos los derechos reservados")
        else:
            embed = discord.Embed(
                title="🔻 NEXUS STOCK",
                description=(
                    "> 🔻 **NEXUS STOCK**\n\n"
                    f"**{member.display_name}** ha abandonado nuestra comunidad.\n\n"
                    "🖤 Gracias por haber formado parte de **Nexus Stock**.\n\n"
                    "Esperamos volver a verte pronto.\n\n"
                    "📩 ¿Necesitas volver a comprar o solicitar soporte?\n\n"
                    "Nuestras puertas siempre estarán abiertas.\n\n"
                    "**— NEXUS STOCK**\n\n"
                    "🔴 *Quality Services • Trusted Community*"
                ),
                color=config.COLOR_EMBED
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text="Nexus Stock © Todos los derechos reservados")
        embed.timestamp = discord.utils.utcnow()
        await goodbye_channel.send(embed=embed)
    else:
        logger.warning(
            f"No se encontró el canal de despedidas en {member.guild.name}. "
            f"Nombre configurado: {config.GOODBYE_CHANNEL_NAME}"
        )

async def get_inviter(member):
    """Obtiene información sobre quién invitó al miembro"""
    try:
        # Obtener todas las invitaciones del servidor
        invites = await member.guild.invites()
        
        # Guardar los usos actuales de cada invitación
        if not hasattr(bot, 'invite_uses'):
            bot.invite_uses = {}
        
        inviter = None
        
        # Comparar con los usos guardados para encontrar cuál se usó
        for invite in invites:
            if invite.inviter.id not in bot.invite_uses:
                bot.invite_uses[invite.inviter.id] = invite.uses
            
            if invite.uses > bot.invite_uses[invite.inviter.id]:
                inviter = invite.inviter
                bot.invite_uses[invite.inviter.id] = invite.uses
                break
        
        # Actualizar todos los usos
        for invite in invites:
            bot.invite_uses[invite.inviter.id] = invite.uses
        
        return inviter
        
    except discord.Forbidden:
        print("No tengo permisos para ver las invitaciones del servidor")
        return None
    except Exception as e:
        print(f"Error al obtener inviter: {e}")
        return None

async def send_invite_log(member, inviter):
    """Envía un mensaje al canal de logs cuando alguien invita a un nuevo miembro"""
    try:
        # Crear ID único para esta invitación
        invite_key = f"{member.guild.id}_{member.id}_{inviter.id}"
        
        # Verificar si ya se envió el mensaje de invitación
        if invite_key in bot.invite_sent:
            return  # Ya se procesó, evitar duplicados
        
        # Marcar como procesado inmediatamente
        bot.invite_sent.add(invite_key)
        
        invite_channel = member.guild.get_channel(config.INVITE_LOG_CHANNEL_ID)
        
        if invite_channel is None:
            print(f"No se encontró el canal de logs de invitaciones con ID {config.INVITE_LOG_CHANNEL_ID}")
            return
        
        # Crear embed de invitación
        embed = discord.Embed(
            title="Nueva Invitación",
            description=f"**{inviter.mention}** ha invitado a **{member.mention}** al servidor.",
            color=config.COLOR_EMBED
        )
        
        embed.add_field(
            name="Información de la Invitación",
            value=f"**Invitado por:** {inviter.name}#{inviter.discriminator}\n**Nuevo miembro:** {member.name}#{member.discriminator}\n**Total de miembros:** {member.guild.member_count}",
            inline=False
        )
        
        embed.add_field(
            name="Estadísticas del Invitador",
            value=f"**ID del invitador:** {inviter.id}\n**Se unió el:** {inviter.joined_at.strftime('%d/%m/%Y') if hasattr(inviter, 'joined_at') and inviter.joined_at else 'N/A'}",
            inline=True
        )
        
        embed.add_field(
            name="Rol del Invitador",
            value=f"**Roles:** {len(inviter.roles)} roles\n**Rol principal:** {inviter.top_role.name}",
            inline=True
        )
        
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"NexusStore © Todos los derechos reservados")
        embed.timestamp = discord.utils.utcnow()
        
        await invite_channel.send(embed=embed)
        print(f"Log de invitación enviado: {inviter.name} invitó a {member.name}")
        
    except discord.Forbidden:
        print(f"No tengo permisos para enviar mensajes al canal de logs de invitaciones")
    except Exception as e:
        print(f"Error al enviar log de invitación: {type(e).__name__}: {e}")

@bot.tree.command(name="panel_tickets", description="Muestra el panel de soporte para abrir tickets")
@has_admin_role()
async def panel_tickets(interaction: discord.Interaction):
    """Muestra el panel de soporte para abrir tickets"""
    print(f"Ejecutando panel_tickets por: {interaction.user.name}")
    
    # Crear el embed con el color personalizado
    embed = discord.Embed(
        title="Nexus Stock • Tienda Oficial",
        description=config.TICKET_PANEL_MESSAGE,
        color=config.COLOR_EMBED
    )
    embed.set_thumbnail(url=config.TICKET_PANEL_IMAGE)
    embed.set_image(url=config.TICKET_PANEL_BANNER)
    
    # Añadir footer
    embed.set_footer(text=config.TICKET_PANEL_FOOTER)
    
    view = TicketPanelSelectView()
    
    # Enviar el mensaje al canal
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Panel de tickets enviado correctamente", ephemeral=True)

@bot.tree.command(name="publicar_reglas", description="Publica las reglas oficiales en este canal")
@has_admin_role()
async def publicar_reglas(interaction: discord.Interaction):
    rules_embed = discord.Embed(
        description=config.RULES_MESSAGE,
        color=config.COLOR_EMBED
    )
    rules_embed.set_footer(text="NexusStore © Todos los derechos reservados")
    await interaction.channel.send(
        embed=rules_embed,
        allowed_mentions=discord.AllowedMentions(everyone=True)
    )
    await interaction.response.send_message("Reglas publicadas correctamente.", ephemeral=True)

@bot.tree.command(name="banear", description="Banea permanentemente a un usuario del servidor")
@has_admin_role()
@app_commands.describe(usuario="Usuario que será baneado", razon="Motivo del baneo")
async def banear(interaction: discord.Interaction, usuario: discord.Member, razon: str):
    """Banea a un miembro y prepara el aviso en el canal de despedidas."""
    if usuario == interaction.user:
        await interaction.response.send_message("No puedes banearte a ti mismo.", ephemeral=True)
        return

    if usuario == interaction.guild.owner:
        await interaction.response.send_message("No puedes banear al dueño del servidor.", ephemeral=True)
        return

    bot_member = interaction.guild.me
    if bot_member and usuario.top_role >= bot_member.top_role:
        await interaction.response.send_message(
            "No puedo banear a ese usuario porque su rol está igual o por encima del mío.",
            ephemeral=True
        )
        return

    bot.recent_bans[(interaction.guild.id, usuario.id)] = {
        "reason": razon,
        "moderator": interaction.user,
    }
    try:
        await usuario.ban(reason=f"{razon} | Moderador: {interaction.user}")
    except discord.Forbidden:
        bot.recent_bans.pop((interaction.guild.id, usuario.id), None)
        await interaction.response.send_message(
            "No tengo permisos para banear a ese usuario.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ {usuario} fue baneado permanentemente. El aviso se publicó en despedidas.",
        ephemeral=True
    )

@bot.tree.command(name="timeout", description="Silencia temporalmente a un usuario")
@has_admin_role()
@app_commands.describe(
    usuario="Usuario que recibirá el timeout",
    duracion="Duración del timeout (ej. 10m, 2h, 1d)",
    razon="Motivo del timeout (opcional)"
)
async def timeout_cmd(
    interaction: discord.Interaction,
    usuario: discord.Member,
    duracion: str,
    razon: Optional[str] = "No especificado"
):
    """Aplica un timeout temporal a un miembro."""
    if usuario == interaction.user:
        await interaction.response.send_message("No puedes ponerte timeout a ti mismo.", ephemeral=True)
        return

    if usuario == interaction.guild.owner:
        await interaction.response.send_message("No puedes ponerle timeout al dueño del servidor.", ephemeral=True)
        return

    bot_member = interaction.guild.me
    if bot_member and usuario.top_role >= bot_member.top_role:
        await interaction.response.send_message(
            "No puedo aplicar timeout a ese usuario porque su rol está igual o por encima del mío.",
            ephemeral=True
        )
        return

    seconds = parse_duration(duracion)
    if not seconds:
        await interaction.response.send_message(
            "Formato de duración inválido. Usa s (segundos), m (minutos), h (horas) o d (días). Ej. `10m`, `2h`.",
            ephemeral=True
        )
        return

    if seconds > 28 * 86400:
        await interaction.response.send_message("El timeout máximo permitido por Discord es de 28 días.", ephemeral=True)
        return

    try:
        await usuario.timeout(
            timedelta(seconds=seconds),
            reason=f"{razon} | Moderador: {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message("No tengo permisos para aplicar timeout a ese usuario.", ephemeral=True)
        return

    await send_mod_log(interaction.guild, usuario, f"TIMEOUT {format_vote_duration(seconds)}", razon)
    await interaction.response.send_message(
        f"✅ {usuario} recibió timeout por `{format_vote_duration(seconds)}`. Razón: {razon}",
        ephemeral=True
    )

@bot.command(name="embed")
@commands.has_role(config.ADMIN_ROLE_ID)
async def embed_cmd(ctx, titulo: str, *, resto: str):
    """Crea un embed personalizado. Uso: !embed "TITULO" descripcion url_banner"""
    partes = resto.rsplit(" ", 1)
    # Detectar si la última parte es una URL de imagen
    if len(partes) == 2 and (partes[1].startswith("http://") or partes[1].startswith("https://")):
        descripcion = partes[0]
        banner_url = partes[1]
    else:
        descripcion = resto
        banner_url = None

    embed = discord.Embed(
        title=titulo,
        description=descripcion,
        color=config.COLOR_EMBED
    )
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()

    await ctx.send(embed=embed)
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command(name="dmall")
@commands.has_role(config.ADMIN_ROLE_ID)
async def dmall(ctx):
    """Envía un mensaje por DM a todos los miembros del servidor con el banner especificado."""
    embed = discord.Embed(
        title="**¡ATENCIÓN A TODA LA COMUNIDAD DE NEXUS STORE!**",
        description=(
            "**¡Pon tu Dm** \n\n"
        ),
        color=config.COLOR_EMBED
    )
    sent = 0
    failed = 0
    for member in ctx.guild.members:
        if member.bot:
            continue
        try:
            await member.send(embed=embed)
            sent += 1
        except discord.Forbidden:
            failed += 1
        except Exception as e:
            logger.error(f"Error enviando DM a {member}: {e}")
            failed += 1
    await ctx.send(f"✅ Mensaje enviado a {sent} usuarios. No se pudieron enviar a {failed} usuarios (DMs desactivados).")

@bot.tree.command(name="cerrar", description="Cierra el ticket actual")
async def cerrar_ticket(interaction: discord.Interaction):
    """Cierra el ticket actual"""
    # Verificar si el canal es un ticket
    if not (interaction.channel.name.startswith(("ticket-", "🎫")) or 
            (interaction.channel.category and interaction.channel.category.name.upper() == "TICKETS")):
        await interaction.response.send_message("❌ Este comando solo puede usarse en canales de tickets.", ephemeral=True)
        return
    
    # Verificar permisos
    allowed_role_names = {"founder", "creator", "admin", "bots", "staff"}
    user_role_names = {
        re.sub(r"[^a-z0-9]", "", role.name.lower())
        for role in interaction.user.roles
    }
    can_close_ticket = (
        interaction.user.id == interaction.guild.owner_id
        or interaction.user.guild_permissions.administrator
        or any(
            allowed_name in role_name
            for role_name in user_role_names
            for allowed_name in allowed_role_names
        )
    )
    
    if not can_close_ticket:
        await interaction.response.send_message("❌ Solo Owner, Founder, Creator, Admin, Bots o Staff puede cerrar tickets.", ephemeral=True)
        return
    
    await interaction.response.send_message("🔒 El ticket se cerrará y este canal se eliminará en 5 segundos...")
    
    # Embed de cierre
    close_embed = discord.Embed(
        title="Ticket Cerrado | Nexus Store",
        description=(
            f"**Este ticket ha sido cerrado por {interaction.user.mention}.**\n\n"
            f"Gracias por contactar con el equipo de Nexus Store.\n"
            f"Si necesitas ayuda nuevamente, no dudes en abrir un nuevo ticket.\n\n"
            f"*Nexus Store — Manteniendo la comunidad segura y organizada.*"
        ),
        color=config.COLOR_EMBED
    )
    close_embed.set_footer(text="NexusStore © Todos los derechos reservados")
    close_embed.timestamp = discord.utils.utcnow()
    await interaction.channel.send(embed=close_embed)
    await asyncio.sleep(5)
    try:
        await interaction.channel.delete()
    except Exception as e:
        await interaction.followup.send(f"Error al eliminar el canal: {e}", ephemeral=True)

@bot.tree.command(name="vouch", description="Publica un vouch con cliente, vendedor, comentario y foto de la compra")
@has_voucher_permission()
@app_commands.describe(
    cliente="Cliente que compró la cuenta",
    vendedor="Vendedor de la cuenta (tú, Mayer o Noxy)",
    producto="Producto comprado", 
    comentario="Comentario sobre la compra (ej. 10 de 10)",
    imagen="Foto o captura de la compra (opcional)"
)
@app_commands.choices(
    vendedor=[
        app_commands.Choice(name="Nexus", value="nexus"),
        app_commands.Choice(name="Mayer", value="mayer"),
        app_commands.Choice(name="Noxy", value="noxy")
    ]
)
async def vouch(
    interaction: discord.Interaction, 
    cliente: discord.Member,
    vendedor: app_commands.Choice[str],
    producto: str, 
    comentario: str, 
    imagen: Optional[discord.Attachment] = None
):
    # Siempre publicar en el canal de Nexus (canal principal del usuario)
    channel = get_configured_channel(interaction.guild, config.VOUCHES_CHANNEL_ID, config.VOUCHES_CHANNEL_NAME)
    
    if not channel:
        await interaction.response.send_message("No existe el canal de vouches principal configurado.", ephemeral=True)
        return
    if imagen and (not imagen.content_type or not imagen.content_type.startswith("image/")):
        await interaction.response.send_message("La evidencia debe ser una imagen.", ephemeral=True)
        return
    vouch_number = await get_next_vouch_number(channel)
    embed = discord.Embed(title="⭐ NUEVO VOUCH", color=config.COLOR_EMBED)
    embed.add_field(name="👤 Cliente", value=cliente.mention, inline=False)
    embed.add_field(name="🏪 Vendedor", value=vendedor.name, inline=False)
    embed.add_field(name="🛒 Producto", value=producto, inline=False)
    embed.add_field(name="💰 Compra", value="Completada", inline=False)
    embed.add_field(name="💬 Comentario", value=f'“{comentario}”', inline=False)
    if imagen:
        embed.set_image(url=imagen.url)
    embed.set_footer(text=f"NEXUS • Vouch #{vouch_number}")
    embed.timestamp = discord.utils.utcnow()
    
    # Determinar la mención correcta según el vendedor
    seller_name = vendedor.value
    seller_mention = interaction.guild.owner.mention  # Por defecto owner (Nexus)
    
    if seller_name == "mayer":
        # Buscar usuario Mayer por rol o nombre
        mayer_role = discord.utils.get(interaction.guild.roles, name="Mayer")
        if mayer_role:
            mayer_member = discord.utils.find(lambda m: mayer_role in m.roles, interaction.guild.members)
            seller_mention = mayer_member.mention if mayer_member else "@Mayer"
        else:
            seller_mention = "@Mayer"
    elif seller_name == "noxy":
        # Buscar usuario Noxy por rol o nombre
        noxy_role = discord.utils.get(interaction.guild.roles, name="Noxy")
        if noxy_role:
            noxy_member = discord.utils.find(lambda m: noxy_role in m.roles, interaction.guild.members)
            seller_mention = noxy_member.mention if noxy_member else "@Noxy"
        else:
            seller_mention = "@Noxy"
    elif seller_name == "magin":
        # Buscar usuario Magin por rol o nombre
        magin_role = discord.utils.get(interaction.guild.roles, name="Magin")
        if magin_role:
            magin_member = discord.utils.find(lambda m: magin_role in m.roles, interaction.guild.members)
            seller_mention = magin_member.mention if magin_member else "@Magin"
        else:
            seller_mention = "@Magin"
    
    await channel.send(content=f"✅ +1 VOUCH {seller_mention}", embed=embed)
    if "$" in producto or "$" in comentario:
        await register_purchase(interaction.guild, cliente, seller=vendedor.name)
    await interaction.response.send_message("✅ Tu vouch fue publicado.", ephemeral=True)

@bot.tree.command(name="top_compradores", description="Muestra el top de compradores de Nexus Stock")
async def top_compradores(interaction: discord.Interaction):
    reputation = load_reputation()
    top = _top_purchasers(reputation, config.LEADERBOARD_SIZE)
    if not top:
        await interaction.response.send_message("Todavía no hay compras registradas.", ephemeral=True)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, (uid, count) in enumerate(top, start=1):
        member = interaction.guild.get_member(int(uid))
        display = member.mention if member else f"Usuario ({uid})"
        rank_icon = medals[index - 1] if index <= 3 else f"`#{index}`"
        lines.append(f"{rank_icon} {display} — **{count}** cuentas")

    embed = discord.Embed(
        title="🏆 TOP COMPRADORES — NEXUS STOCK",
        description="\n".join(lines),
        color=config.COLOR_EMBED
    )
    embed.set_footer(text="Nexus Stock © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="enviar_embed", description="Envía un embed al canal actual con estilo NexusStore")
@has_admin_role()
@app_commands.describe(
    titulo="Título del embed",
    descripcion="Descripción del embed"
)
async def send_embed(
    interaction: discord.Interaction,
    titulo: str,
    descripcion: str
):
    """Envía un embed con formato profesional y estilo NexusStore"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Verificar permisos del bot
        if not interaction.channel.permissions_for(interaction.guild.me).send_messages:
            embed_error = discord.Embed(
                title="❌ Sin Permisos",
                description="No tengo permisos para enviar mensajes en este canal.",
                color=config.COLOR_EMBED
            )
            embed_error.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed_error, ephemeral=True)
            return
        
        # Crear embed simple y limpio
        embed = discord.Embed(
            title=titulo,
            description=descripcion,
            color=config.COLOR_EMBED  # Color azul #0619D4
        )
        
        embed.set_footer(text="NexusStore © Todos los derechos reservados • Tu destino para recursos de élite")
        embed.timestamp = discord.utils.utcnow()
        
        # Enviar embed al canal
        await interaction.channel.send(embed=embed)
        
        # Confirmación simple al administrador
        await interaction.followup.send("✅ Embed enviado correctamente.", ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error en enviar_embed: {e}")
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"No se pudo enviar el embed: {type(e).__name__}",
            color=config.COLOR_EMBED
        )
        error_embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.followup.send(error_embed, ephemeral=True)

def has_admin_role_prefix():
    """Restringe los comandos de prefijo al dueño del servidor."""
    async def predicate(ctx):
        return ctx.guild is not None and ctx.author.id == ctx.guild.owner_id
    return commands.check(predicate)


@bot.command(name="enviar_embed", help="Envía un embed con formato profesional y estilo NexusStore. Uso: !enviar_embed \"Título\" Descripción")
@has_admin_role_prefix()
async def prefix_enviar_embed(ctx, titulo: str, *, descripcion: str):
    """Envía un embed con formato profesional y estilo NexusStore"""
    try:
        # Verificar permisos del bot
        if not ctx.channel.permissions_for(ctx.guild.me).send_messages:
            return
        
        embed = discord.Embed(
            title=titulo,
            description=descripcion,
            color=config.COLOR_EMBED  # Color azul #0619D4
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados • Tu destino para recursos de élite")
        embed.timestamp = discord.utils.utcnow()
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error en enviar_embed (prefijo): {e}")

@prefix_enviar_embed.error
async def embed_commands_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        admin_role = ctx.guild.get_role(config.ADMIN_ROLE_ID) if config.ADMIN_ROLE_ID else None
        role_name = admin_role.name if admin_role else "Administrador"
        embed = discord.Embed(
            title="Rol Requerido",
            description=f"Necesitas el rol **{role_name}** para usar este comando.",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await ctx.send(embed=embed, delete_after=10)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(
            title="Argumento Faltante",
            description=f"Uso correcto del comando:\n`{config.PREFIX}{ctx.command.name} \"Título\" Descripción`\n\n*Nota: Recuerda poner el título entre comillas si contiene espacios.*",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await ctx.send(embed=embed, delete_after=10)
    else:
        logger.error(f"Error en comando de prefijo: {error}")

def format_vote_duration(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class VotingButton(discord.ui.Button):
    def __init__(self, label: str, view_ref):
        super().__init__(label=label, style=discord.ButtonStyle.danger)
        self.vote_label = label
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        if self.view_ref.ended:
            await interaction.response.send_message("⏰ This poll has already ended.", ephemeral=True)
            return

        if interaction.user.id in self.view_ref.voted_users:
            await interaction.response.send_message("⚠️ You have already voted in this poll. You may vote again only in a future poll.", ephemeral=True)
            return

        self.view_ref.voted_users.add(interaction.user.id)
        self.view_ref.vote_counts[self.vote_label] += 1

        await interaction.response.defer(ephemeral=True)
        if self.view_ref.message is not None:
            await self.view_ref.message.edit(embed=self.view_ref.build_embed(), view=self.view_ref)
        await interaction.followup.send(f"✅ Your vote has been recorded for **{self.vote_label}**.", ephemeral=True)


class VotingView(discord.ui.View):
    def __init__(self, labels: list[str], duration_seconds: int):
        super().__init__(timeout=None)
        self.labels = labels
        self.vote_counts = {label: 0 for label in labels}
        self.voted_users = set()
        self.message = None
        self.ended = False
        self.end_time = discord.utils.utcnow() + timedelta(seconds=duration_seconds)

        for label in labels:
            self.add_item(VotingButton(label, self))

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🗳️ Poll",
            description=self.description,
            color=config.COLOR_EMBED
        )

        for label in self.labels:
            embed.add_field(
                name=label,
                value=f"{self.vote_counts[label]} votes",
                inline=True
            )

        if self.ended:
            embed.set_footer(text="Poll closed • NexusStore © All rights reserved")
        else:
            remaining_seconds = max(0, int((self.end_time - discord.utils.utcnow()).total_seconds()))
            embed.set_footer(text=f"Ends in {format_vote_duration(remaining_seconds)} • NexusStore © All rights reserved")

        embed.timestamp = discord.utils.utcnow()
        return embed


@bot.tree.command(name="votaciones", description="Envía un mensaje de votaciones con botones personalizados")
@has_admin_role()
@app_commands.describe(
    texto="Texto que aparecerá en el mensaje de votaciones",
    opcion_1="Texto del primer botón",
    opcion_2="Texto del segundo botón",
    duracion="Duración de la votación (ej. 5s, 5m, 5h, 5d)"
)
async def votaciones(
    interaction: discord.Interaction,
    texto: Optional[str] = None,
    opcion_1: Optional[str] = "Sí",
    opcion_2: Optional[str] = "No",
    duracion: Optional[str] = "1h"
):
    """Envía un mensaje elegante de votaciones con botones rojo de Nexus Store y contador de votos."""
    await interaction.response.defer()

    duration_seconds = parse_duration(duracion or "1h") if duracion else None
    if duration_seconds is None:
        await interaction.followup.send("⚠️ Please use a valid format such as 5s, 5m, 5h, or 5d.", ephemeral=True)
        return

    texto_final = texto or "🗳️ A poll is now open. Please cast your vote once and choose the option you prefer."

    labels = [label for label in [opcion_1, opcion_2] if label]
    if not labels:
        labels = ["Sí", "No"]

    view = VotingView(labels, duration_seconds)
    view.description = texto_final

    message = await interaction.followup.send(embed=view.build_embed(), view=view)
    view.message = message

    async def close_voting():
        try:
            await asyncio.sleep(duration_seconds)
            if view.message is None or view.ended:
                return
            view.ended = True
            for item in view.children:
                item.disabled = True
            await view.message.edit(embed=view.build_embed(), view=view)
        except Exception as e:
            logger.error(f"Error cerrando votación: {e}")

    asyncio.create_task(close_voting())

@bot.tree.command(name="anuncio", description="Envía un anuncio oficial")
@has_admin_role()
@app_commands.describe(
    mensaje="Mensaje del anuncio"
)
async def anuncio(interaction: discord.Interaction, mensaje: str):
    """Envía un anuncio oficial con el estilo de Nexus Store"""
    
    embed = discord.Embed(
        title="Anuncio Oficial",
        description=mensaje,
        color=config.COLOR_EMBED
    )
    
    embed.set_thumbnail(url=bot.user.display_avatar.url if bot.user.display_avatar else None)
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="join_voice", description="Conecta el bot a un canal de voz")
@has_admin_role()
async def join_voice(interaction: discord.Interaction):
    """Conecta el bot al canal de voz configurado con manejo robusto de errores"""
    # Usar el ID directamente para evitar problemas de configuración
    voice_channel_id = 1455587273788756232
    
    try:
        # Obtener el canal de voz
        voice_channel = interaction.guild.get_channel(voice_channel_id)
        
        if not voice_channel or not isinstance(voice_channel, discord.VoiceChannel):
            embed = discord.Embed(
                title="Canal no encontrado",
                description="No se encontró el canal de voz especificado.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        # Si ya está conectado, desconectar primero
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect(force=True)
            print(f"Desconectado del canal anterior en {interaction.guild.name}")
            await asyncio.sleep(1)
        
        # Conectar con el mismo manejo robusto que la función automática
        await interaction.response.defer(ephemeral=True)
        
        # Intentar conectar con el mismo sistema de reintentos
        for attempt in range(5):
            try:
                voice_client = await voice_channel.connect(
                    timeout=10, 
                    reconnect=True,
                    self_mute=False,
                    self_deaf=False
                )
                
                # Esperar para estabilizar
                await asyncio.sleep(2)
                
                if voice_client.is_connected():
                    # Iniciar mantenimiento
                    bot.loop.create_task(play_silent_audio(voice_client))
                    
                    embed = discord.Embed(
                        title="Conectado a voz",
                        description=f"El bot se ha conectado al canal **{voice_channel.name}** correctamente (intento {attempt + 1}).",
                        color=config.COLOR_EMBED
                    )
                    embed.set_footer(text="NexusStore © Todos los derechos reservados")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    
                    print(f"Conectado manualmente al canal de voz '{voice_channel.name}' (intento {attempt + 1})")
                    return
                else:
                    await voice_client.disconnect(force=True)
                    raise discord.ConnectionClosed(None, 4017)
                    
            except discord.ConnectionClosed as e:
                error_code = getattr(e, 'code', 'Unknown')
                print(f"Intento {attempt + 1} fallido: Conexión cerrada con código {error_code}")
                
                if error_code == 4017 and attempt < 4:
                    wait_time = 3 + (attempt * 2)
                    await asyncio.sleep(wait_time)
                elif attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                continue
                
            except Exception as e:
                print(f"Error en intento {attempt + 1}: {type(e).__name__}: {e}")
                if attempt < 4:
                    await asyncio.sleep(1)
                continue
        
        # Si todos los intentos fallaron
        embed = discord.Embed(
            title="Error de conexión",
            description=f"No se pudo conectar al canal de voz después de 5 intentos. Intente más tarde.",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.followup.send(embed=embed, ephemeral=True)
        print(f"No se pudo conectar manualmente después de 5 intentos")
            
    except Exception as e:
        embed = discord.Embed(
            title="Error",
            description=f"Ocurrió un error inesperado: {type(e).__name__}",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="leave_voice", description="Desconecta el bot del canal de voz")
@has_admin_role()
async def leave_voice(interaction: discord.Interaction):
    """Desconecta el bot del canal de voz actual"""
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        embed = discord.Embed(
            title="Desconectado de voz",
            description="El bot se ha desconectado del canal de voz.",
            color=config.COLOR_EMBED
        )
    else:
        embed = discord.Embed(
            title="No conectado",
            description="El bot no está conectado a ningún canal de voz.",
            color=config.COLOR_EMBED
        )
    
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="voice_status", description="Muestra el estado de conexión de voz")
async def voice_status(interaction: discord.Interaction):
    """Muestra el estado actual de la conexión de voz"""
    if interaction.guild.voice_client:
        voice_client = interaction.guild.voice_client
        embed = discord.Embed(
            title="Estado de Voz",
            description=f"**Conectado a:** {voice_client.channel.name}\n**Servidor:** {interaction.guild.name}",
            color=config.COLOR_EMBED
        )
        embed.add_field(
            name="Estado",
            value="Conectado y manteniendo presencia",
            inline=True
        )
        embed.add_field(
            name="Usuarios en canal",
            value=str(len(voice_client.channel.members)),
            inline=True
        )
    else:
        embed = discord.Embed(
            title="Estado de Voz",
            description="El bot no está conectado a ningún canal de voz.",
            color=config.COLOR_EMBED
        )
    
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Comandos de Moderación
@bot.tree.command(name="clear", description="Elimina mensajes del canal")
@app_commands.describe(
    cantidad="Número de mensajes a eliminar (1-100), o 'todos' para eliminar todos"
)
@has_admin_role()
async def clear_messages(interaction: discord.Interaction, cantidad: str):
    """Elimina mensajes del canal con opción de cantidad o todos"""
    try:
        # Verificar si la interacción ya fue respondida
        if interaction.response.is_done():
            await interaction.followup.send("Esta interacción ya está siendo procesada.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Verificar permisos del bot
        if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
            embed = discord.Embed(
                title="Sin Permisos",
                description="No tengo permisos para eliminar mensajes en este canal.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Verificar permisos del usuario
        if not interaction.channel.permissions_for(interaction.user).manage_messages:
            embed = discord.Embed(
                title="Sin Permisos",
                description="No tienes permisos para usar este comando.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Procesar el parámetro cantidad
        if cantidad.lower() == 'todos':
            # Eliminar todos los mensajes posibles
            try:
                deleted = await interaction.channel.purge(limit=100, check=lambda m: not m.pinned)
                count = len(deleted)
                
                embed = discord.Embed(
                    title="Canal Limpiado",
                    description=f"Se eliminaron **{count} mensajes** del canal.\n(Los mensajes anclados fueron preservados)",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text=f"Limpiado por {interaction.user.name} | NexusStore © Todos los derechos reservados")
                embed.timestamp = discord.utils.utcnow()
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                    
            except discord.Forbidden:
                embed = discord.Embed(
                    title="Error",
                    description="No puedo eliminar mensajes antiguos (más de 14 días).",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except Exception as e:
                embed = discord.Embed(
                    title="Error",
                    description=f"Ocurrió un error al limpiar el canal: {type(e).__name__}",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
        else:
            # Intentar convertir a número
            try:
                num = int(cantidad)
                if num < 1 or num > 100:
                    raise ValueError
                
                # Eliminar la cantidad especificada
                deleted = await interaction.channel.purge(limit=num, check=lambda m: not m.pinned)
                count = len(deleted)
                
                embed = discord.Embed(
                    title="Mensajes Eliminados",
                    description=f"Se eliminaron **{count} mensajes** del canal.",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text=f"Limpiado por {interaction.user.name} | NexusStore © Todos los derechos reservados")
                embed.timestamp = discord.utils.utcnow()
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                    
            except ValueError:
                embed = discord.Embed(
                    title="Cantidad Inválida",
                    description="Por favor, especifica un número entre 1 y 100, o escribe 'todos'.",
                    color=config.COLOR_EMBED
                )
                embed.add_field(
                    name="Ejemplos:",
                    value="• `/clear cantidad:10` - Elimina 10 mensajes\n• `/clear cantidad:todos` - Elimina todos los mensajes",
                    inline=False
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except discord.Forbidden:
                embed = discord.Embed(
                    title="Error",
                    description="No puedo eliminar mensajes antiguos (más de 14 días).",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except Exception as e:
                embed = discord.Embed(
                    title="Error",
                    description=f"Ocurrió un error al eliminar mensajes: {type(e).__name__}",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
    except Exception as e:
        logger.error(f"Error crítico en clear_messages: {e}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Ocurrió un error inesperado.", ephemeral=True)
            else:
                await interaction.followup.send("Ocurrió un error inesperado.", ephemeral=True)
        except:
            pass

@bot.tree.command(name="clear_user", description="Elimina mensajes de un usuario específico")
@app_commands.describe(
    usuario="Usuario cuyos mensajes quieres eliminar",
    cantidad="Número de mensajes a eliminar (1-100), o 'todos' para eliminar todos"
)
@has_admin_role()
async def clear_user_messages(interaction: discord.Interaction, usuario: discord.Member, cantidad: str):
    """Elimina mensajes de un usuario específico"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Verificar permisos del bot
        if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
            embed = discord.Embed(
                title="Sin Permisos",
                description="No tengo permisos para eliminar mensajes en este canal.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Verificar permisos del usuario
        if not interaction.channel.permissions_for(interaction.user).manage_messages:
            embed = discord.Embed(
                title="Sin Permisos",
                description="No tienes permisos para usar este comando.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # No permitir eliminar mensajes del bot o de administradores (protección)
        if usuario.bot:
            embed = discord.Embed(
                title="Operación no permitida",
                description="No puedo eliminar mensajes de otros bots.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Procesar el parámetro cantidad
        if cantidad.lower() == 'todos':
            # Eliminar todos los mensajes del usuario
            try:
                deleted = await interaction.channel.purge(
                    limit=None, 
                    check=lambda m: m.author == usuario and not m.pinned
                )
                count = len(deleted)
                
                embed = discord.Embed(
                    title="Mensajes de Usuario Eliminados",
                    description=f"Se eliminaron **{count} mensajes** de {usuario.mention}.\n(Los mensajes anclados fueron preservados)",
                    color=config.COLOR_EMBED
                )
                embed.set_thumbnail(url=usuario.display_avatar.url)
                embed.set_footer(text=f"Limpiado por {interaction.user.name} | NexusStore © Todos los derechos reservados")
                embed.timestamp = discord.utils.utcnow()
                
                # Enviar confirmación temporal
                temp_msg = await interaction.followup.send(embed=embed, ephemeral=False)
                
                # Eliminar el mensaje de confirmación después de 5 segundos
                await asyncio.sleep(5)
                try:
                    await temp_msg.delete()
                except:
                    pass
                    
            except discord.Forbidden:
                embed = discord.Embed(
                    title="Error",
                    description="No puedo eliminar mensajes antiguos (más de 14 días).",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except Exception as e:
                embed = discord.Embed(
                    title="Error",
                    description=f"Ocurrió un error al limpiar mensajes del usuario: {type(e).__name__}",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
        else:
            # Intentar convertir a número
            try:
                num = int(cantidad)
                if num < 1 or num > 100:
                    raise ValueError
                
                # Eliminar la cantidad especificada del usuario
                deleted = await interaction.channel.purge(
                    limit=num, 
                    check=lambda m: m.author == usuario and not m.pinned
                )
                count = len(deleted)
                
                embed = discord.Embed(
                    title="Mensajes de Usuario Eliminados",
                    description=f"Se eliminaron **{count} mensajes** de {usuario.mention}.",
                    color=config.COLOR_EMBED
                )
                embed.set_thumbnail(url=usuario.display_avatar.url)
                embed.set_footer(text=f"Limpiado por {interaction.user.name} | NexusStore © Todos los derechos reservados")
                embed.timestamp = discord.utils.utcnow()
                
                # Enviar confirmación temporal
                temp_msg = await interaction.followup.send(embed=embed, ephemeral=False)
                
                # Eliminar el mensaje de confirmación después de 3 segundos
                await asyncio.sleep(3)
                try:
                    await temp_msg.delete()
                except:
                    pass
                    
            except ValueError:
                embed = discord.Embed(
                    title="Cantidad Inválida",
                    description="Por favor, especifica un número entre 1 y 100, o escribe 'todos'.",
                    color=config.COLOR_EMBED
                )
                embed.add_field(
                    name="Ejemplos:",
                    value="• `/clear_user cantidad:10 @usuario` - Elimina 10 mensajes del usuario\n• `/clear_user cantidad:todos @usuario` - Elimina todos los mensajes del usuario",
                    inline=False
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except discord.Forbidden:
                embed = discord.Embed(
                    title="Error",
                    description="No puedo eliminar mensajes antiguos (más de 14 días).",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except Exception as e:
                embed = discord.Embed(
                    title="Error",
                    description=f"Ocurrió un error al eliminar mensajes del usuario: {type(e).__name__}",
                    color=config.COLOR_EMBED
                )
                embed.set_footer(text="NexusStore © Todos los derechos reservados")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
    except Exception as e:
        embed = discord.Embed(
            title="Error Crítico",
            description="Ocurrió un error inesperado. Por favor, inténtalo de nuevo.",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.followup.send(embed=embed, ephemeral=True)

# Comandos de Versículos Diarios
@bot.tree.command(name="versiculo_hoy", description="Muestra el versículo del día")
@app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.user.id)
async def versiculo_hoy(interaction: discord.Interaction):
    """Muestra el versículo del día actual"""
    await interaction.response.defer()
    
    try:
        # Obtener versículo del día
        verse_data = await get_daily_verse()
        if not verse_data:
            embed = discord.Embed(
                title="❌ Error",
                description="No se pudo obtener el versículo de hoy. Por favor, inténtalo más tarde.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Crear y enviar embed
        embed = create_verse_embed(verse_data)
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error en comando versiculo_hoy: {e}")
        embed = discord.Embed(
            title="❌ Error",
            description="Ocurrió un error al obtener el versículo.",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="versiculo_aleatorio", description="Muestra un versículo aleatorio")
@app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.user.id)
async def versiculo_aleatorio(interaction: discord.Interaction):
    """Muestra un versículo aleatorio de la Biblia"""
    await interaction.response.defer()
    
    try:
        # Obtener versículo aleatorio
        verse_data = await VerseSource.get_random_bible_verse()
        if not verse_data:
            embed = discord.Embed(
                title="❌ Error",
                description="No se pudo obtener un versículo aleatorio.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Crear y enviar embed
        embed = create_verse_embed(verse_data)
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error en comando versiculo_aleatorio: {e}")
        embed = discord.Embed(
            title="❌ Error",
            description="Ocurrió un error al obtener el versículo aleatorio.",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="enviar_versiculo", description="Envía el versículo del día manualmente")
@has_admin_role()
async def enviar_versiculo(interaction: discord.Interaction):
    """Envía manualmente el versículo del día al canal configurado"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Verificar que el canal esté configurado
        if config.VERSE_CHANNEL_ID is None:
            embed = discord.Embed(
                title="❌ Canal no configurado",
                description="No hay canal de versículos configurado.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Obtener canal
        channel = interaction.guild.get_channel(config.VERSE_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title="❌ Canal no encontrado",
                description="No se encontró el canal de versículos en este servidor.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Obtener y enviar versículo
        verse_data = await get_daily_verse()
        if not verse_data:
            embed = discord.Embed(
                title="❌ Error",
                description="No se pudo obtener el versículo del día.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Enviar versículo al canal
        embed = create_verse_embed(verse_data)
        await channel.send(embed=embed)
        
        # Confirmación al administrador
        confirm_embed = discord.Embed(
            title="✅ Versículo Enviado",
            description=f"El versículo del día ha sido enviado a #{channel.name}.",
            color=config.COLOR_EMBED
        )
        confirm_embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.followup.send(confirm_embed, ephemeral=True)
        
        logger.info(f"Versículo manual enviado por {interaction.user.name} a {channel.name}")
        
    except Exception as e:
        logger.error(f"Error en comando enviar_versiculo: {e}")
        embed = discord.Embed(
            title="❌ Error",
            description="Ocurrió un error al enviar el versículo.",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="versiculo_estado", description="Muestra el estado del sistema de versículos")
@has_admin_role()
async def versiculo_estado(interaction: discord.Interaction):
    """Muestra el estado actual del sistema de versículos diarios"""
    try:
        # Obtener información del estado
        enabled = config.VERSE_ENABLED
        channel_id = config.VERSE_CHANNEL_ID
        time_config = config.VERSE_TIME
        timezone_config = config.VERSE_TIMEZONE
        
        # Crear embed de estado
        embed = discord.Embed(
            title="Estado del Sistema de Versículos",
            color=config.COLOR_EMBED
        )
        
        # Estado del sistema
        status = "Activado" if enabled else "Desactivado"
        embed.add_field(
            name="Estado del Sistema",
            value=status,
            inline=True
        )
        
        # Canal configurado
        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
            channel_name = channel.name if channel else "No encontrado"
            embed.add_field(
                name="Canal de Envío",
                value=f"#{channel_name} (ID: {channel_id})",
                inline=True
            )
        else:
            embed.add_field(
                name="Canal de Envío",
                value="No configurado",
                inline=True
            )
        
        # Hora de envío
        embed.add_field(
            name="Hora de Envío",
            value=f"{time_config} ({timezone_config})",
            inline=True
        )
        
        # Próximo envío
        if enabled:
            try:
                timezone = pytz.timezone(timezone_config)
                hour, minute = map(int, time_config.split(':'))
                
                now = datetime.now(timezone)
                next_send = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                if now >= next_send:
                    next_send += timedelta(days=1)
                
                time_until = next_send - now
                hours = int(time_until.total_seconds() // 3600)
                minutes = int((time_until.total_seconds() % 3600) // 60)
                
                embed.add_field(
                    name="Próximo Envío",
                    value=f"{next_send.strftime('%d/%m/%Y %H:%M')} (en {hours}h {minutes}m)",
                    inline=False
                )
            except:
                embed.add_field(
                    name="📅 Próximo Envío",
                    value="Error calculando próximo envío",
                    inline=False
                )
        
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        embed.timestamp = discord.utils.utcnow()
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error en comando versiculo_estado: {e}")
        embed = discord.Embed(
            title="❌ Error",
            description="Ocurrió un error al obtener el estado del sistema.",
            color=config.COLOR_EMBED
        )
        embed.set_footer(text="NexusStore © Todos los derechos reservados")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# Manejo de errores mejorado
@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error):
    try:
        # Ignorar errores de interacción duplicada
        if isinstance(error, (app_commands.CommandInvokeError, app_commands.AppCommandError)):
            if "already been acknowledged" in str(error) or "Interaction has already been responded" in str(error):
                return  # Ignorar silenciosamente
        
        if isinstance(error, ChannelRestrictionError):
            embed = discord.Embed(
                title="Canal no permitido",
                description=str(error),
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except:
                pass
            return

        if isinstance(error, app_commands.CheckFailure):
            # Error de permisos personalizado (nuestro decorador has_admin_role)
            admin_role = interaction.guild.get_role(config.ADMIN_ROLE_ID) if config.ADMIN_ROLE_ID else None
            role_name = admin_role.name if admin_role else "Administrador"
            
            embed = discord.Embed(
                title="Rol Requerido",
                description=f"Necesitas el rol **{role_name}** para usar este comando.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except:
                pass  # Silenciar cualquier error en el manejador de errores
                
        elif isinstance(error, app_commands.CommandInvokeError):
            logger.error(f"Error en comando: {error.original}")
            embed = discord.Embed(
                title="Error al ejecutar comando",
                description="Ocurrió un error al ejecutar el comando. Por favor, inténtalo de nuevo.",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except:
                pass
                
        else:
            logger.error(f"Error no manejado: {type(error).__name__}: {error}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Ocurrió un error inesperado.", ephemeral=True)
                else:
                    await interaction.followup.send("Ocurrió un error inesperado.", ephemeral=True)
            except:
                pass
                
    except Exception as e:
        logger.error(f"Error crítico en manejador de errores: {e}")


# --- COMANDOS DE SORTEOS (SLASH & PREFIX) ---

@bot.tree.command(name="sorteo_iniciar", description="Inicia un nuevo sorteo")
@has_admin_role()
@app_commands.describe(
    duracion="Duración del sorteo (ej. 10s, 5m, 2h, 1d)",
    ganadores="Número de ganadores a elegir",
    premio="El premio del sorteo"
)
async def sorteo_iniciar_slash(interaction: discord.Interaction, duracion: str, ganadores: int, premio: str):
    await interaction.response.defer(ephemeral=True)
    
    seconds = parse_duration(duracion)
    if not seconds:
        await interaction.followup.send("Formato de duración inválido. Usa s (segundos), m (minutos), h (horas) o d (días). Ej. `10m`, `2h`.", ephemeral=True)
        return
        
    if ganadores <= 0:
        await interaction.followup.send("El número de ganadores debe ser mayor a 0.", ephemeral=True)
        return
        
    end_timestamp = discord.utils.utcnow().timestamp() + seconds
    
    embed = discord.Embed(
        title="NUEVO SORTEO",
        description=f"Reacciona con el botón de abajo para participar!\n\n**Premio:** {premio}\n**Ganadores:** {ganadores}\n**Participantes:** 0\n**Finaliza:** <t:{int(end_timestamp)}:R>",
        color=config.COLOR_EMBED
    )
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    embed.timestamp = discord.utils.utcnow()
    
    view = GiveawayView()
    
    message = await interaction.channel.send(embed=embed, view=view)
    
    giveaways = load_giveaways()
    giveaways.append({
        "message_id": message.id,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "prize": premio,
        "end_time": end_timestamp,
        "winners_count": ganadores,
        "participants": [],
        "ended": False
    })
    save_giveaways(giveaways)
    
    await interaction.followup.send(f"Sorteo iniciado correctamente para **{premio}**.", ephemeral=True)

# --- COMANDOS DE FINALIZAR SORTEO ---

@bot.tree.command(name="sorteo_finalizar", description="Finaliza un sorteo activo inmediatamente")
@has_admin_role()
@app_commands.describe(id_mensaje="ID del mensaje del sorteo")
async def sorteo_finalizar_slash(interaction: discord.Interaction, id_mensaje: str):
    await interaction.response.defer(ephemeral=True)
    try:
        msg_id = int(id_mensaje)
    except ValueError:
        await interaction.followup.send("El ID de mensaje debe ser un número válido.", ephemeral=True)
        return
        
    giveaways = load_giveaways()
    giveaway = next((g for g in giveaways if g["message_id"] == msg_id), None)
    if not giveaway:
        await interaction.followup.send("No se encontró ningún sorteo con ese ID de mensaje.", ephemeral=True)
        return
        
    if giveaway.get("ended", False):
        await interaction.followup.send("Este sorteo ya ha finalizado.", ephemeral=True)
        return
        
    await end_giveaway(msg_id)
    await interaction.followup.send("Sorteo finalizado con éxito.", ephemeral=True)

# --- COMANDOS DE CANCELAR SORTEO ---

@bot.tree.command(name="sorteo_cancelar", description="Cancela un sorteo activo sin elegir ganadores")
@has_admin_role()
@app_commands.describe(id_mensaje="ID del mensaje del sorteo")
async def sorteo_cancelar_slash(interaction: discord.Interaction, id_mensaje: str):
    await interaction.response.defer(ephemeral=True)
    try:
        msg_id = int(id_mensaje)
    except ValueError:
        await interaction.followup.send("El ID de mensaje debe ser un número válido.", ephemeral=True)
        return
        
    giveaways = load_giveaways()
    giveaway = next((g for g in giveaways if g["message_id"] == msg_id), None)
    if not giveaway:
        await interaction.followup.send("No se encontró ningún sorteo con ese ID de mensaje.", ephemeral=True)
        return
        
    if giveaway.get("ended", False):
        await interaction.followup.send("Este sorteo ya ha finalizado y no se puede cancelar.", ephemeral=True)
        return
        
    giveaway["ended"] = True
    save_giveaways(giveaways)
    
    try:
        guild = bot.get_guild(giveaway["guild_id"]) or await bot.fetch_guild(giveaway["guild_id"])
        channel = guild.get_channel(giveaway["channel_id"]) or await bot.fetch_channel(giveaway["channel_id"])
        message = await channel.fetch_message(msg_id)
        
        embed = message.embeds[0]
        embed.description = f"**Premio:** {giveaway['prize']}\n\nEste sorteo ha sido cancelado por un administrador."
        embed.color=config.COLOR_EMBED
        await message.edit(embed=embed, view=None)
    except Exception as e:
        logger.error(f"Error cancelando sorteo en Discord: {e}")
        
    await interaction.followup.send("Sorteo cancelado con éxito.", ephemeral=True)



@bot.tree.command(name="agregar_rol_faltantes", description="Agrega el rol automático a los usuarios que no lo tengan")
@has_admin_role()
async def agregar_rol_faltantes(interaction: discord.Interaction):
    """Assign AUTO_ROLE_ID to members missing it."""
    await interaction.response.defer(ephemeral=True)
    role = interaction.guild.get_role(config.AUTO_ROLE_ID)
    if role is None:
        try:
            role = await interaction.guild.fetch_role(config.AUTO_ROLE_ID)
        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"No se pudo obtener el rol con ID {config.AUTO_ROLE_ID}: {e}",
                color=config.COLOR_EMBED
            )
            embed.set_footer(text="NexusStore © Todos los derechos reservados")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
    added = 0
    failed = 0
    async for member in interaction.guild.fetch_members(limit=None):
        if role not in member.roles:
            try:
                await member.add_roles(role)
                added += 1
                await asyncio.sleep(0.5)
            except discord.Forbidden:
                failed += 1
            except Exception:
                failed += 1
    embed = discord.Embed(
        title="Asignación completada",
        description=f"Se asignó el rol a **{added}** usuarios.\nFallos: **{failed}**.",
        color=config.COLOR_EMBED
    )
    embed.set_footer(text="NexusStore © Todos los derechos reservados")
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- COMANDOS DE REROLL SORTEO ---

@bot.tree.command(name="sorteo_reroll", description="Elige nuevos ganadores de un sorteo finalizado")
@has_admin_role()
@app_commands.describe(id_mensaje="ID del mensaje del sorteo")
async def sorteo_reroll_slash(interaction: discord.Interaction, id_mensaje: str):
    await interaction.response.defer(ephemeral=True)
    try:
        msg_id = int(id_mensaje)
    except ValueError:
        await interaction.followup.send("El ID de mensaje debe ser un número válido.", ephemeral=True)
        return
        
    giveaways = load_giveaways()
    giveaway = next((g for g in giveaways if g["message_id"] == msg_id), None)
    if not giveaway:
        await interaction.followup.send("No se encontró ningún sorteo con ese ID de mensaje.", ephemeral=True)
        return
        
    if not giveaway.get("ended", False):
        await interaction.followup.send("Este sorteo aún no ha finalizado.", ephemeral=True)
        return
        
    participants = giveaway["participants"]
    if not participants:
        await interaction.followup.send("No hubo participantes en este sorteo para elegir ganadores.", ephemeral=True)
        return
        
    import random
    winners_count = giveaway["winners_count"]
    actual_winners_count = min(len(participants), winners_count)
    winners_ids = random.sample(participants, actual_winners_count)
    winners_mentions = ", ".join([f"<@{uid}>" for uid in winners_ids])
    
    try:
        guild = bot.get_guild(giveaway["guild_id"]) or await bot.fetch_guild(giveaway["guild_id"])
        channel = guild.get_channel(giveaway["channel_id"]) or await bot.fetch_channel(giveaway["channel_id"])
        message = await channel.fetch_message(msg_id)
        
        embed = message.embeds[0]
        embed.description = f"**Premio:** {giveaway['prize']}\n**Ganadores (Reroll):** {winners_mentions}\n**Participantes:** {len(participants)}\n\nEl sorteo ha finalizado."
        embed.color=config.COLOR_EMBED
        await message.edit(embed=embed, view=None)
        
        await channel.send(f"[REROLL] Felicidades {winners_mentions}! Has ganado el sorteo por **{giveaway['prize']}**.")
    except Exception as e:
        logger.error(f"Error realizando reroll: {e}")
        await interaction.followup.send("Ocurrió un error al contactar al canal o mensaje.", ephemeral=True)
        return
        
    await interaction.followup.send("Reroll realizado con éxito.", ephemeral=True)


if __name__ == "__main__":
    bot.run(config.BOT_TOKEN)
