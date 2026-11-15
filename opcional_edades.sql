-- SOLO en la copia de UserService. OPCIONAL, separado del generador de órdenes.
-- Por defecto simula el cambio y hace ROLLBACK. No modifica profesionales.
-- Ejecutar antes de plan si decides cambiar las edades.
\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE propuesta_edades(id text PRIMARY KEY,birth_date date);
INSERT INTO propuesta_edades VALUES
('59641cc7-5eb3-4934-8424-2b7ebef2bf5b','2004-03-12'),
('00fe57fe-b301-4d4d-bb3e-e3df11fa59b9','1996-08-14'),
('5b9b0e96-1ae5-464a-aa89-4200ef3e3e9e','1988-11-14'),
('86cd679c-60a4-46b8-b29b-699c6950916d','1960-06-21'),
('7feba530-646d-427a-88c5-5a901d744c0e','1977-04-10');
DO $$ BEGIN
 IF (SELECT count(*) FROM users u JOIN propuesta_edades p ON p.id=u.id WHERE u.role='CLIENT')<>5 THEN
  RAISE EXCEPTION 'Los cinco IDs deben existir y conservar rol CLIENT';
 END IF;
END $$;
SELECT u.id,u.username,u.birth_date AS anterior,p.birth_date AS propuesta,
 extract(year FROM age(DATE '2025-01-01',p.birth_date)) AS edad_inicio_2025
FROM users u JOIN propuesta_edades p ON p.id=u.id ORDER BY u.username;
-- Guardar este CSV local antes de decidir confirmar (contiene fechas personales).
\copy (SELECT u.id,u.username,u.birth_date FROM users u JOIN propuesta_edades p ON p.id=u.id) TO 'edades_antes_seed.csv' WITH CSV HEADER
UPDATE users u SET birth_date=p.birth_date FROM propuesta_edades p
WHERE u.id=p.id AND u.role='CLIENT';
-- Si aceptas la propuesta, reemplaza SOLO este ROLLBACK por COMMIT en tu copia local.
ROLLBACK;
