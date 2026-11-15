#!/usr/bin/env python3
"""Seed histórico Fatum. plan no escribe; apply exige autorización local explícita.
No crea servicios, no cambia imágenes, usuarios ni agendas. Solo usar datos de prueba.
"""
import argparse
import hashlib
import json
import math
import os
import random
import sys
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ZONE = ZoneInfo('America/Bogota')
DAYS = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']
COLOMBIA_LOCATIONS = [
    ('Bogotá D.C.', 'Cundinamarca'), ('Medellín', 'Antioquia'), ('Cali', 'Valle del Cauca'),
    ('Barranquilla', 'Atlántico'), ('Cartagena', 'Bolívar'), ('Bucaramanga', 'Santander'),
    ('Pereira', 'Risaralda'), ('Manizales', 'Caldas'), ('Santa Marta', 'Magdalena'),
    ('Ibagué', 'Tolima'), ('Villavicencio', 'Meta'), ('Neiva', 'Huila'),
    ('Pasto', 'Nariño'), ('Montería', 'Córdoba'), ('Armenia', 'Quindío'),
]
JSON_FIELDS = {'answers', 'details', 'fixed_questions'}
ORDER_FIELDS = ('id client_id client_name client_username provider_id booking service '
                'negotiated_price estimated_time delivery_city delivery_state country residence '
                'answers requirements order_status reason created_at delivery_started_at '
                'start_confirmed_by_customer start_confirmed_by_provider '
                'completion_confirmed_by_customer completion_confirmed_by_provider '
                'payment_confirmed_by_customer payment_confirmed_by_provider updated_at').split()
RATING_FIELDS = 'id order_id target_type target_id rater_id score details created_at updated updated_at'.split()
SNAP_FIELDS = 'order_id service_id client_id provider_id is_completed version'.split()
METRIC_FIELDS = ('id service_id year week snapshot_date views_in_period dwell_seconds_in_period '
                 'dwell_events_in_period new_orders_in_period favorite_count total_orders pending_orders '
                 'accepted_orders rejected_orders delivering_orders completed_orders cancelled_orders '
                 'not_delivered_orders dispute_related_orders created_at').split()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def serial(value):
    if isinstance(value, Decimal):
        return format(value, '.2f')
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise TypeError(str(type(value)))


def plain(value):
    return json.loads(json.dumps(value, default=serial, ensure_ascii=False))


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshot_address(client):
    """Copia la dirección principal si existe; si no, genera un snapshot colombiano.
    El fallback vive únicamente en la orden; nunca se inserta en addresses ni en users.
    """
    principal = client.get('principal') or []
    if len(principal) == 1:
        address = principal[0]
        values = [address.get('residence'), address.get('city'), address.get('state'), address.get('country')]
        if (all(isinstance(value, str) and value.strip() for value in values)
                and address['country'].strip().casefold() == 'colombia'):
            return ({'residence': address['residence'].strip(), 'city': address['city'].strip(),
                     'state': address['state'].strip(), 'country': 'Colombia'}, False)
    index = int(hashlib.sha256(client['id'].encode()).hexdigest(), 16) % len(COLOMBIA_LOCATIONS)
    city, state = COLOMBIA_LOCATIONS[index]
    return ({'residence': 'Dirección sintética de prueba, sin domicilio real',
             'city': city, 'state': state, 'country': 'Colombia'}, True)


def db(name, readonly=True):
    dsn = os.environ.get(name)
    require(dsn, f'Falta {name}. Usa un DSN PostgreSQL, no jdbc:postgresql.')
    require(not dsn.startswith('jdbc:'), f'{name} no acepta el prefijo jdbc:.')
    conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15)
    conn.execute("SET TIME ZONE 'America/Bogota'")
    conn.execute("SET search_path TO public")
    conn.execute("SET statement_timeout TO '120s'")
    conn.execute("SET lock_timeout TO '15s'")
    conn.commit()
    conn.read_only = readonly
    return conn


def rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def baseline(state):
    # Las proyecciones agregadas se validan contra ratings, no se usan como fuente primaria.
    keys = ['users', 'services', 'categories', 'availability', 'unavailable', 'existing_orders',
            'existing_ratings', 'existing_rate_orders']
    return digest({k: state[k] for k in keys})


def load_state(scope, exclude=()):
    ids = scope['service_ids']
    require(ids and len(ids) == len(set(ids)), 'Alcance de servicios vacío o duplicado.')
    exclude = list(exclude)
    with db('CATALOG_DSN') as c, db('USERS_DSN') as u, db('RATE_DSN') as r:
        for conn, minimum in [(c, 10), (u, 11), (r, 3)]:
            versions = rows(conn, 'SELECT version,success FROM flyway_schema_history')
            require(all(x['success'] for x in versions), 'Hay migraciones fallidas.')
            numeric = [int(x['version']) for x in versions if str(x['version']).isdigit()]
            require(numeric and max(numeric) >= minimum, f'Falta migración hasta V{minimum}.')
        constraints = rows(c, "SELECT conname FROM pg_constraint WHERE conrelid='services'::regclass AND conname='uk_services_provider_category'")
        old_indexes = rows(c, "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename='services' AND indexname='uk_services_provider_category'")
        require(not constraints and not old_indexes, 'V10 no eliminó la unicidad por categoría.')
        services = rows(c, 'SELECT * FROM services WHERE id=ANY(%s) ORDER BY id', (ids,))
        require(len(services) == len(ids), 'Hay servicios del alcance que no existen en esta BD.')
        providers = sorted({s['provider_id'] for s in services})
        user_ids = sorted(set(providers + scope['client_ids']))
        users = rows(u, '''SELECT u.id,u.complete_name,u.username,u.role,u.birth_date,u.is_active,
            u.verification_status, (SELECT jsonb_agg(jsonb_build_object('residence',a.residence,
            'city',a.city,'state',a.state,'country',a.country) ORDER BY a.id)
            FROM addresses a WHERE a.user_aws_id=u.aws_id AND a.position=0) AS principal
            FROM users u WHERE u.id=ANY(%s) ORDER BY u.id''', (user_ids,))
        require(len(users) == len(user_ids), 'Faltan usuarios internos del alcance.')
        categories = rows(c, 'SELECT * FROM categories ORDER BY name')
        availability = rows(c, 'SELECT * FROM owner_availability WHERE provider_id=ANY(%s) ORDER BY provider_id,day', (providers,))
        unavailable = rows(c, 'SELECT * FROM provider_unavailable_dates WHERE provider_id=ANY(%s) ORDER BY id', (providers,))
        orders = rows(c, 'SELECT * FROM orders WHERE provider_id=ANY(%s) AND NOT (id=ANY(%s)) ORDER BY id', (providers, exclude))
        ratings = rows(r, 'SELECT * FROM ratings WHERE NOT (order_id=ANY(%s)) ORDER BY id', (exclude,))
        rate_orders = rows(r, 'SELECT * FROM order_snapshots WHERE NOT (order_id=ANY(%s)) ORDER BY order_id', (exclude,))
        # comprobar si los acumulados existentes son consistentes ANTES de aplicar incrementos
        totals = {x['target_id']: x for x in rows(r, "SELECT target_id,sum(score) AS total,count(*) AS n FROM ratings WHERE target_type IN ('CLIENT','PROFESSIONAL') AND target_id=ANY(%s) GROUP BY target_id", (user_ids,))}
        snapshots = {x['user_id']: x for x in rows(r, 'SELECT * FROM user_snapshots WHERE user_id=ANY(%s)', (user_ids,))}
        for user in users:
            uid = user['id']
            expected = totals.get(uid, {'total': 0, 'n': 0})
            snap = snapshots.get(uid)
            require((snap is not None or expected['n'] == 0), f'Falta user_snapshot con ratings para {uid}; reconciliar primero.')
            if snap:
                require(snap['total_score'] == expected['total'] and snap['ratings_count'] == expected['n'], f'Acumulado inconsistente de {uid}; no se corregirá automáticamente.')
                require(snap['role'] == user['role'], f'Rol de RateService desactualizado para {uid}.')
            require(user['is_active'] and user['verification_status'] == 'VERIFIED', f'Usuario no válido: {user["username"]}.')
        for s in services:
            require(s['status'] == 'ACTIVE', f'Servicio no activo: {s["title"]}.')
            require(s['base_price'] >= 0, f'Precio negativo: {s["title"]}.')
            require(s['default_time'] is None or 0 <= s['default_time'] <= 1440, 'Duración del servicio inválida.')
            require(s['price_type'] in ('FIXED', 'HOURLY', 'STARTING_AT'), 'Tipo de precio desconocido.')
            require(s['job_type'] in ('PRESENCIAL', 'REMOTE', 'HYBRID'), 'Tipo de prestación desconocido.')
            count = rows(c, 'SELECT count(*) AS n FROM service_portafolio WHERE service_id=%s', (s['id'],))[0]['n']
            require(1 <= count <= 10, f'Galería ausente o inválida: {s["title"]}.')
        identities = {name: rows(conn, 'SELECT current_database() AS db')[0]['db'] for name, conn in [('Catalog', c), ('Users', u), ('Rate', r)]}
        return {'users': users, 'services': services, 'categories': categories,
                'availability': availability, 'unavailable': unavailable,
                'existing_orders': orders, 'existing_ratings': ratings,
                'existing_rate_orders': rate_orders, 'databases': identities}


def answers_for(service, rng):
    result, seen = [], set()
    for q in service.get('fixed_questions') or []:
        text = q['question']
        require(text.strip().lower() not in seen, f'Pregunta duplicada en {service["title"]}.')
        seen.add(text.strip().lower())
        kind = q['type']
        if kind in ('SINGLE_CHOICE', 'MULTIPLE_CHOICE'):
            require(len(q.get('options', [])) >= 2, 'Pregunta de opciones inválida.')
            answer = [rng.choice(q['options'])]
        elif kind == 'TEXT':
            # Datos sintéticos; no representa información de salud real.
            answer = [rng.choice(['Sin condiciones relevantes para este servicio.',
                                  'Ninguna alergia ni lesión reportada para esta prueba.'])]
        else:
            raise ValueError(f'Tipo de pregunta no soportado: {kind}.')
        result.append({'question': text.strip(), 'answer': answer})
    return result


def overlap(start, end, ranges):
    return any(start < b and a < end for a, b in ranges)


def find_slot(provider, client, preferred, duration, requested_duration, state, occupied_p, occupied_c, year):
    availability = {(x['provider_id'], x['day']): x for x in state['availability']}
    unavailable = [(x['start_date'], x['final_date']) for x in state['unavailable'] if x['provider_id'] == provider]
    low, high = date(year, 1, 8), date(year, 12, 28)
    span = (high - low).days + 1
    for offset in range(span):
        day = low + timedelta(days=((preferred - low).days + offset) % span)
        schedule = availability.get((provider, DAYS[day.weekday()]))
        if not schedule:
            continue
        initial = max(schedule['initial_time'], time(8))
        final = min(schedule['final_time'], time(20))
        booking = datetime.combine(day, initial)
        finish = datetime.combine(day, final)
        while booking + timedelta(minutes=max(duration, requested_duration)) <= finish:
            end = booking + timedelta(minutes=duration)
            guard_end = booking + timedelta(minutes=max(duration, requested_duration))
            forbidden = any(a <= booking <= b for a, b in unavailable)
            # También excluir una prestación que atraviese un bloqueo, criterio más conservador.
            forbidden = forbidden or any(booking <= b and a < guard_end for a, b in unavailable)
            if (not forbidden and not overlap(booking, guard_end, occupied_p[provider])
                    and not overlap(booking, end, occupied_c[client])):
                occupied_p[provider].append((booking, guard_end))
                occupied_c[client].append((booking, end))
                return booking
            booking += timedelta(minutes=15)
    raise ValueError(f'No hay capacidad en 2025 para profesional {provider}. No se alteraron agendas.')


def qualified_rate_ids(state):
    orders = {o['id']: o for o in state['existing_orders']}
    snapshots = {o['order_id']: o for o in state['existing_rate_orders']}
    qualified = set()
    for r in state['existing_ratings']:
        o, s = orders.get(r['order_id']), snapshots.get(r['order_id'])
        if (r['target_type'] == 'SERVICE' and r['updated'] and o and s and s['is_completed']
                and r['target_id'] == o['service'] == s['service_id']
                and r['rater_id'] == o['client_id'] == s['client_id']
                and o['provider_id'] == s['provider_id']):
            qualified.add(r['order_id'])
    return qualified


def report(manifest, state):
    ratings = qualified_rate_ids(state)
    out = []
    for s in manifest['services']:
        old = [o for o in state['existing_orders'] if o['service'] == s['id']]
        new = [o for o in manifest['orders'] if o['service'] == s['id']]
        rated_old = sum(o['order_status'] == 'COMPLETED' and o['id'] in ratings for o in old)
        completed_new = sum(o['order_status'] == 'COMPLETED' for o in new)
        total = len(old) + len(new)
        out.append({'service_id': s['id'], 'title': s['title'], 'provider': s['provider_username'],
                    'existing': len(old), 'new': len(new), 'total_final': total,
                    'new_completed': completed_new, 'completed_rated_final': rated_old + completed_new,
                    'percent_completed_rated': round(100 * (rated_old + completed_new) / total, 2)})
    return out


def plan(args):
    scope = json.loads(Path(args.scope).read_text(encoding='utf-8'))
    state = load_state(scope)
    rng = random.Random(args.seed)
    users = {u['id']: u for u in state['users']}
    cats = {c['name']: c for c in state['categories']}
    providers = {s['provider_id'] for s in state['services']}
    for uid in providers:
        require(users[uid]['role'] == 'PROFESSIONAL', 'Proveedor no profesional.')
    for uid in scope['client_ids']:
        require(users[uid]['role'] == 'CLIENT', 'Cliente del alcance cambió de rol.')
        require(users[uid]['birth_date'] <= date(args.year - 18, 1, 1), f'{users[uid]["username"]} no era adulto al inicio del año histórico.')
    occupied_p = {uid: [] for uid in providers}
    occupied_c = {uid: [] for uid in scope['client_ids']}
    addresses, fallback_users = {}, []
    for uid in scope['client_ids']:
        addresses[uid], used_fallback = snapshot_address(users[uid])
        if used_fallback:
            fallback_users.append(users[uid]['username'])
    for o in state['existing_orders']:
        if o['order_status'] not in ('REJECTED', 'CANCELLED'):
            span = (o['booking'], o['booking'] + timedelta(minutes=o['estimated_time'] or 30))
            occupied_p[o['provider_id']].append(span)
            if o['client_id'] in occupied_c:
                occupied_c[o['client_id']].append(span)
    orders, ratings = [], []
    rated = qualified_rate_ids(state)
    namespace = uuid.uuid4()  # Se conserva en manifiesto; reanudar con ese archivo, no con un plan nuevo.
    for number, s in enumerate(state['services']):
        cat = cats[s['category']]
        require(cat['scheduling_model'] == 'SINGLE_SESSION', 'Este generador admite categorías SINGLE_SESSION.')
        require(cat['is_able'] and not any(c['parent_name'] == cat['name'] for c in cats.values()), 'Categoría deshabilitada o no hoja.')
        parent = cat
        seen = set()
        while parent['parent_name']:
            require(parent['name'] not in seen, 'Ciclo en categorías.')
            seen.add(parent['name'])
            parent = cats[parent['parent_name']]
            require(parent['is_able'], 'Categoría padre deshabilitada.')
        old = [o for o in state['existing_orders'] if o['service'] == s['id']]
        old_rated = sum(o['order_status'] == 'COMPLETED' and o['id'] in rated for o in old)
        new_completed = 30 + (number % 5) * 2  # entre 60 y 76% de las primeras 50
        extra = max(0, math.ceil((Decimal('0.4') * (len(old) + 50) - old_rated - new_completed) / Decimal('0.6')))
        statuses = ['COMPLETED'] * (new_completed + extra) + ['REJECTED'] * (50 - new_completed - 5) + ['CANCELLED'] * 5
        rng.shuffle(statuses)
        for i, status in enumerate(statuses):
            client_id = scope['client_ids'][(i + number) % len(scope['client_ids'])]
            client = users[client_id]
            require(client_id != s['provider_id'], 'Autorreserva no permitida.')
            base_duration = s['default_time']
            duration = base_duration if base_duration and base_duration > 0 else rng.choice([30, 45, 60, 75])
            require(0 < duration <= 1440, 'Duración inválida.')
            requested = base_duration if base_duration is not None else 30
            # Distribución estratificada a lo largo del año, con mayor estacionalidad en Halloween.
            halloween = 'halloween' in s['title'].lower()
            if halloween:
                preferred = date(args.year, 10, 1) + timedelta(days=rng.randrange(30))
            else:
                preferred = date(args.year, 1, 8) + timedelta(days=int(i * 354 / len(statuses)))
            booking = find_slot(s['provider_id'], client_id, preferred, duration, requested, state, occupied_p, occupied_c, args.year)
            created = booking - timedelta(days=rng.randint(1, 6))
            accepted = created + timedelta(hours=2)
            started = booking if status == 'COMPLETED' else None
            updated = booking + timedelta(minutes=duration + 10) if started else created + timedelta(hours=rng.randint(3, 8))
            addr = addresses[client_id]
            price = s['base_price']
            if s['is_negotiable']:
                price = (price * rng.choice([Decimal('.9'), Decimal('.95'), Decimal('1'), Decimal('1.05'), Decimal('1.1')])).quantize(Decimal('.01'))
            oid = str(uuid.uuid5(namespace, f'{s["id"]}/{i}'))
            order = dict(id=oid, client_id=client_id, client_name=client['complete_name'],
                client_username=client['username'], provider_id=s['provider_id'], booking=booking,
                service=s['id'], negotiated_price=price, estimated_time=duration,
                delivery_city=addr['city'], delivery_state=addr['state'], country=addr['country'],
                residence=addr['residence'], answers=answers_for(s, rng),
                requirements='Solicitud sintética para pruebas de reportes.', order_status=status,
                reason={'REJECTED': 'Solicitud rechazada por incompatibilidad de condiciones.',
                        'CANCELLED': 'Cliente canceló antes de la aceptación.'}.get(status),
                created_at=created, updated_at=updated, delivery_started_at=started)
            for flag in ORDER_FIELDS:
                if '_confirmed_by_' in flag:
                    order[flag] = True if status == 'COMPLETED' else None
            orders.append(order)
            if started:
                for target, target_id, rater_id in [('SERVICE', s['id'], client_id),
                        ('PROFESSIONAL', s['provider_id'], client_id), ('CLIENT', client_id, s['provider_id'])]:
                    score = rng.choices([1, 2, 3, 4, 5], [2, 4, 14, 35, 45])[0]
                    ratings.append(dict(id=str(uuid.uuid5(namespace, f'{oid}/{target}')), order_id=oid,
                        target_type=target, target_id=target_id, rater_id=rater_id, score=score,
                        details={'comment': 'Calificación sintética para pruebas.'}, updated=True,
                        created_at=updated.replace(tzinfo=ZONE),
                        updated_at=(updated + timedelta(hours=rng.randint(1, 18))).replace(tzinfo=ZONE)))
            order['_accepted_at'] = accepted if started else None
    manifest = plain({'format': 1, 'year': args.year, 'namespace': str(namespace), 'zone': 'America/Bogota',
        'created': datetime.now(ZONE), 'scope': scope, 'baseline_hash': baseline(state),
        'databases': state['databases'], 'users': state['users'], 'services': state['services'],
        'orders': orders, 'ratings': ratings,
        'assumptions': ['Solo datos sintéticos de desarrollo.', 'Reloj histórico de creación: booking >= created_at + 30 min.',
                        'Si falta la dirección principal, el snapshot de la orden usa una dirección colombiana sintética; no modifica addresses.',
                        'No cambia usuarios, agendas, categorías, imágenes ni servicios.'],
        'address_fallback_users': sorted(fallback_users)})
    manifest['summary'] = report(manifest, state)
    for line in manifest['summary']:
        require(line['total_final'] >= 50 and line['percent_completed_rated'] >= 40, 'No se cumple el objetivo por servicio.')
    path = Path(args.file)
    require(not path.exists(), 'El manifiesto ya existe: no se sobrescribe. Usa verify o el mismo archivo para reanudar.')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print_summary(manifest)
    print(f'PLAN guardado en {path}. Ninguna fila escrita en la BD. Contiene direcciones: guárdalo privado.')


def print_summary(m):
    print('Bases:', m['databases'])
    print(f'Órdenes NUEVAS: {len(m["orders"])}; completadas nuevas: {len(m["ratings"]) // 3}; ratings nuevos: {len(m["ratings"])}')
    if m.get('address_fallback_users'):
        print('Direcciones sintéticas en el snapshot de la orden para:', ', '.join(m['address_fallback_users']))
    for s in m['summary']:
        print(f'{s["provider"]}: {s["title"]}: existentes={s["existing"]}, nuevas={s["new"]}, total={s["total_final"]}, completadas calificadas={s["percent_completed_rated"]}%')


def load_manifest(path):
    m = json.loads(Path(path).read_text(encoding='utf-8'))
    require(m['format'] == 1 and m['zone'] == 'America/Bogota', 'Formato/zona de manifiesto incompatible.')
    require(len(m['orders']) == len({o['id'] for o in m['orders']}), 'IDs de órdenes duplicados.')
    return m


def values_for(row, fields):
    return [Jsonb(row[f]) if f in JSON_FIELDS and row[f] is not None else row[f] for f in fields]


def insert_exact(conn, table, fields, row, pk):
    # No se usa ON CONFLICT DO UPDATE: nunca sobrescribir datos existentes.
    current = rows(conn, f'SELECT {",".join(fields)} FROM {table} WHERE {pk}=%s', (row[pk],))
    expected = {f: row[f] for f in fields}
    if current:
        require(plain(current[0]) == plain(expected), f'Conflicto con fila existente en {table}: {row[pk]}. No se sobrescribe.')
        return False
    conn.execute(f'INSERT INTO {table} ({",".join(fields)}) VALUES ({",".join(["%s"] * len(fields))})', values_for(row, fields))
    return True


def apply(args):
    require(args.allow_historical_write and args.test_database, 'apply requiere --allow-historical-write --test-database. Confirma localmente cronología histórica completa y destino de pruebas.')
    m = load_manifest(args.file)
    state = load_state(m['scope'], [o['id'] for o in m['orders']])
    require(baseline(state) == m['baseline_hash'], 'Cambió el estado base desde plan. No se escribe. Revisar o regenerar en una copia limpia.')
    print_summary(m)
    with db('CATALOG_DSN', False) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('fatum-seed-catalog'))")
        new = 0
        for o in m['orders']:
            new += insert_exact(c, 'orders', ORDER_FIELDS, o, 'id')
        for s in m['services']:
            c.execute('''INSERT INTO service_metrics(service_id,favorite_count)
                SELECT %s,count(*) FROM client_favorites WHERE service_id=%s
                ON CONFLICT(service_id) DO NOTHING''', (s['id'], s['id']))
    print(f'Catalog confirmado: {new} órdenes insertadas; las restantes ya coincidían.')
    # La segunda transacción no es atómica con la primera. Reanudar con el MISMO manifiesto.
    with db('RATE_DSN', False) as r:
        r.execute("SELECT pg_advisory_xact_lock(hashtext('fatum-seed-rate'))")
        now = datetime.now(ZONE)
        for u in m['users']:
            r.execute('''INSERT INTO user_snapshots(user_id,name,role,total_score,ratings_count,version,created_at,updated_at)
                VALUES(%s,%s,%s,0,0,0,%s,%s) ON CONFLICT(user_id) DO NOTHING''',
                (u['id'], u['complete_name'], u['role'], now, now))
        for o in m['orders']:
            if o['order_status'] == 'COMPLETED':
                snap = dict(order_id=o['id'], service_id=o['service'], client_id=o['client_id'],
                            provider_id=o['provider_id'], is_completed=True, version=0)
                insert_exact(r, 'order_snapshots', SNAP_FIELDS, snap, 'order_id')
        new = 0
        for rating in m['ratings']:
            if insert_exact(r, 'ratings', RATING_FIELDS, rating, 'id'):
                new += 1
                if rating['target_type'] != 'SERVICE':
                    r.execute('''UPDATE user_snapshots SET total_score=total_score+%s,
                        ratings_count=ratings_count+1,version=version+1,
                        updated_at=GREATEST(updated_at,%s::timestamptz) WHERE user_id=%s''',
                        (rating['score'], rating['updated_at'], rating['target_id']))
    print(f'Rate confirmado: {new} ratings insertados. Los acumulados solo aumentaron por filas nuevas.')
    verify(args)


def verify(args):
    m = load_manifest(args.file)
    ids = [o['id'] for o in m['orders']]
    with db('CATALOG_DSN') as c, db('RATE_DSN') as r:
        generated = rows(c, f'SELECT {",".join(ORDER_FIELDS)} FROM orders WHERE id=ANY(%s)', (ids,))
        require(len(generated) == len(ids), 'Faltan órdenes del manifiesto en Catalog. Reanudar apply.')
        expected = {o['id']: {f: o[f] for f in ORDER_FIELDS} for o in m['orders']}
        require(all(plain(o) == expected[o['id']] for o in generated), 'Alguna orden del seed fue alterada.')
        stored_ratings = rows(r, f'SELECT {",".join(RATING_FIELDS)} FROM ratings WHERE order_id=ANY(%s)', (ids,))
        wanted = {x['id']: x for x in m['ratings']}
        require(len(stored_ratings) == len(wanted) and all(plain(x) == wanted.get(x['id']) for x in stored_ratings), 'Faltan ratings o no coinciden. Reanudar apply con el mismo archivo.')
        actual_snaps = rows(r, 'SELECT * FROM order_snapshots WHERE order_id=ANY(%s)', (ids,))
        require(len(actual_snaps) == len(m['ratings']) // 3, 'Cantidad de snapshots de órdenes incoherente.')
        by_id = {o['id']: o for o in m['orders']}
        for snap in actual_snaps:
            o = by_id[snap['order_id']]
            require(snap['is_completed'] and o['order_status'] == 'COMPLETED'
                    and snap['service_id'] == o['service'] and snap['client_id'] == o['client_id']
                    and snap['provider_id'] == o['provider_id'], 'Snapshot de orden con identidad incoherente.')
        for s in m['services']:
            all_orders = rows(c, 'SELECT id,order_status FROM orders WHERE service=%s', (s['id'],))
            service_ratings = rows(r, '''SELECT x.order_id FROM ratings x JOIN order_snapshots o ON o.order_id=x.order_id
                WHERE x.target_type='SERVICE' AND x.target_id=%s AND x.updated AND o.is_completed
                AND x.rater_id=o.client_id AND o.service_id=%s''', (s['id'], s['id']))
            rated = {x['order_id'] for x in service_ratings}
            done = sum(o['order_status'] == 'COMPLETED' and o['id'] in rated for o in all_orders)
            total = len(all_orders)
            require(total >= 50 and Decimal(done) / total >= Decimal('.4'), f'Objetivo incumplido: {s["title"]}.')
            print(f'OK {s["title"]}: {total} órdenes, {done} completadas con rating final de servicio ({100 * done / total:.1f}%).')
    # Comprueba también roles y acumulados de las ocho personas contra TODAS sus ratings.
    load_state(m['scope'], ids)
    print('VERIFICACIÓN OK: filas del manifiesto y objetivos por servicio; acumulados de usuarios consistentes.')


def snapshots(args):
    require(args.allow_historical_write and args.test_database, 'snapshots requiere autorización local y destino de pruebas.')
    m = load_manifest(args.file)
    verify(args)
    ids = [o['id'] for o in m['orders']]
    low, high = date(m['year'], 1, 1), date(m['year'], 12, 31)
    counts = []
    with db('CATALOG_DSN', False) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('fatum-seed-catalog'))")
        preexisting = rows(c, '''SELECT id FROM orders WHERE service=ANY(%s) AND NOT(id=ANY(%s)) AND created_at<%s''',
                           (m['scope']['service_ids'], ids, datetime.combine(high + timedelta(days=1), time())))
        require(not preexisting, 'Hay órdenes previas al cierre histórico fuera del manifiesto; no se puede reconstruir su trayectoria exacta. No se sobrescriben snapshots.')
        day = low
        cuts = []
        while day <= high:
            if day.weekday() == 6:
                cuts.append(day)
            day += timedelta(days=1)
        if not cuts or cuts[-1] != high:
            cuts.append(high)
        for s in m['services']:
            relevant = [o for o in m['orders'] if o['service'] == s['id']]
            for cut in cuts:
                end = datetime.combine(cut, time(23, 59, 59))
                week_start = datetime.combine(cut - timedelta(days=cut.weekday()), time())
                hist = []
                for o in relevant:
                    if datetime.fromisoformat(o['created_at']) > end:
                        continue
                    if datetime.fromisoformat(o['updated_at']) <= end:
                        status = o['order_status']
                    elif o['_accepted_at'] and datetime.fromisoformat(o['booking']) <= end:
                        status = 'DELIVERING'
                    elif o['_accepted_at'] and datetime.fromisoformat(o['_accepted_at']) <= end:
                        status = 'ACCEPTED'
                    else:
                        status = 'PENDING'
                    hist.append(status)
                by = Counter(hist)
                year, week, _ = cut.isocalendar()
                favorite_count = rows(c, 'SELECT count(*) AS n FROM client_favorites WHERE service_id=%s AND created_at<=%s', (s['id'], end))[0]['n']
                row = dict(id=str(uuid.uuid5(uuid.UUID(m['namespace']), f'snapshot/{s["id"]}/{year}/{week}')),
                    service_id=s['id'], year=year, week=week, snapshot_date=cut.isoformat(),
                    views_in_period=0, dwell_seconds_in_period=0, dwell_events_in_period=0,
                    new_orders_in_period=sum(week_start <= datetime.fromisoformat(o['created_at']) <= end for o in relevant),
                    favorite_count=favorite_count, total_orders=len(hist), pending_orders=by['PENDING'],
                    accepted_orders=by['ACCEPTED'], rejected_orders=by['REJECTED'], delivering_orders=by['DELIVERING'],
                    completed_orders=by['COMPLETED'], cancelled_orders=by['CANCELLED'],
                    not_delivered_orders=0, dispute_related_orders=0, created_at=end.isoformat())
                found = rows(c, 'SELECT id FROM service_metrics_snapshot WHERE service_id=%s AND year=%s AND week=%s', (s['id'], year, week))
                require(not found or found[0]['id'] == row['id'], 'Ya existe un snapshot histórico de otra fuente. No se sobrescribe.')
                counts.append(insert_exact(c, 'service_metrics_snapshot', METRIC_FIELDS, row, 'id'))
    print(f'Snapshots de órdenes: {sum(counts)} nuevos. Vistas/permanencia no simuladas (cero). Favoritos calculados por fecha.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'apply', 'verify', 'snapshots'])
    parser.add_argument('--scope', default='alcance.json')
    parser.add_argument('--file', default='plan_2025.json')
    parser.add_argument('--year', type=int, default=2025)
    parser.add_argument('--seed', type=int, default=20251115)
    parser.add_argument('--allow-historical-write', action='store_true')
    parser.add_argument('--test-database', action='store_true')
    args = parser.parse_args()
    require(2000 <= args.year < date.today().year, 'El año debe ser histórico.')
    {'plan': plan, 'apply': apply, 'verify': verify, 'snapshots': snapshots}[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
