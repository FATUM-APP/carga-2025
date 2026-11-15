\set ON_ERROR_STOP on
\echo 'CatalogService: verificación de carga 2025 (solo lectura)'
BEGIN;
SET TRANSACTION READ ONLY;

\echo '1) Alcance: deben existir exactamente los 19 servicios preexistentes; no se verifica identidad histórica sin manifest pre-seed'
SELECT
    CASE WHEN COUNT(*) = 19 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS service_count,
    19 AS expected_service_count
FROM services;

WITH expected(service_id) AS (
    VALUES
        ('38a11e3a-ad1b-44e6-98a3-0d8aa75f5022'),
        ('808f2daf-2abd-4f43-b325-c2d3edc6302b'),
        ('821dd5e6-914f-4bc4-a8f8-e081a6564674'),
        ('6c799815-39d0-44a4-b4ea-16b760c86939'),
        ('12c6e856-a37a-4658-a598-57b4258d0548'),
        ('72d75419-6c4b-4e64-ac7d-bcc4ccc7f7bc'),
        ('b44b034e-d42d-42ef-b2a8-9925e237c269'),
        ('71c4e773-b296-4484-bac9-ad9841ddf0f8'),
        ('2a123683-cb69-41d2-9f03-9ba7e7303fdb'),
        ('391d798d-b9b2-4694-b978-a83d70650948'),
        ('db7c10b9-3f42-4484-9ed0-09927797da7f'),
        ('5b3ca7c5-385c-4d13-bbb5-4e51bf8c85b9'),
        ('c8a8cbe9-1423-47ee-9c9d-cb6ca024a0e1'),
        ('4b9f7fb6-653d-4baa-9ea0-1ff84c7bf4e0'),
        ('5085742f-f223-46c5-a5c9-a83993c2cd84'),
        ('5a535c11-6b1e-410e-a0db-f1eaed4feb3f'),
        ('a7f200c8-2e91-4524-9e1f-bc7e78480b93'),
        ('c03381ad-f9ba-4cea-910c-3f5c3fa776cb'),
        ('3cafe36c-b82b-48ab-ac7a-a2a6ace1d4dd')
), actual AS (
    SELECT id AS service_id FROM services
), missing AS (
    SELECT e.service_id FROM expected e LEFT JOIN actual a USING (service_id)
    WHERE a.service_id IS NULL
), unexpected AS (
    SELECT a.service_id FROM actual a LEFT JOIN expected e USING (service_id)
    WHERE e.service_id IS NULL
)
SELECT
    CASE WHEN (SELECT COUNT(*) FROM missing) = 0
            AND (SELECT COUNT(*) FROM unexpected) = 0
         THEN 'PASS' ELSE 'FAIL' END AS check,
    (SELECT COUNT(*) FROM missing) AS missing_expected_services,
    (SELECT COUNT(*) FROM unexpected) AS unexpected_services;

\echo 'Manifest de servicios e imágenes: guardar antes y después y comparar fila por fila; este script no puede conocer el baseline'
SELECT
    s.id AS service_id,
    s.provider_id,
    s.title,
    s.status,
    COUNT(sp.id) AS image_count,
    md5(COALESCE(
        string_agg(
            concat_ws('|', sp.id, sp.image_key, sp.original_filename, sp.content_type, sp.file_size::text),
            E'\\n' ORDER BY sp.id
        ),
        ''
    )) AS image_manifest_md5
FROM services s
LEFT JOIN service_portafolio sp ON sp.service_id = s.id
GROUP BY s.id, s.provider_id, s.title, s.status
ORDER BY s.id;

\echo '2) Cohorte histórica: created_at en 2025; booking también debe quedar en 2025'
WITH service_2025 AS (
    SELECT
        s.id AS service_id,
        s.title,
        COUNT(o.id) AS total_2025_created,
        COUNT(o.id) FILTER (WHERE o.order_status = 'COMPLETED') AS completed_2025_created,
        COUNT(o.id) FILTER (WHERE o.order_status = 'REJECTED') AS rejected_2025_created,
        COUNT(o.id) FILTER (WHERE o.order_status = 'CANCELLED') AS cancelled_2025_created,
        COUNT(o.id) FILTER (WHERE o.order_status NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')) AS other_status_2025_created,
        COUNT(o.id) FILTER (
            WHERE o.booking >= TIMESTAMP '2025-01-01'
              AND o.booking < TIMESTAMP '2026-01-01'
        ) AS booking_2025,
        COUNT(o.id) FILTER (
            WHERE o.booking < TIMESTAMP '2025-01-01'
               OR o.booking >= TIMESTAMP '2026-01-01'
        ) AS booking_outside_2025
    FROM services s
    LEFT JOIN orders o
      ON o.service = s.id
     AND o.created_at >= TIMESTAMP '2025-01-01'
     AND o.created_at <  TIMESTAMP '2026-01-01'
    GROUP BY s.id, s.title
)
SELECT
    service_id,
    title,
    total_2025_created,
    completed_2025_created,
    rejected_2025_created,
    cancelled_2025_created,
    other_status_2025_created,
    booking_2025,
    booking_outside_2025,
    ROUND(completed_2025_created::numeric / NULLIF(total_2025_created, 0), 4) AS completed_share,
    CASE
        WHEN total_2025_created >= 50
         AND completed_2025_created::numeric >= CEIL(total_2025_created * 0.40)
         AND booking_outside_2025 = 0
         AND other_status_2025_created = 0
        THEN 'PASS' ELSE 'FAIL'
    END AS check
FROM service_2025
ORDER BY service_id;

\echo 'Resumen de mínimos y proporción completada de la cohorte 2025'
WITH service_2025 AS (
    SELECT
        s.id AS service_id,
        COUNT(o.id) AS total_orders,
        COUNT(o.id) FILTER (WHERE o.order_status = 'COMPLETED') AS completed_orders
    FROM services s
    LEFT JOIN orders o
      ON o.service = s.id
     AND o.created_at >= TIMESTAMP '2025-01-01'
     AND o.created_at <  TIMESTAMP '2026-01-01'
    GROUP BY s.id
)
SELECT
    CASE WHEN COUNT(*) = 19 THEN 'PASS' ELSE 'FAIL' END AS services_present_check,
    COUNT(*) AS services_present,
    COUNT(*) FILTER (WHERE total_orders >= 50) AS services_meeting_minimum,
    COUNT(*) FILTER (
        WHERE total_orders >= 50
          AND completed_orders::numeric >= CEIL(total_orders * 0.40)
    ) AS services_meeting_minimum_and_completed_share,
    CASE WHEN COUNT(*) FILTER (
        WHERE total_orders < 50
           OR completed_orders::numeric < CEIL(total_orders * 0.40)
    ) = 0 THEN 'PASS' ELSE 'FAIL' END AS all_services_check
FROM service_2025;

\echo 'Comparación de total final actual: útil si el criterio 40% se interpreta sobre todas las órdenes, no solo la cohorte 2025'
SELECT
    s.id AS service_id,
    s.title,
    COUNT(o.id) AS total_orders_all_time,
    COUNT(o.id) FILTER (WHERE o.order_status = 'COMPLETED') AS completed_orders_all_time,
    ROUND(
        COUNT(o.id) FILTER (WHERE o.order_status = 'COMPLETED')::numeric
        / NULLIF(COUNT(o.id), 0),
        4
    ) AS completed_share_all_time,
    CASE
        WHEN COUNT(o.id) > 0
         AND COUNT(o.id) FILTER (WHERE o.order_status = 'COMPLETED')::numeric >= CEIL(COUNT(o.id) * 0.40)
        THEN 'PASS' ELSE 'FAIL'
    END AS check
FROM services s
LEFT JOIN orders o ON o.service = s.id
GROUP BY s.id, s.title
ORDER BY s.id;

\echo '3) Fechas: en las filas sembradas, created_at y updated_at deben ser históricas y booking debe ser 2025'
SELECT
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS rows_with_date_outside_2025
FROM orders o
WHERE o.created_at >= TIMESTAMP '2025-01-01'
  AND o.created_at <  TIMESTAMP '2026-01-01'
  AND (
       o.booking < TIMESTAMP '2025-01-01'
    OR o.booking >= TIMESTAMP '2026-01-01'
    OR o.updated_at < TIMESTAMP '2025-01-01'
    OR o.updated_at >= TIMESTAMP '2026-01-01'
  );

SELECT
    COUNT(*) AS rows_created_2025,
    COUNT(*) FILTER (
        WHERE booking >= TIMESTAMP '2025-01-01' AND booking < TIMESTAMP '2026-01-01'
    ) AS rows_booking_2025,
    COUNT(*) FILTER (
        WHERE updated_at >= TIMESTAMP '2025-01-01' AND updated_at < TIMESTAMP '2026-01-01'
    ) AS rows_updated_2025
FROM orders
WHERE created_at >= TIMESTAMP '2025-01-01'
  AND created_at <  TIMESTAMP '2026-01-01';

\echo '4) Solapes: se comprueba toda la cohorte created_at-2025, incluso estados no bloqueantes, por el requisito de no solapar toda la carga'
WITH cohort AS (
    SELECT
        o.id,
        o.provider_id,
        o.service,
        o.booking,
        o.estimated_time,
        o.order_status,
        o.booking + make_interval(mins => COALESCE(o.estimated_time, 30)) AS ends_at
    FROM orders o
    WHERE o.created_at >= TIMESTAMP '2025-01-01'
      AND o.created_at <  TIMESTAMP '2026-01-01'
), overlapping_orders AS (
    SELECT a.id AS order_a, b.id AS order_b, a.provider_id,
           a.service AS service_a, b.service AS service_b,
           a.booking AS booking_a, a.ends_at AS ends_a,
           b.booking AS booking_b, b.ends_at AS ends_b
    FROM cohort a
    JOIN cohort b
      ON a.provider_id = b.provider_id
     AND a.id < b.id
     AND a.booking < b.ends_at
     AND b.booking < a.ends_at
)
SELECT
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS overlapping_pairs
FROM overlapping_orders;

WITH cohort AS (
    SELECT
        o.id,
        o.provider_id,
        o.service,
        o.booking,
        o.order_status,
        o.booking + make_interval(mins => COALESCE(o.estimated_time, 30)) AS ends_at
    FROM orders o
    WHERE o.created_at >= TIMESTAMP '2025-01-01'
      AND o.created_at <  TIMESTAMP '2026-01-01'
)
SELECT a.order_id AS order_a, b.order_id AS order_b, a.provider_id,
       a.service_id AS service_a, b.service_id AS service_b,
       a.booking AS booking_a, a.ends_at AS ends_a,
       b.booking AS booking_b, b.ends_at AS ends_b,
       a.status AS status_a, b.status AS status_b
FROM (
    SELECT id AS order_id, provider_id, service AS service_id, booking,
           ends_at, order_status AS status
    FROM cohort
) a
JOIN (
    SELECT id AS order_id, provider_id, service AS service_id, booking,
           ends_at, order_status AS status
    FROM cohort
) b
  ON a.provider_id = b.provider_id
 AND a.order_id < b.order_id
 AND a.booking < b.ends_at
 AND b.booking < a.ends_at
ORDER BY a.provider_id, a.booking, b.booking;

\echo '5) Horario semanal: DAY es el enum Java DayOfWeek en mayúsculas (MONDAY...SUNDAY)'
WITH cohort AS (
    SELECT
        o.id,
        o.provider_id,
        o.service,
        o.booking,
        o.estimated_time,
        o.booking + make_interval(mins => COALESCE(o.estimated_time, 30)) AS ends_at
    FROM orders o
    WHERE o.created_at >= TIMESTAMP '2025-01-01'
      AND o.created_at <  TIMESTAMP '2026-01-01'
), violations AS (
    SELECT
        c.id AS order_id,
        c.provider_id,
        c.service,
        c.booking,
        c.ends_at,
        CASE EXTRACT(ISODOW FROM c.booking)::int
            WHEN 1 THEN 'MONDAY'
            WHEN 2 THEN 'TUESDAY'
            WHEN 3 THEN 'WEDNESDAY'
            WHEN 4 THEN 'THURSDAY'
            WHEN 5 THEN 'FRIDAY'
            WHEN 6 THEN 'SATURDAY'
            WHEN 7 THEN 'SUNDAY'
        END AS expected_day,
        a.day AS actual_day,
        a.initial_time,
        a.final_time,
        CASE
            WHEN a.provider_id IS NULL THEN 'NO_AVAILABILITY_ROW'
            WHEN c.booking::date <> c.ends_at::date THEN 'CROSSES_MIDNIGHT'
            WHEN c.booking::time < a.initial_time OR c.ends_at::time > a.final_time THEN 'OUT_OF_SCHEDULE'
            ELSE NULL
        END AS reason
    FROM cohort c
    LEFT JOIN owner_availability a
      ON a.provider_id = c.provider_id
     AND a.day = CASE EXTRACT(ISODOW FROM c.booking)::int
                    WHEN 1 THEN 'MONDAY'
                    WHEN 2 THEN 'TUESDAY'
                    WHEN 3 THEN 'WEDNESDAY'
                    WHEN 4 THEN 'THURSDAY'
                    WHEN 5 THEN 'FRIDAY'
                    WHEN 6 THEN 'SATURDAY'
                    WHEN 7 THEN 'SUNDAY'
                  END
    WHERE a.provider_id IS NULL
       OR c.booking::date <> c.ends_at::date
       OR c.booking::time < a.initial_time
       OR c.ends_at::time > a.final_time
)
SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
       COUNT(*) AS schedule_violations
FROM violations;

WITH cohort AS (
    SELECT
        o.id,
        o.provider_id,
        o.service,
        o.booking,
        o.booking + make_interval(mins => COALESCE(o.estimated_time, 30)) AS ends_at
    FROM orders o
    WHERE o.created_at >= TIMESTAMP '2025-01-01'
      AND o.created_at <  TIMESTAMP '2026-01-01'
)
SELECT
    c.id AS order_id,
    c.provider_id,
    c.service,
    c.booking,
    c.ends_at,
    CASE EXTRACT(ISODOW FROM c.booking)::int
        WHEN 1 THEN 'MONDAY'
        WHEN 2 THEN 'TUESDAY'
        WHEN 3 THEN 'WEDNESDAY'
        WHEN 4 THEN 'THURSDAY'
        WHEN 5 THEN 'FRIDAY'
        WHEN 6 THEN 'SATURDAY'
        WHEN 7 THEN 'SUNDAY'
    END AS expected_day,
    a.day,
    a.initial_time,
    a.final_time
FROM cohort c
LEFT JOIN owner_availability a
  ON a.provider_id = c.provider_id
 AND a.day = CASE EXTRACT(ISODOW FROM c.booking)::int
                WHEN 1 THEN 'MONDAY'
                WHEN 2 THEN 'TUESDAY'
                WHEN 3 THEN 'WEDNESDAY'
                WHEN 4 THEN 'THURSDAY'
                WHEN 5 THEN 'FRIDAY'
                WHEN 6 THEN 'SATURDAY'
                WHEN 7 THEN 'SUNDAY'
              END
WHERE a.provider_id IS NULL
   OR c.booking::date <> c.ends_at::date
   OR c.booking::time < a.initial_time
   OR c.ends_at::time > a.final_time
ORDER BY c.provider_id, c.booking;

\echo '6) Indisponibilidad: se comprueba tanto el instante de booking (la misma regla del servicio) como intersección del intervalo completo'
WITH cohort AS (
    SELECT
        o.id,
        o.provider_id,
        o.service,
        o.booking,
        o.booking + make_interval(mins => COALESCE(o.estimated_time, 30)) AS ends_at
    FROM orders o
    WHERE o.created_at >= TIMESTAMP '2025-01-01'
      AND o.created_at <  TIMESTAMP '2026-01-01'
), booking_in_range AS (
    SELECT DISTINCT c.id
    FROM cohort c
    JOIN provider_unavailable_dates u
      ON u.provider_id = c.provider_id
     AND c.booking >= u.start_date
     AND c.booking <= u.final_date
), interval_intersection AS (
    SELECT DISTINCT c.id
    FROM cohort c
    JOIN provider_unavailable_dates u
      ON u.provider_id = c.provider_id
     AND c.booking < u.final_date
     AND c.ends_at > u.start_date
)
SELECT 'booking_in_unavailability' AS check_name,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
       COUNT(*) AS violations
FROM booking_in_range
UNION ALL
SELECT 'interval_intersects_unavailability',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       COUNT(*)
FROM interval_intersection;

WITH cohort AS (
    SELECT
        o.id,
        o.provider_id,
        o.service,
        o.booking,
        o.booking + make_interval(mins => COALESCE(o.estimated_time, 30)) AS ends_at
    FROM orders o
    WHERE o.created_at >= TIMESTAMP '2025-01-01'
      AND o.created_at <  TIMESTAMP '2026-01-01'
)
SELECT c.id AS order_id, c.provider_id, c.service, c.booking, c.ends_at,
       u.id AS unavailable_id, u.start_date, u.final_date, u.reason
FROM cohort c
JOIN provider_unavailable_dates u
  ON u.provider_id = c.provider_id
 AND c.booking < u.final_date
 AND c.ends_at > u.start_date
ORDER BY c.provider_id, c.booking;

\echo '7) IDs y campos de snapshot: Catalog usa UUID público de UserService; no existe FK cross-DB, por eso se reportan nulos/errores locales'
SELECT
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS invalid_order_rows
FROM orders o
WHERE o.created_at >= TIMESTAMP '2025-01-01'
  AND o.created_at <  TIMESTAMP '2026-01-01'
  AND (
       o.client_id IS NULL OR o.client_id = ''
    OR o.provider_id IS NULL OR o.provider_id = ''
    OR o.service IS NULL OR o.service = ''
    OR o.client_name IS NULL OR o.client_username IS NULL
    OR o.delivery_city IS NULL OR o.delivery_state IS NULL
    OR o.country IS NULL OR o.residence IS NULL
    OR o.estimated_time < 0 OR o.estimated_time > 1440
  );

SELECT
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS rows_with_country_outside_colombia
FROM orders
WHERE created_at >= TIMESTAMP '2025-01-01'
  AND created_at <  TIMESTAMP '2026-01-01'
  AND lower(btrim(country)) <> 'colombia';

SELECT
    order_status,
    COUNT(*) AS count
FROM orders
WHERE created_at >= TIMESTAMP '2025-01-01'
  AND created_at <  TIMESTAMP '2026-01-01'
GROUP BY order_status
ORDER BY order_status;

\echo 'Fin: ROLLBACK garantiza que el script nunca escribe en CatalogService'
ROLLBACK;
