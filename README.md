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

El rol indicado por `CO_ADMIN_ROLE_NAME` puede leer y cerrar tickets, usar `/banear`,
`/timeout`, `/clear`, `/clear_user`, `/crear_canal` y `/editar_canal`. La limpieza
por comando excluye los mensajes del dueño. Los comandos de canal solo crean canales
de texto o editan nombre/tema; no borran canales ni cambian sus permisos.

No concedas a ese rol `Administrator`, `Manage Messages`, `Manage Channels`,
`Manage Server` ni `Manage Roles`: esos permisos de Discord permitirían saltarse las
protecciones del bot. El bot necesita `Manage Messages`, `Manage Channels`,
`Ban Members` y `Moderate Members`, y su rol debe estar por encima del rol Admin y
de los miembros a los que moderará. El bot agrega el rol Admin a los tickets
existentes y nuevos al conectarse.
