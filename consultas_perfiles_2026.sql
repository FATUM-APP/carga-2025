-- CatalogService, SOLO LECTURA. No crea perfiles ni modifica usuarios.
-- Intervalo inclusivo 2026-01-01 a 2026-10-31, hora local America/Bogota.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
SET LOCAL TIME ZONE 'America/Bogota';

\echo '1) Interés observado por usuario y categoría (eventos de permanencia, NO todos los GET reales)'
SELECT e.user_id,s.category,count(*) AS dwell_events,
       sum(e.dwell_time_seconds) AS dwell_seconds,
       round(avg(e.dwell_time_seconds),2) AS average_dwell_seconds,
       count(DISTINCT e.service_id) AS distinct_services,
       min(e.created_at) AS first_event,max(e.created_at) AS last_event
FROM service_view_event e JOIN services s ON s.id=e.service_id
WHERE e.created_at >= TIMESTAMP '2026-01-01'
  AND e.created_at < TIMESTAMP '2026-11-01'
GROUP BY e.user_id,s.category ORDER BY e.user_id,dwell_seconds DESC;

\echo '2) Reservas por usuario y categoría; fecha de solicitud, no fecha de pago'
SELECT o.client_id,o.client_username,s.category,count(*) AS requests,
       count(*) FILTER(WHERE o.order_status='COMPLETED') AS completed,
       count(*) FILTER(WHERE o.order_status='CANCELLED') AS cancelled,
       count(*) FILTER(WHERE o.order_status='REJECTED') AS rejected,
       round(avg(o.negotiated_price),2) AS average_negotiated_price,
       sum(o.negotiated_price) FILTER(WHERE o.order_status='COMPLETED') AS completed_order_amount
FROM orders o JOIN services s ON s.id=o.service
WHERE o.created_at >= TIMESTAMP '2026-01-01'
  AND o.created_at < TIMESTAMP '2026-11-01'
GROUP BY o.client_id,o.client_username,s.category ORDER BY o.client_id,requests DESC;

\echo '3) Favoritos ACTUALES: no hay historial de eliminaciones en el esquema'
SELECT f.client_id,f.service_id,s.title,s.category,f.created_at,
       f.created_at<TIMESTAMP '2026-11-01' AS added_by_october_cut
FROM client_favorites f JOIN services s ON s.id=f.service_id
ORDER BY f.client_id,f.created_at;

\echo '4) Servicios vistos, pedidos y favoritos: evitar multiplicar filas al unir eventos con órdenes'
WITH visits AS (
 SELECT user_id,service_id,count(*) AS events,sum(dwell_time_seconds) AS seconds
 FROM service_view_event WHERE created_at>='2026-01-01' AND created_at<'2026-11-01'
 GROUP BY user_id,service_id
), requests AS (
 SELECT client_id AS user_id,service AS service_id,count(*) AS orders,
        count(*) FILTER(WHERE order_status='COMPLETED') AS completed
 FROM orders WHERE created_at>='2026-01-01' AND created_at<'2026-11-01'
 GROUP BY client_id,service
)
SELECT v.user_id,v.service_id,s.title,s.category,v.events,v.seconds,
       round(v.seconds::numeric/nullif(v.events,0),2) AS avg_seconds,
       coalesce(r.orders,0) AS requests,coalesce(r.completed,0) AS completed,
       f.id IS NOT NULL AS is_current_favorite,
       f.id IS NOT NULL AND f.created_at<TIMESTAMP '2026-11-01' AS current_favorite_added_by_cut
FROM visits v JOIN services s ON s.id=v.service_id
LEFT JOIN requests r USING(user_id,service_id)
LEFT JOIN client_favorites f ON f.client_id=v.user_id AND f.service_id=v.service_id
ORDER BY v.user_id,v.seconds DESC;

\echo '5) Métricas vivas: promedio de permanencia por servicio'
SELECT s.id,s.title,m.total_views,m.favorite_count,m.total_dwell_seconds,m.dwell_event_count,
       round(m.total_dwell_seconds::numeric/nullif(m.dwell_event_count,0),2) AS avg_dwell_seconds
FROM service_metrics m JOIN services s ON s.id=m.service_id ORDER BY s.title;

\echo '6) Histórico enero-octubre: flujos se SUMAN; stocks no'
SELECT s.id,s.title,sum(h.views_in_period) AS synthetic_known_views,
       sum(h.dwell_seconds_in_period) AS recorded_dwell_seconds,
       sum(h.dwell_events_in_period) AS recorded_dwell_events,
       sum(h.new_orders_in_period) AS new_requests
FROM service_metrics_snapshot h JOIN services s ON s.id=h.service_id
WHERE h.snapshot_date>=DATE '2026-01-01' AND h.snapshot_date<=DATE '2026-10-31'
GROUP BY s.id,s.title ORDER BY s.title;
ROLLBACK;
