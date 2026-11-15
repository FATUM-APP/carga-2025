# Reemplazar el seed 2025 por un seed integral 2026

## Alcance y límites

Interpretación del intervalo solicitado: **del 1 de enero al 31 de octubre de 2026, ambos inclusive**, zona America/Bogota. No se generan órdenes, reservas, inicios, calificaciones, eventos o favoritos nuevos en noviembre/diciembre ni en 2025.

El seed anterior usó 2025 porque originalmente pediste datos del año pasado. Esta actualización permite retirar esa carga específica y crear una nueva del intervalo solicitado, incorporando comportamiento para probar tu agente.

**No ejecutar DELETE por año/fecha.** La retirada se basa en los IDs de tu `plan_2025.json`, no en todas las órdenes de 2025. Conserva las órdenes, ratings, favoritos, navegación, snapshots y demás filas ajenas a ese manifiesto. Si alguna fila de la carga fue modificada, la herramienta se detiene: no pisa cambios posteriores.

El agente no se crea en este paquete. Se preparan señales y referencias para evaluar su capacidad de inferir preferencias, sin escribir un «perfil» en UserService.

**Son datos sintéticos, no comportamiento real.** La restricción de 50 nuevas órdenes por cada uno de 19 servicios con sólo cinco clientes produce una muestra de actividad muy intensa. Úsala como prueba funcional/dataset de escenarios, no como muestra representativa de clientes reales, análisis causal ni ventas/reputación pública real.

## Archivos: qué agregar y qué conservar

En TU carpeta actual `carga-2025`, conserva:

- `seed_fatum.py`, sin editarlo: los nuevos scripts lo importan como módulo.
- `plan_2025.json`, el manifiesto que realmente aplicaste. **No lo regeneres ni sustituyas por uno del ZIP.**
- `alcance.json`, `requirements.txt`, `.venv` y tu configuración privada de conexión, sin cambios.

Agrega desde el paquete de actualización:

1. `retirar_seed_2025.py`: revisión, backup selectivo y retirada del seed por sus IDs.
2. `seed_2026.py`: planificación, carga de órdenes/ratings/comportamiento/snapshots y verificación.
3. `consultas_perfiles_2026.sql`: consultas de sólo lectura para explorar las señales por cliente.
4. `GUIA_ACTUALIZACION_2026.md`: esta guía.
5. `gitignore_adicional.txt`: líneas que debes añadir a tu `.gitignore`; no sustituir el que ya tengas.

**No necesitas modificar archivos de los microservicios ni agregar migraciones Flyway.** Se usan las tablas existentes. Tampoco necesitas reinstalar Python si tu entorno anterior sigue funcionando.

No reemplazar la carpeta completa: eso podría hacerte perder `.venv`, conexiones o manifiestos. Copia solamente los archivos nuevos dentro de tu carpeta actual.

## Antes de eliminar: backups y túneles

Mantén abiertos los tres túneles SSH que ya configuraste. En la terminal del proyecto:

```bash
source .venv/bin/activate
python -c "import os; print({k: bool(os.getenv(k)) for k in ('USERS_DSN','CATALOG_DSN','RATE_DSN')})"
```

Las tres variables deben mostrar True. Si abriste otra terminal, debes volver a definirlas o cargar tu `conexion.env` privado. En tu Mac apuntan a `127.0.0.1` y los puertos locales de los túneles, no directamente a RDS.

Confirma los destinos:

```bash
psql "$USERS_DSN" -c 'SELECT current_database(),current_user;'
psql "$CATALOG_DSN" -c 'SELECT current_database(),current_user;'
psql "$RATE_DSN" -c 'SELECT current_database(),current_user;'
```

Respaldar el estado ACTUAL antes de la retirada. Usar pg_dump de versión compatible con tu servidor (en el adjunto, PostgreSQL 18):

```bash
pg_dump "$CATALOG_DSN" -Fc -f catalog_antes_retiro_2025.dump
pg_dump "$RATE_DSN" -Fc -f rate_antes_retiro_2025.dump
pg_dump "$USERS_DSN" -Fc -f users_antes_retiro_2025.dump
```

No subas DSNs, contraseñas, claves SSH, dumps o manifiestos a GitHub. Si una contraseña quedó expuesta en conversaciones/historial, rótala y actualiza tu configuración privada.

Usa copias de prueba y pausa escrituras/consumidores/scheduler de métricas mientras haces revisión-retirada-plan-carga. Los advisory locks de los scripts coordinan ejecuciones propias; **no detienen tu app**.

## Paso A. Revisar la retirada: todavía no borra nada

```bash
python retirar_seed_2025.py plan \
  --file plan_2025.json \
  --cleanup-file retiro_2025.json
```

Este paso sólo lee las BDs y guarda un archivo privado con las filas seleccionadas y su identificación. El resumen debe señalar las órdenes/ratings/snapshots de la carga anterior. No debe incluir órdenes previas ajenas al manifiesto.

El snapshot de cierre del 31 de diciembre de 2025 usa semana ISO 1 de 2026. También se identifica por su UUID derivado del namespace original y se retira: **no confundirlo con un snapshot ajeno de 2026**.

La herramienta comprueba:

- Órdenes y ratings del manifiesto conservan sus valores.
- Snapshots de Rate corresponden a las completadas del seed.
- Snapshots de Catalog seleccionados tienen UUID y contenido esperados.
- No hay dependencias posteriores de las órdenes que obliguen a borrar información ajena.
- Los acumulados de usuarios son consistentes con todas sus calificaciones.

Si falla, no elimines registros manualmente para «hacerla pasar». Conserva el error, el manifiesto y los backups y revisa la diferencia. No se intenta adivinar ownership cuando falta el manifiesto.

## Paso B. Confirmar la retirada limitada al seed

Sólo después de revisar el resumen y tener los backups:

```bash
python retirar_seed_2025.py apply \
  --cleanup-file retiro_2025.json \
  --confirm-delete-seed-2025 \
  --test-database
```

Ese flag es tu autorización local explícita para borrar **las filas identificadas en `retiro_2025.json`**. No elimina por fecha.

Orden:

1. Rate: borra ratings y snapshots del seed; descuenta de `user_snapshots` únicamente las puntuaciones y cantidades de ratings realmente retirados. Mantiene esas filas de proyección, sus identidades y roles; sólo ajusta acumulados/metadatos de actualización.
2. Catalog: retira snapshots y órdenes propias, sin borrar datos secundarios ajenos.
3. Verifica ausencia de los IDs seleccionados y consistencia de los acumulados.

No elimina `service_metrics`: la carga 2025 no añadió vistas/permanencia ni favoritos nuevos y no es seguro atribuir la creación de sus filas sólo al seed. Conserva los contadores originales.

Como son dos BDs, no hay transacción global. Si Rate confirma y Catalog falla, **reanuda con el mismo `retiro_2025.json`**; no vuelvas a crear un plan de retiro que ya no encontrará la carga completa.

```bash
python retirar_seed_2025.py verify --cleanup-file retiro_2025.json
```

No es un comando para deshacer todo el sistema. Si quieres restaurar el estado anterior completo, utiliza los dumps con el procedimiento de restauración de tus copias.

## Paso C. Preparar el plan 2026: no escribe en las BDs

```bash
python seed_2026.py plan \
  --old-plan plan_2025.json \
  --file plan_2026.json \
  --profiles-file perfiles_esperados_2026.json
```

La nueva planificación exige que las órdenes, ratings y snapshots propios de 2025 ya hayan sido retirados. El manifiesto viejo se usa como alcance y prueba de qué carga se reemplazó; no se modifica.

Genera localmente:

- `plan_2026.json`: todas las filas a insertar, fingerprints del estado previo y contadores esperados.
- `perfiles_esperados_2026.json`: preferencias intencionales y señales realmente generadas para evaluar el agente.

No se sobrescriben archivos existentes. Después de una carga parcial, **no regeneres el manifiesto**. Continúa con el mismo.

### Qué simula

**Órdenes y ratings:** 50 órdenes nuevas por servicio, más completadas extra sólo si hacen falta para mantener el 40 % del total final. Distribución entre enero y octubre; Halloween se concentra en octubre. Agenda, bloqueos y solapes se validan, nunca se editan. Ratings finales SERVICE/PROFESSIONAL/CLIENT, con fechas dentro del mismo intervalo y puntuaciones distintas.

**Preferencias por cliente:** interés principal/secundario en categorías y preferencia de precio. Son asignaciones sintéticas explícitas, no inferencias de edad, género, salud u otros atributos sensibles. Influyen en qué servicios solicita, explora, marca y evalúa cada cliente. El volumen fijo por servicio limita cuánto puede parecerse la distribución a una población real.

**Navegación:** visitas previas a cada solicitud y sesiones exploratorias; permanencia más larga en categorías de interés y más breve en otras. Cada visita nueva representa una apertura del detalle (un GET que incrementaría views) seguida de un evento de permanencia. La API actual separa ambas operaciones: `trackViewEvent` por sí solo NO incrementa vistas.

**Favoritos:** añade variedad (objetivo 2–5 por cliente dentro del alcance), precedidos por una visita. Si el cliente ya tiene favoritos, se conservan y se cuenta esa cantidad al decidir cuántos agregar. Nunca duplica un par cliente/servicio ni elimina favoritos reales para forzar una etiqueta.

**Direcciones:** snapshot colombiano en órdenes; si no hay dirección principal válida, fallback sintético. No toca `addresses` ni `users`.

**Métricas:** snapshots semanales más corte parcial al 31 de octubre, con órdenes, favoritos, vistas y permanencia. Se generan dentro de la carga; ya no debes correr el `snapshots` del script 2025.

### Qué no modifica

No cambia UserService, direcciones, fechas de nacimiento, servicios, imágenes, categorías, precios de catálogo, disponibilidad ni indisponibilidad. No cambia órdenes/ratings/eventos/favoritos/snapshots ajenos a la carga. Sí suma las aportaciones nuevas a los acumulados de métricas y ratings: esas actualizaciones son necesarias para que sean coherentes.

No envía notificaciones ni eventos de EventBridge. No instrumenta el frontend. No inventa un historial de favoritos removidos: la tabla sólo guarda los favoritos que siguen presentes.

### Contadores históricos frente a contadores de la semana actual

`service_metrics` conserva sus valores originales y añade al acumulado global vistas/permanencia de la simulación y los favoritos nuevos. **No suma enero–octubre a `period_views`/`period_dwell_*` de la semana actual de noviembre**. Esos contadores de periodo vivo se conservan.

En snapshots, las vistas representan las visitas NUEVAS conocidas de la simulación. Los GET anteriores no tienen una bitácora individual y no se pueden repartir retrospectivamente con exactitud. La permanencia anterior sí puede incluirse si existen eventos fechados. No confundir ausencia de trazas con cero visitas reales históricas.

Si existen snapshots ajenos para las mismas semanas de enero–octubre, el plan aborta en lugar de sobrescribirlos. Si una orden ajena tuvo cambios que atraviesan un corte semanal, tampoco inventa su estado anterior: puede requerirse su historial para reconstruir el corte. Órdenes anteriores al corte con estado final ya conocido se incorporan al stock sin modificarlas.

## Paso D. Aplicar el plan 2026

Revisa las cantidades y los destinos que imprime `plan`. Después:

```bash
python seed_2026.py apply \
  --file plan_2026.json \
  --allow-historical-write \
  --test-database
```

Una transacción de Catalog inserta órdenes, eventos, favoritos y snapshots y aplica los incrementos de métricas. Una transacción posterior de Rate inserta sus snapshots/ratings y actualiza acumulados. No hay commit global entre las BDs.

Imprime progreso cada 500 filas preparadas. **«Preparadas» no significa confirmado**: espera los mensajes `CATALOG COMMIT` y `RATE COMMIT`, seguidos de la verificación final.

Si hay una interrupción, conserva el manifiesto y repite ese MISMO comando. Reconoce los IDs ya confirmados y no repite incrementos. No ejecutes dos copias de apply simultáneas ni cierres los túneles mientras trabaja.

El script valida el estado base nuevamente. Si cambió por tráfico, no continúa a ciegas. No generes otro plan encima de una carga parcial.

## Paso E. Verificar y explorar para el agente

```bash
python seed_2026.py verify --file plan_2026.json
psql "$CATALOG_DSN" -f consultas_perfiles_2026.sql
```

El verificador comprueba las filas del manifiesto, fechas, snapshots, incremento de métricas, favoritos y consistencia de puntuaciones, además de que el estado previo no cambió.

Las consultas SQL muestran:

- Eventos y tiempo por usuario/categoría.
- Solicitudes y completadas por usuario/categoría.
- Favoritos actuales y si fueron añadidos antes del corte de octubre.
- Servicios vistos, pedidos y favoritos, agregados antes de unirlos para no multiplicar filas.
- Promedio de permanencia por servicio y flujos históricos.

Para perfiles INDIVIDUALES, el agente necesita `service_view_event.user_id`, `client_favorites.client_id` y `orders.client_id`, junto con servicio/categoría y ratings. Los snapshots por servicio son agregados: no revelan por sí solos quién miró cada servicio. Puede leerlos por conexión autorizada, export o API de tu proyecto; este paquete no expone nuevos endpoints.

`perfiles_esperados_2026.json` es el oráculo de prueba. Guarda el archivo fuera de las entradas del agente cuando lo evalúes: compara su salida con las preferencias intencionales y las estadísticas realmente generadas. Si le das esas etiquetas como entrada, no estás probando si infiere preferencias a partir de comportamiento.

No interpretar un servicio sin favorito como prueba de desagrado, ni una permanencia larga como certeza de intención de compra. Las reglas de simulación inducen correlaciones por diseño; no demuestran comportamiento causal real.

### Limitación preexistente de KPIs por profesional

En `main` revisado, el agregador de métricas de proveedor toma el stock del último snapshot de una lista que mezcla servicios en vez de sumar el último de cada servicio. Esta actualización no modifica esa lógica. Para validar perfiles individuales usa eventos/órdenes/favoritos; no falsifiques snapshots para compensar un bug de agregación.

## Resumen de comandos (mantén los túneles abiertos)

```bash
# Sólo revisión y backup selectivo
python retirar_seed_2025.py plan --file plan_2025.json --cleanup-file retiro_2025.json

# Destructivo, limitado a IDs revisados, después de tus backups
python retirar_seed_2025.py apply --cleanup-file retiro_2025.json --confirm-delete-seed-2025 --test-database
python retirar_seed_2025.py verify --cleanup-file retiro_2025.json

# Nueva planificación, todavía sin DML
python seed_2026.py plan --old-plan plan_2025.json --file plan_2026.json --profiles-file perfiles_esperados_2026.json

# Nueva carga integral, incluida navegación y snapshots
python seed_2026.py apply --file plan_2026.json --allow-historical-write --test-database
python seed_2026.py verify --file plan_2026.json

# Explorar señales, sólo lectura
psql "$CATALOG_DSN" -f consultas_perfiles_2026.sql
```

**No vuelvas a aplicar el plan de 2025 ni ejecutes sus snapshots después de la retirada**, porque reintroducirías esos datos. No elimines ninguno de los tres manifiestos (`plan_2025.json`, `retiro_2025.json`, `plan_2026.json`).

## Pruebas

Los scripts se validan en PostgreSQL local de ensayo con las migraciones SQL reales. Esto no sustituye revisar los planes y probar primero contra copias restauradas de tus bases RDS. No se ha ejecutado ningún cambio en tu infraestructura desde esta conversación.
