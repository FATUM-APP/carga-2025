# Poblar Fatum con órdenes y calificaciones históricas de 2025

## Qué revisé y qué incluye este paquete

Revisé de nuevo las ramas `main` el 15 de noviembre de 2026:

| Servicio | Commit revisado |
|---|---|
| CatalogService | `eecdc851274388f0b9a106085dc7203ef701f645` |
| UserService | `8554ae02fb81d35dd6ad400ea7f1719275b5b7f2` |
| RateService | `136425dd6053b6f30134133b1d1ab5b6136218f5` |

Catalog ya eliminó la anotación de unicidad por proveedor/categoría, eliminó su validación en `CatalogItemService`, actualizó el test e incorporó V10. El título sigue siendo único por profesional. No se necesita otro cambio de código para ejecutar este seed.

En el nuevo adjunto hay dos consultas: la primera muestra 12 servicios y la última muestra **19**. Este paquete usa la última: **Camilo 6, Samantha 7, Yaneth 6**. No crea los dos servicios pendientes, ni imágenes, ni categorías. Conserva las categorías reales del adjunto, aunque difieran de las sugerencias anteriores: por ejemplo, el masaje de Camilo y el drenaje de Samantha quedaron en `Spa & Skin Care`; eso se respeta.

Archivos:

- `seed_fatum.py`: generador, carga directa PostgreSQL, verificación y snapshots históricos opcionales.
- `alcance.json`: los 19 IDs de servicios y los 5 IDs internos de clientes.
- `requirements.txt`: dependencia de Python.
- `conexion.env.example`: plantilla sin contraseñas reales.
- `verificar_catalog.sql`, `verificar_rate.sql`: comprobaciones SQL de solo lectura.
- `opcional_edades.sql`: simulación reversible para variar edades de clientes, NO se ejecuta automáticamente.

**No conecté ni escribí en tu BD.** Las pruebas se hicieron con PostgreSQL 16 local, las migraciones SQL reales y datos sintéticos. No fue una prueba de red contra tu PostgreSQL 18, ni de S3/Cognito/APIs desplegadas.

## Qué hace exactamente la carga

Añade **50 órdenes NUEVAS por servicio**. No significa completar hasta 50: los registros existentes se mantienen y el total final puede superar ese número.

De las primeras 50 nuevas, entre 30 y 38 por servicio quedan completadas y calificadas (60–76 %); el resto son rechazadas o canceladas. No inventa órdenes históricas todavía en entrega. Si las órdenes actuales diluyen el porcentaje, añade más órdenes completadas hasta asegurar que al menos el 40 % del total final de cada servicio esté COMPLETED y tenga rating final de SERVICE.

Para cada orden nueva completada crea tres ratings finales: SERVICE, PROFESSIONAL y CLIENT, con valores variados 1–5 y `updated=true`. Por tanto, las tres representan una respuesta enviada en la simulación, no sólo el 5 inicial automático. El reemplazo posterior por API ya no está permitido para esos ratings, como exige la regla de una sola actualización.

Con 19 servicios y sin necesidad de compensar un gran número de órdenes antiguas, el plan genera **950 órdenes nuevas, 642 completadas nuevas y 1.926 ratings nuevos**. El resumen de `plan` informa las cantidades definitivas con los datos vivos.

Respeta:

- Usuarios internos `users.id`, no identificadores Cognito `aws_id`.
- Roles, estado activo y verificación de los ocho usuarios del alcance.
- Dirección principal REAL del cliente, tomada de `addresses` con `position=0` y su FK `user_aws_id`.
- Servicios activos, categorías hoja habilitadas y padres habilitados.
- Existencia de al menos una imagen por servicio; no toca portafolios.
- Preguntas fijas reales y JSON de respuestas `[{"question":"...","answer":["..."]}]`.
- Horarios semanales, bloqueos de agenda, duración, anticipación histórica y ausencia de cruces de la carga por profesional y cliente.
- Precio base intacto si no es negociable; variación moderada si es negociable. No multiplica automáticamente HOURLY por duración: el microservicio tampoco lo hace.
- Las seis confirmaciones en true y delivery_started_at no nulo en COMPLETED. Cancelaciones desde PENDING con motivo; rechazadas/canceladas sin confirmaciones.
- Proyecciones de RateService y sumas/conteos de ratings de usuario, sin duplicar puntuaciones al reejecutar.

No modifica usuarios, fechas de nacimiento, servicios, precios del catálogo, imágenes, agendas, categorías ni órdenes previas. Tampoco inventa visitas o tiempos de permanencia ni envía eventos históricos hacia colas.

**Advertencia comercial:** todos los registros creados son sintéticos. Usa una copia de desarrollo; no presentarlos como ventas reales o reputación pública real.

## Decisión necesaria sobre fechas

Para representar una prestación realizada en 2025, no basta con atrasar `created_at` y `updated_at`: también `booking`, `delivery_started_at` y las fechas de ratings deben ser históricas y coherentes. La anticipación de 30 minutos se evalúa respecto al instante de creación histórico, no al reloj de hoy. No es posible reservar en 2025 mediante la API viva sin que rechace LEAD_TIME.

El comando de escritura exige **`--allow-historical-write`**. Usarlo significa que aceptas explícitamente esta cronología histórica completa, incluyendo las fechas de reserva, inicio, ratings y, si lo solicitas, snapshots. Si quieres permitir únicamente created_at/updated_at, **no ejecutes apply**: una entrega de 2025 con booking futura sería inconsistente.

La zona asumida es **America/Bogota**. Catalog guarda TIMESTAMP sin zona y RateService TIMESTAMPTZ. El script convierte respetando esa diferencia. Si el despliegue utiliza otra zona, no ejecutar sin ajustar y verificar primero la interpretación temporal.

## Paso 1. Preparar una copia y backups

Primero respalda las tres bases con `pg_dump` 18 (tu servidor del adjunto es 18; un pg_dump 15/16 no debe usarse contra ese servidor).

```bash
pg_dump "host=TU_HOST port=5432 dbname=TU_BD_CATALOG user=TU_USUARIO sslmode=require" \
  -W -Fc -f catalog_antes_seed.dump
pg_dump "host=TU_HOST port=5432 dbname=TU_BD_USERS user=TU_USUARIO sslmode=require" \
  -W -Fc -f users_antes_seed.dump
pg_dump "host=TU_HOST port=5432 dbname=TU_BD_RATE user=TU_USUARIO sslmode=require" \
  -W -Fc -f rate_antes_seed.dump
```

Restaura cada dump en una BD de ensayo correspondiente, no mezcles tablas de los tres microservicios en una sola base. Configura tus instancias de desarrollo para leer esas copias cuando quieras validar la interfaz. Comprueba la restauración antes de avanzar. La guía no incluye DROP DATABASE ni borrar datos de producción.

Mantén las copias sin tráfico de escritura ni consumidores de eventos durante plan/apply. El script usa locks de coordinación entre ejecuciones propias, pero **esos locks no detienen a tu aplicación**. `--test-database` es tu confirmación local de que elegiste las copias; no detecta mágicamente si un servidor es producción.

Los nombres reales se obtienen de `DB_URL` en cada despliegue. En el adjunto Catalog es `catalogservice_db`. El primer adjunto de usuarios tenía prompt `postgres=>`: no asumir que su BD se llama `fatum_users`. RateService tampoco fija el nombre. Confirmar con `SELECT current_database();` y el DB_URL de cada servicio.

## Paso 2. Instalar las herramientas

Necesitas Python **3.10 o superior**, pip y clientes PostgreSQL para backup/diagnóstico. En Linux/macOS:

```bash
cd carga-2025
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

En Windows PowerShell:

```powershell
cd carga-2025
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Si falla el acceso a PostgreSQL, no publiques el servidor para todo Internet: usa VPN/túnel autorizado o el equipo que ya tenga acceso, y reglas de red limitadas a lo necesario.

## Paso 3. Configurar las tres conexiones

El script no acepta `jdbc:postgresql://`. Usa un DSN PostgreSQL, por ejemplo:

```text
host=TU_HOST port=5432 dbname=TU_BD_DE_PRUEBA user=TU_USUARIO sslmode=require
```

Preferiblemente guarda las contraseñas con libpq/pgpass:

- Linux/macOS: `~/.pgpass`, permisos `600`.
- Windows: `%APPDATA%\postgresql\pgpass.conf`.

Formato, una línea por base/usuario:

```text
HOST:5432:BD_CATALOG_DE_PRUEBA:USUARIO:CONTRASEÑA
HOST:5432:BD_USERS_DE_PRUEBA:USUARIO:CONTRASEÑA
HOST:5432:BD_RATE_DE_PRUEBA:USUARIO:CONTRASEÑA
```

En pgpass, los caracteres `:` y `\` dentro de una contraseña se escapan según libpq. No compartas este archivo. Alternativamente usa DSNs con credenciales sólo en tu configuración privada, nunca en GitHub ni en capturas del terminal.

`sslmode=require` cifra, pero no verifica completamente la identidad del servidor. Con una CA del proveedor usa `sslmode=verify-full sslrootcert=/ruta/ca.pem`.

### Linux/macOS

```bash
cp conexion.env.example conexion.env
# Edita conexion.env con host, usuario y nombres de tus copias.
chmod 600 conexion.env
source conexion.env
```

### Windows PowerShell

```powershell
$env:USERS_DSN="host=TU_HOST port=5432 dbname=BD_USERS_DE_PRUEBA user=TU_USUARIO sslmode=require"
$env:CATALOG_DSN="host=TU_HOST port=5432 dbname=BD_CATALOG_DE_PRUEBA user=TU_USUARIO sslmode=require"
$env:RATE_DSN="host=TU_HOST port=5432 dbname=BD_RATE_DE_PRUEBA user=TU_USUARIO sslmode=require"
```

Puedes verificar qué copias seleccionaste desde psql antes del plan:

```bash
psql "$USERS_DSN" -c 'SELECT current_database(),current_user;'
psql "$CATALOG_DSN" -c 'SELECT current_database(),current_user;'
psql "$RATE_DSN" -c 'SELECT current_database(),current_user;'
```

PowerShell usa `$env:USERS_DSN`, `$env:CATALOG_DSN`, `$env:RATE_DSN` en lugar de las variables Bash.

## Paso 4. Resolver precondiciones

El script comprueba que las migraciones llegaron al menos a Catalog V10, User V11 y Rate V3; y que ya no existe `uk_services_provider_category`. No modifica `flyway_schema_history` ni intenta reparar esquemas automáticamente.

Todos los clientes necesitan una dirección principal: **el adjunto `users.city` vacío no demuestra que no tengan dirección**, porque la fuente correcta es la tabla `addresses`. Si falta, añádela por la API de UserService en el entorno de prueba. Su M2M devuelve null cuando no hay dirección, mientras Catalog exige dirección no nula: inventar un domicilio por SQL no imitaría ese flujo.

Todos deben ser adultos al inicio de 2025. Los usuarios del primer adjunto cumplen, si no cambiaron después.

Si RateService ya tiene sumas/conteos inconsistentes o roles desactualizados, el script se detiene en lugar de sobrescribirlos. Comparar usando `verificar_rate.sql`, resolver la proyección existente y repetir. No ignorar el error ni poner los acumulados en cero.

### Edades opcionales

Por defecto se mantienen tus fechas de nacimiento. Si deseas variedad adicional, `opcional_edades.sql` propone cinco fechas para CLIENT únicamente, muestra el antes/después y exporta un CSV de respaldo. **Termina en ROLLBACK**, así que la primera ejecución no confirma cambios:

```bash
psql "$USERS_DSN" -f opcional_edades.sql
```

Si aceptas esas fechas, en TU copia del archivo sustituye el ROLLBACK final por COMMIT y ejecútalo antes de crear el plan. No ejecutar contra profesionales. No regenerar ese respaldo después de confirmar sin conservar el original. No tocar Cognito ni identidades.

## Paso 5. Planificar: NO escribe en ninguna BD

```bash
python seed_fatum.py plan --file plan_2025.json
```

Lee datos actuales, agenda y órdenes existentes; genera en memoria todas las filas y guarda un manifiesto local. El resumen muestra por servicio: órdenes existentes, nuevas, total final y porcentaje de completadas calificadas. También muestra nombres de bases elegidas.

Si no hay capacidad de agenda, no cambia horarios: aborta. Si hay datos faltantes o inválidos, aborta antes de escribir. El manifiesto no se sobrescribe si ya existe.

**El manifiesto contiene nombres y direcciones reales** necesarios para los snapshots de órdenes. Guárdalo privado, no lo subas al repositorio ni lo adjuntes a conversaciones públicas. En Linux/macOS se crea con permisos 600; en Windows aplica permisos/ACL privados al directorio.

Comprueba que aparecen exactamente los 19 servicios esperados. Si decides cargar menos, edita `alcance.json` ANTES del plan. No se incluyen automáticamente servicios futuros que no figuren en ese archivo.

## Paso 6. Aplicar, después de revisar el plan

Sólo si aceptas la cronología histórica completa y estás en las copias de prueba:

```bash
python seed_fatum.py apply --file plan_2025.json --allow-historical-write --test-database
```

Orden de operaciones:

1. Vuelve a leer y comparar el estado base con el usado para planificar; si cambió, no inicia la carga.
2. En una transacción de Catalog inserta las órdenes y inicializa filas de métricas faltantes, sin tocar contadores ya existentes.
3. En otra transacción de Rate inserta los snapshots de completadas y sus tres ratings, crea user_snapshots faltantes y aplica incrementos sólo por ratings recién insertados.
4. Verifica que coincidan todas las filas del manifiesto y que cada servicio cumpla cantidad/porcentaje.

No usa DELETE/TRUNCATE, no cambia órdenes previas ni sobrescribe ratings existentes.

### Si falla a mitad de la carga

Catalog y Rate son bases separadas: **no hay un commit global**. Si Catalog confirma y Rate falla, conserva el manifiesto y ejecuta otra vez **el mismo comando con el mismo archivo** después de resolver la conexión/problema. Reconoce las filas ya insertadas por sus IDs y comprueba que coincidan; no las duplica ni aumenta puntuaciones dos veces.

No generes otro plan después de una carga parcial: produciría otro conjunto de IDs. Si una fila del seed fue editada o el estado base cambió por tráfico, detén el procedimiento y revisa: el script no la sobrescribe. La opción segura para deshacer todo es restaurar las copias desde los backups, no borrar arbitrariamente por fechas.

## Paso 7. Verificar independientemente

```bash
python seed_fatum.py verify --file plan_2025.json
```

No escribe. Cruza las dos bases y comprueba cada servicio individualmente, además de las filas concretas del manifiesto y los acumulados de las ocho personas.

También puedes ejecutar las consultas SQL adjuntas, que son de solo lectura:

```bash
psql "$CATALOG_DSN" -f verificar_catalog.sql
psql "$RATE_DSN" -f verificar_rate.sql
```

Estas consultas pueden mostrar inconsistencias PREEXISTENTES fuera del seed; no significa necesariamente que el script las haya creado. La verificación cruzada del porcentaje real está en `seed_fatum.py verify`, porque una consulta local no puede unir automáticamente dos bases independientes.

En particular, las comprobaciones SQL de `updated=true` sirven para verificar el objetivo de calificaciones finales: un rating anterior con `updated=false` puede ser un 5 inicial legítimo de RateService, no corrupción. Tampoco exigir que los ratings anteriores sean de 2025: el script de Python sólo requiere fechas y valores exactos para sus propias filas.

## Paso 8. Llenar los reportes históricos de Catalog (opcional)

Insertar orders no crea los reportes históricos por sí solo. `MetricsService.generateSnapshotForService` genera la semana actual, no reconstruye 2025. Si deseas que esos endpoints muestren el año anterior:

```bash
python seed_fatum.py snapshots --file plan_2025.json --allow-historical-write --test-database
```

Genera cortes semanales de órdenes de 2025, más un corte al 31 de diciembre, usando la cronología guardada en el manifiesto. Calcula entradas semanales y stocks acumulados por estado. Los favoritos se cuentan por su fecha; vistas y permanencia quedan en cero en estos snapshots porque **no se simularon**, sin modificar los contadores actuales de métricas.

El año/semana del snapshot es ISO; el 31 de diciembre de 2025 pertenece a la semana ISO 1 de 2026, pero `snapshot_date` sigue siendo 2025-12-31. El historial anual del servicio consulta por fecha y lo incluye.

Este paso se detiene, sin confirmar su transacción, si:

- Ya hay un snapshot en la misma semana creado por otra fuente.
- Hay órdenes ajenas al manifiesto creadas antes del cierre de 2025 para esos servicios. No existe una tabla de historial de estados para reconstruirlas fielmente.

No sobrescribe snapshots reales ni finge conocer la trayectoria de órdenes antiguas. Si tienes órdenes históricas preexistentes, las nuevas órdenes/ratings siguen pudiendo cargarse; la reconstrucción de snapshots requerirá un tratamiento adicional de esas órdenes. No borrar datos para evitar esa validación.

En el ensayo sin historia previa de 2025 se generaron 1.007 snapshots para 19 servicios. La segunda ejecución insertó cero porque ya coincidían.

**Limitación ajena al seed:** `MetricsMapper.toAggregated` usa el stock del último snapshot de la lista. Para métricas del proveedor, la lista mezcla varios servicios y no suma todos sus últimos stocks: ese endpoint puede devolver totalOrders/estados de un solo servicio aunque los snapshots estén bien. Los flujos (new_orders/vistas) sí se suman. Si vas a usar KPIs por profesional, corrige esa agregación aparte; no compenses escribiendo conteos falsos en los snapshots.

## Paso 9. Comprobar por las APIs y la interfaz

Con las instancias de desarrollo apuntando a las copias:

- Consulta las órdenes por cliente, profesional y servicio y comprueba las fechas y estados.
- `GET /rating/order?orderId=ID` debe mostrar tres ratings para una completada del manifiesto.
- `GET /score/service?serviceId=ID` debe devolver promedio y count reales, no provisional.
- `GET /score/user?userId=ID` debe concordar con la suma/conteo de sus ratings.
- Consulta los endpoints de historial/métricas de 2025 si ejecutaste snapshots.

El seed directo reproduce el **estado persistido**, no las notificaciones históricas, SSE ni eventos EventBridge. Eso evita correos/notificaciones artificiales. No es un replay de eventos de producción.

## Pruebas realizadas

Con PostgreSQL local y las migraciones reales:

- 19 servicios, 8 usuarios, direcciones, agendas y un bloqueo de fechas.
- Conservación de una orden previa de 2026.
- Planificación sin DML, inserción y verificación de 950 órdenes nuevas y 1.926 ratings.
- Segunda aplicación: cero órdenes y cero ratings nuevos.
- Snapshots: 1.007 la primera vez, cero la segunda.
- Comprobaciones SQL: cero cruces por proveedor y cero incoherencias básicas de cronología en el ensayo.
- Reanudación con Catalog ya cargado y Rate todavía vacío: cero órdenes duplicadas y 1.926 ratings recuperados correctamente.
- Compensación al introducir 200 órdenes previas adicionales no completadas: el plan añadió órdenes completadas extra para mantener el porcentaje sobre el total final.
- Cambio del estado base después de planificar: el script rechazó apply antes de escribir.
- Simulación opcional de edades: ROLLBACK conservó las ocho fechas originales del ensayo.

Estas pruebas no sustituyen ejecutar primero contra tus copias restauradas con tus datos reales. No he realizado ningún cambio en tu infraestructura.
