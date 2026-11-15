# Nexus-Stock-Bot
Discord bot for Nexus Stock

## Persistencia del ranking

En Render, configura `DATABASE_URL` con la URL de una base PostgreSQL gratuita.
El bot crea la tabla de reputación al iniciar y guarda ahí el ranking y las ventas,
para que sobrevivan a reinicios y despliegues. Si Render no encuentra `DATABASE_URL`,
no inicia en Render para evitar continuar usando almacenamiento temporal.

En ejecución local se usa `reputation.json`. Si la tabla de la base de datos aún está
vacía y ese archivo existe al iniciar, el bot lo importa automáticamente. No subas un
respaldo con datos de usuarios al repositorio; consérvalo fuera del código.

Para recuperar compras guardadas en Discord, configura la base de datos y despliega
el bot. Luego, como dueño del servidor, ejecuta `/recuperar_reputacion`. El bot lee
los mensajes del canal de reputación en orden; necesita permiso para ver el canal y
leer su historial. Los mensajes de compra deben seguir existiendo. Si no encuentra
registros, no modifica los datos.

## Rol Admin limitado

El rol indicado por `CO_ADMIN_ROLE_NAME` puede usar los comandos slash y comandos
administrativos de prefijo del bot desde cualquier canal, incluido `/vouch`.
`/drop` sigue limitado a `#drops` y `/cerrar` solo funciona en tickets. La limpieza
por comando excluye los mensajes del dueño. Los comandos de canal solo crean canales
de texto o editan nombre/tema; no borran canales ni cambian sus permisos. Las
restricciones propias de cada comando (por ejemplo, jerarquía de roles al moderar)
se mantienen.

No concedas a ese rol `Administrator`, `Manage Messages`, `Manage Channels`,
`Manage Server` ni `Manage Roles`: esos permisos de Discord permitirían saltarse las
protecciones del bot. El bot necesita `Manage Messages`, `Manage Channels`,
`Ban Members` y `Moderate Members`, y su rol debe estar por encima del rol Admin y
de los miembros a los que moderará. El bot agrega el rol Admin a los tickets
existentes y nuevos al conectarse.

## Nexus Drop

El comando `/drop` pide premio, descripción, imagen adjunta opcional, duración y
cantidad de ganadores. La imagen se adjunta directamente desde Discord, igual que
en `/vouch`; el bot la vuelve a adjuntar al mensaje del Drop. Solo el dueño o Staff
autorizado puede crear o finalizar Drops. La participación está abierta a los
miembros del servidor. El botón `PARTICIPANTES` muestra la lista de inscritos de
forma privada solo al creador del Drop o al Staff autorizado. Discord muestra los
botones en el mensaje público; el bot rechaza los clics de usuarios no autorizados.

Configura `DROP_CHANNEL_NAME = "drops"` o indica el ID en `DROP_CHANNEL_ID`. El
comando y el envío del formulario se validan en ese canal; fuera de él, `/drop`
responde con un aviso y no publica nada.
La duración permitida es de 1 minuto a 30 días y acepta formatos como `10m`, `1h`
o `2d`; se permiten de 1 a 20 ganadores.
Al ganar, se anuncia al usuario y se le indica que abra un ticket en
`TICKET_OPEN_CHANNEL_ID` para reclamar el premio.

Los Drops, participantes y resultados se guardan en la tabla PostgreSQL
`nexus_drops` cuando `DATABASE_URL` está configurada, y sus botones siguen
funcionando después de reiniciar el bot. En ejecución local sin base de datos se
usa `drops.json`.
