\set ON_ERROR_STOP on
\echo 'RateService: verificación de snapshots y ratings de la carga 2025 (solo lectura)'
BEGIN;
SET TRANSACTION READ ONLY;
SET LOCAL TIME ZONE 'America/Bogota';

\echo '1) Conteos básicos: los snapshots de orden solo deben representar órdenes completadas'
SELECT
    COUNT(*) AS order_snapshots,
    COUNT(*) FILTER (WHERE is_completed) AS completed_snapshots,
    COUNT(*) FILTER (WHERE NOT is_completed) AS incomplete_snapshots,
    CASE WHEN COUNT(*) FILTER (WHERE NOT is_completed) = 0 THEN 'PASS' ELSE 'FAIL' END AS incomplete_snapshot_check
FROM order_snapshots;

SELECT
    COUNT(*) AS ratings,
    COUNT(*) FILTER (WHERE target_type = 'SERVICE') AS service_ratings,
    COUNT(*) FILTER (WHERE target_type = 'SERVICE' AND updated) AS service_ratings_updated,
    COUNT(*) FILTER (WHERE target_type IN ('CLIENT', 'PROFESSIONAL')) AS user_ratings,
    COUNT(*) FILTER (WHERE target_type NOT IN ('CLIENT', 'PROFESSIONAL', 'SERVICE')) AS unknown_target_types
FROM ratings;

\echo '2) Integridad por orden completada: exactamente CLIENT, PROFESSIONAL y SERVICE, con IDs/rater correctos'
WITH per_order AS (
    SELECT
        o.order_id,
        o.service_id,
        o.client_id,
        o.provider_id,
        COUNT(r.id) AS rating_count,
        COUNT(DISTINCT r.target_type) AS target_type_count,
        COUNT(r.id) FILTER (
            WHERE r.target_type = 'SERVICE'
              AND r.target_id = o.service_id
              AND r.rater_id = o.client_id
        ) AS service_rows,
        COUNT(r.id) FILTER (
            WHERE r.target_type = 'SERVICE'
              AND r.target_id = o.service_id
              AND r.rater_id = o.client_id
              AND r.updated
        ) AS service_updated_rows,
        COUNT(r.id) FILTER (
            WHERE r.target_type = 'PROFESSIONAL'
              AND r.target_id = o.provider_id
              AND r.rater_id = o.client_id
        ) AS professional_rows,
        COUNT(r.id) FILTER (
            WHERE r.target_type = 'CLIENT'
              AND r.target_id = o.client_id
              AND r.rater_id = o.provider_id
        ) AS client_rows,
        COUNT(r.id) FILTER (WHERE r.target_type = 'SERVICE' AND r.updated) AS any_updated_service_rows
    FROM order_snapshots o
    LEFT JOIN ratings r ON r.order_id = o.order_id
    WHERE o.is_completed
    GROUP BY o.order_id, o.service_id, o.client_id, o.provider_id
)
SELECT
    order_id,
    service_id,
    client_id,
    provider_id,
    rating_count,
    target_type_count,
    service_rows,
    service_updated_rows,
    professional_rows,
    client_rows,
    CASE
        WHEN service_id IS NOT NULL
         AND client_id IS NOT NULL
         AND provider_id IS NOT NULL
         AND rating_count = 3
         AND target_type_count = 3
         AND service_rows = 1
         AND service_updated_rows = 1
         AND professional_rows = 1
         AND client_rows = 1
        THEN 'PASS' ELSE 'FAIL'
    END AS check
FROM per_order
ORDER BY order_id;

WITH per_order AS (
    SELECT
        o.order_id,
        o.service_id,
        o.client_id,
        o.provider_id,
        COUNT(r.id) AS rating_count,
        COUNT(DISTINCT r.target_type) AS target_type_count,
        COUNT(r.id) FILTER (
            WHERE r.target_type = 'SERVICE'
              AND r.target_id = o.service_id
              AND r.rater_id = o.client_id
              AND r.updated
        ) AS service_updated_rows,
        COUNT(r.id) FILTER (
            WHERE r.target_type = 'PROFESSIONAL'
              AND r.target_id = o.provider_id
              AND r.rater_id = o.client_id
        ) AS professional_rows,
        COUNT(r.id) FILTER (
            WHERE r.target_type = 'CLIENT'
              AND r.target_id = o.client_id
              AND r.rater_id = o.provider_id
        ) AS client_rows
    FROM order_snapshots o
    LEFT JOIN ratings r ON r.order_id = o.order_id
    WHERE o.is_completed
    GROUP BY o.order_id, o.service_id, o.client_id, o.provider_id
), failures AS (
    SELECT * FROM per_order
    WHERE service_id IS NULL
       OR client_id IS NULL
       OR provider_id IS NULL
       OR rating_count <> 3
       OR target_type_count <> 3
       OR service_updated_rows <> 1
       OR professional_rows <> 1
       OR client_rows <> 1
)
SELECT
    CASE WHEN COUNT(*) = 0 AND (SELECT COUNT(*) FROM per_order) > 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    (SELECT COUNT(*) FROM per_order) AS completed_orders_checked,
    COUNT(*) AS failed_completed_orders
FROM failures;

\echo '3) SERVICE rated updated=true: resumen por service_id; el cruce contra services.id debe hacerse desde el verificador externo'
SELECT
    r.target_id AS service_id,
    COUNT(DISTINCT r.order_id) AS completed_orders_with_service_rating,
    COUNT(*) AS service_rating_rows,
    COUNT(*) FILTER (WHERE r.updated) AS service_rating_rows_updated,
    COUNT(*) FILTER (WHERE NOT r.updated) AS service_rating_rows_not_updated,
    CASE
        WHEN COUNT(*) > 0 AND COUNT(*) FILTER (WHERE NOT r.updated) = 0
        THEN 'PASS' ELSE 'FAIL'
    END AS check
FROM ratings r
JOIN order_snapshots o ON o.order_id = r.order_id AND o.is_completed
WHERE r.target_type = 'SERVICE'
GROUP BY r.target_id
ORDER BY r.target_id;

SELECT
    COUNT(*) AS completed_orders,
    COUNT(*) FILTER (
        WHERE EXISTS (
            SELECT 1
            FROM ratings r
            WHERE r.order_id = o.order_id
              AND r.target_type = 'SERVICE'
              AND r.target_id = o.service_id
              AND r.rater_id = o.client_id
              AND r.updated
        )
    ) AS completed_orders_with_updated_service_rating,
    CASE
        WHEN COUNT(*) > 0
         AND COUNT(*) FILTER (
             WHERE EXISTS (
                 SELECT 1
                 FROM ratings r
                 WHERE r.order_id = o.order_id
                   AND r.target_type = 'SERVICE'
                   AND r.target_id = o.service_id
                   AND r.rater_id = o.client_id
                   AND r.updated
             )
         ) = COUNT(*)
        THEN 'PASS' ELSE 'FAIL'
    END AS all_completed_service_rating_check
FROM order_snapshots o
WHERE o.is_completed;

\echo '4) La cohorte Rate histórica se identifica por created_at/updated_at de ratings; snapshots no tienen fecha propia'
WITH rating_orders_2025 AS (
    SELECT
        r.order_id,
        COUNT(*) AS ratings_created_2025,
        COUNT(*) FILTER (WHERE r.target_type = 'SERVICE' AND r.updated) AS updated_service_ratings_created_2025
    FROM ratings r
    WHERE r.created_at >= TIMESTAMPTZ '2025-01-01 00:00:00-05'
      AND r.created_at <  TIMESTAMPTZ '2026-01-01 00:00:00-05'
    GROUP BY r.order_id
)
SELECT
    COUNT(*) AS order_ids_with_rating_created_2025,
    COUNT(*) FILTER (WHERE ratings_created_2025 = 3) AS order_ids_with_exactly_3_ratings_created_2025,
    COUNT(*) FILTER (
        WHERE ratings_created_2025 = 3 AND updated_service_ratings_created_2025 = 1
    ) AS order_ids_with_3_and_updated_service_2025,
    CASE
        WHEN COUNT(*) > 0
         AND COUNT(*) FILTER (
            WHERE ratings_created_2025 <> 3 OR updated_service_ratings_created_2025 <> 1
        ) = 0
        THEN 'PASS' ELSE 'FAIL'
    END AS historical_rating_shape_check
FROM rating_orders_2025;

SELECT
    r.order_id,
    r.target_type,
    r.target_id,
    r.rater_id,
    r.score,
    r.updated,
    r.created_at,
    r.updated_at
FROM ratings r
WHERE r.created_at < TIMESTAMPTZ '2025-01-01 00:00:00-05'
   OR r.created_at >= TIMESTAMPTZ '2026-01-01 00:00:00-05'
   OR r.updated_at < TIMESTAMPTZ '2025-01-01 00:00:00-05'
   OR r.updated_at >= TIMESTAMPTZ '2026-01-01 00:00:00-05'
ORDER BY r.created_at, r.order_id, r.target_type;

\echo '5) Ratings huérfanos o snapshots que no corresponden a una orden completada'
SELECT
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS orphan_or_incomplete_rating_rows
FROM ratings r
LEFT JOIN order_snapshots o ON o.order_id = r.order_id
WHERE o.order_id IS NULL OR NOT o.is_completed;

SELECT
    r.order_id,
    r.target_type,
    r.target_id,
    r.rater_id,
    o.order_id AS snapshot_order_id,
    o.is_completed
FROM ratings r
LEFT JOIN order_snapshots o ON o.order_id = r.order_id
WHERE o.order_id IS NULL OR NOT o.is_completed
ORDER BY r.order_id, r.target_type;

\echo '6) Campos no nulos y timestamps; las constraints ya cubren score, pero se reportan los fallos de snapshots/eventos'
SELECT
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS invalid_completed_snapshot_rows
FROM order_snapshots o
WHERE o.is_completed
  AND (
       o.order_id IS NULL OR o.order_id = ''
    OR o.service_id IS NULL OR o.service_id = ''
    OR o.client_id IS NULL OR o.client_id = ''
    OR o.provider_id IS NULL OR o.provider_id = ''
  );

SELECT
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS check,
    COUNT(*) AS invalid_rating_rows
FROM ratings r
WHERE r.order_id IS NULL OR r.order_id = ''
   OR r.target_type IS NULL
   OR r.target_id IS NULL OR r.target_id = ''
   OR r.rater_id IS NULL OR r.rater_id = ''
   OR r.score < 1 OR r.score > 5
   OR r.created_at IS NULL
   OR r.updated_at IS NULL
   OR r.updated_at < r.created_at;

SELECT
    target_type,
    updated,
    COUNT(*) AS count,
    MIN(score) AS min_score,
    MAX(score) AS max_score,
    MIN(created_at) AS first_created_at,
    MAX(created_at) AS last_created_at,
    MIN(updated_at) AS first_updated_at,
    MAX(updated_at) AS last_updated_at
FROM ratings
GROUP BY target_type, updated
ORDER BY target_type, updated;

\echo '7) Coherencia de user_snapshots: total_score/ratings_count deben ser la suma/conteo de ratings CLIENT y PROFESSIONAL'
WITH expected AS (
    SELECT
        r.target_id AS user_id,
        COUNT(*)::integer AS expected_ratings_count,
        SUM(r.score)::bigint AS expected_total_score
    FROM ratings r
    WHERE r.target_type IN ('CLIENT', 'PROFESSIONAL')
    GROUP BY r.target_id
)
SELECT
    u.user_id,
    u.ratings_count,
    u.total_score,
    COALESCE(e.expected_ratings_count, 0) AS expected_ratings_count,
    COALESCE(e.expected_total_score, 0) AS expected_total_score,
    CASE
        WHEN u.ratings_count = COALESCE(e.expected_ratings_count, 0)
         AND u.total_score = COALESCE(e.expected_total_score, 0)
        THEN 'PASS' ELSE 'FAIL'
    END AS check
FROM user_snapshots u
LEFT JOIN expected e ON e.user_id = u.user_id
ORDER BY u.user_id;

WITH expected AS (
    SELECT
        r.target_id AS user_id,
        COUNT(*)::integer AS expected_ratings_count,
        SUM(r.score)::bigint AS expected_total_score
    FROM ratings r
    WHERE r.target_type IN ('CLIENT', 'PROFESSIONAL')
    GROUP BY r.target_id
)
SELECT
    e.user_id,
    e.expected_ratings_count,
    e.expected_total_score
FROM expected e
LEFT JOIN user_snapshots u ON u.user_id = e.user_id
WHERE u.user_id IS NULL
ORDER BY e.user_id;

\echo 'Fin: ROLLBACK garantiza que el script nunca escribe en RateService'
ROLLBACK;
