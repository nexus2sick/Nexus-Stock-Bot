import discord

# Configuración del Bot
BOT_TOKEN = "MTU1MTIyMzQwNzc1OTMyNzMxMw.Ge0dFv.Ld1s4cQynLdcW5p8enkS22JRNjZuFYhLycudE0"  # Reemplaza con tu token de bot
PREFIX = "!"
COLOR_EMBED = 0x8B0000  # Color rojo oscuro #0x8B0000

# Configuración de Bienvenida
WELCOME_CHANNEL_ID = None  # ID del canal donde se enviarán los mensajes de bienvenida
WELCOME_CHANNEL_NAME = "👋・bienvenidas"
GOODBYE_CHANNEL_ID = None
GOODBYE_CHANNEL_NAME = "👋・despedidas"
BOOST_CHANNEL_ID = None  # ID del canal para notificaciones de boost
WELCOME_BANNER = None  # Sin banner externo

# Configuración de Invitaciones
INVITE_LOG_CHANNEL_ID = None  # ID del canal donde se enviarán los logs de invitaciones

# Configuración de Voz
VOICE_CHANNEL_ID = None  # ID del canal de voz donde el bot estará conectado siempre (usar /join_voice manualmente)

# IDs de Roles (opcional)
ADMIN_ROLE_ID = None  # Rol requerido para comandos de admin
SUPPORT_ROLE_ID = None
AUTO_ROLE_ID = None  # Rol que se asigna automáticamente al entrar
MOD_LOG_CHANNEL_ID = None
MOD_LOG_CHANNEL_NAME = "🛡️・mod-logs"
VOUCHES_CHANNEL_ID = None
VOUCHES_CHANNEL_NAME = "✅・nexus-vouches"

# Moderacion
MODERATION_ENABLED = True
PROHIBITED_WORDS = [
	"cabron",
	"cb",
	"mamabicho",
	"mmb",
	"hijueputa",
	"hijo de puta",
	"hdpt",
	"cagate en tu madre",
	"pendejo",
	"pndj",
	"wlb",
	"welebichos",
	"me maman el bicho",
	"mierda",
	"basura"
]  # Agrega aqui las palabras que quieras bloquear
PROMOTION_WORDS = ["discord.gg/", "discord.com/invite/", "compra", "vendo", "venta"]
BAD_WORD_TIMEOUT_MINUTES = 10
PROMOTION_WARNINGS_BEFORE_BAN = 3

# Tickets: aviso a las 15 horas y cierre a las 20 horas sin actividad
TICKET_INACTIVITY_ENABLED = True
TICKET_WARNING_HOURS = 15
TICKET_CLOSE_HOURS = 20

# Mensaje del Panel de Tickets
TICKET_PANEL_MESSAGE = """
Welcome to **Nexus Store** support.

Choose the area that best fits your query in the menu below so we can assist you as quickly and efficiently as possible.

Important:
Open a ticket if you have a real question, want to make a purchase, or have a pending case to resolve. Avoid opening unnecessary tickets to avoid penalties.
"""

# Imagen y Banner del Panel de Tickets
TICKET_PANEL_IMAGE = None  # Sin imagen externa
TICKET_PANEL_BANNER = None  # Sin banner externo

# Métodos de Pago
PAY_METHODS_TITLE = "**NEXUSFN v1.0.0**"
PAY_METHODS_MESSAGE = """
El checker oficial desarrollado por **Nexus Stock**.

⚡ **Rápido**.
🔒 **Seguro**.
🖥️ **Protección HWID**.
📈 **Dashboard en tiempo real**.

━━━━━━━━━━━━━━━━━━━━━━

✅ Características
• **Sistema de licencias**
• **Protección HWID**
• **Dashboard en tiempo real**
• **Multi-thread**
• **Resultados organizados**
• **Actualizaciones**
• **Soporte**

━━━━━━━━━━━━━━━━━━━━━━

🎫 Para comprar, abre un ⁠├・🎫・tickets .

💰 PRECIOS
🗓️ 3 Días ............. $3

🗓️ 7 Días ............. $5

🗓️ 30 Días .......... $15

♾️ Lifetime ........ $35

━━━━━━━━━━━━━━━━━━━━━━

💳 Métodos de pago

{paypal} • PayPal
{bitcoin} • Crypto
{bank} • Ath movil

📌 INFORMACIÓN
🔒 Cada licencia queda ligada al HWID.

🖥️ Una licencia funciona en un solo PC.

📥 Las actualizaciones serán anunciadas en este canal.

🎫 Si necesitas ayuda abre un ⁠├・🎫・tickets .

@everyone
"""


# Configuración de Versículo Diario
VERSE_CHANNEL_ID = None  # ID del canal donde se enviarán los versículos diarios
VERSE_TIME = "08:00"  # Hora de envío del versículo (formato HH:MM)
VERSE_ENABLED = True  # Activar/desactivar envío automático
VERSE_TIMEZONE = "America/Bogota"  # Zona horaria para el envío

# Configuración Anti-Link
ANTI_LINK_ENABLED = False  # Activar/desactivar sistema anti-link
ALLOWED_LINK_ROLE_ID = None  # ID del rol que puede enviar enlaces

# Configuración Anti-Nuke (Blindaje Interno)
ANTI_NUKE_ENABLED = False  # Activar/desactivar sistema anti-nuke
NUKE_LIMIT_CHANNELS = 3  # Límite de canales que se pueden borrar en 60 segundos
NUKE_LIMIT_ROLES = 2  # Límite de roles que se pueden borrar en 60 segundos
NUKE_LIMIT_KICKS = 3  # Límite de usuarios que se pueden expulsar en 60 segundos
NUKE_LIMIT_BANS = 2  # Límite de usuarios que se pueden banear en 60 segundos
SAFE_ADMIN_ROLES = [None]  # Roles que no serán afectados por el anti-nuke

# Configuración Anti-Raid (Blindaje Perimetral)
ANTI_RAID_ENABLED = False  # Activar/desactivar sistema anti-raid
MIN_ACCOUNT_AGE_DAYS = 7  # Edad mínima de cuenta para entrar (días)
RAID_THRESHOLD_USERS = 5  # Número de usuarios que entran en tiempo corto para considerar raid
RAID_TIME_WINDOW = 30  # Ventana de tiempo para detectar raid (segundos)
CAPTCHA_ENABLED = False  # Activar/desactivar verificación por captcha

# Configuración Anti-Phishing y Seguridad
ANTI_PHISHING_ENABLED = False  # Activar/desactivar anti-phishing
ANTI_IP_LOGGER_ENABLED = False  # Activar/desactivar anti-ip logger
ANTI_INVITE_SPAM_ENABLED = False  # Activar/desactivar anti-spam de invitaciones