#!/usr/bin/env python3
"""Carga sintética Fatum 2026-01-01 a 2026-10-31. Nunca ejecutar sobre producción.
Mantiene seed_fatum.py original como módulo. plan sólo SELECT; apply exige confirmación.
"""
import argparse
import json
import math
import os
import random
import sys
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import seed_fatum as b

EVENT_FIELDS = 'id service_id user_id dwell_time_seconds created_at'.split()
FAVORITE_FIELDS = 'id client_id service_id created_at'.split()
LIVE_FIELDS = ('service_id total_views favorite_count total_dwell_seconds dwell_event_count '
               'period_views period_dwell_seconds period_dwell_events').split()
START = date(2026, 1, 1)
END = date(2026, 10, 31)


def dt(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def private_json(path, value):
    path = Path(path)
    b.require(not path.exists(), f'No se sobrescribe {path}. Conserva los manifiestos de cargas aplicadas.')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(b.plain(value), f, indent=2, ensure_ascii=False)
        f.write('\n')


def announce(text):
    print(text, flush=True)


def validate_scope(scope):
    b.require(len(scope['service_ids']) == 19 and len(set(scope['service_ids'])) == 19,
              'Esta actualización requiere los 19 servicios del manifiesto original.')
    b.require(len(scope['client_ids']) == 5 and len(set(scope['client_ids'])) == 5,
              'Esta actualización requiere cinco clientes distintos.')


@contextmanager
def writer_lock():
    # Lock de sesión propio: abarca AMBOS commits, a diferencia del lock por transacción.
    # No sustituye pausar escrituras de la aplicación u otros scripts.
    with b.db('CATALOG_DSN', False) as c:
        c.execute("SELECT pg_advisory_lock(hashtext('fatum-seed-2026-writer'))")
        try:
            yield
        finally:
            c.execute("SELECT pg_advisory_unlock(hashtext('fatum-seed-2026-writer'))")


def catalog_state(scope, exclude=None):
    exclude = exclude or {}
    ids = scope['service_ids']
    with b.db('CATALOG_DSN') as c:
        return {
            'portfolio': b.rows(c, 'SELECT * FROM service_portafolio WHERE service_id=ANY(%s) ORDER BY service_id,id', (ids,)),
            'events': b.rows(c, 'SELECT * FROM service_view_event WHERE service_id=ANY(%s) AND NOT(id=ANY(%s)) ORDER BY id', (ids, exclude.get('events', []))),
            'favorites': b.rows(c, 'SELECT * FROM client_favorites WHERE service_id=ANY(%s) AND NOT(id=ANY(%s)) ORDER BY id', (ids, exclude.get('favorites', []))),
            'snapshots': b.rows(c, 'SELECT * FROM service_metrics_snapshot WHERE service_id=ANY(%s) AND NOT(id=ANY(%s)) ORDER BY id', (ids, exclude.get('snapshots', []))),
            'live': b.rows(c, 'SELECT * FROM service_metrics WHERE service_id=ANY(%s) ORDER BY service_id', (ids,)),
        }


def extra_hash(extra):
    return b.digest({k: extra[k] for k in ('portfolio', 'events', 'favorites', 'snapshots')})


def scope_ids(m):
    return {k: [x['id'] for x in m[k]] for k in ('events', 'favorites', 'snapshots')}


def profiles_for(users, services, clients):
    categories = sorted({s['category'] for s in services})
    desired = ['Spa & Skin Care', 'Nail Studio', 'Maquillaje Profesional', 'Peluqueria & Barberia', 'Estetica Corporal']
    result = {}
    for i, uid in enumerate(clients):
        primary = desired[i % len(desired)]
        if primary not in categories:
            primary = categories[i % len(categories)]
        secondary = next(cat for cat in categories if cat != primary)
        result[uid] = dict(user_id=uid, username=users[uid]['username'], synthetic=True,
            primary_category=primary, secondary_category=secondary,
            weights={cat: (9.0 if cat == primary else 2.0 if cat == secondary else 0.18) for cat in categories},
            budget_preference=['economico', 'intermedio', 'premium', 'intermedio', 'economico'][i % 5],
            favorite_target=2 + i % 4,
            purpose='Referencia de prueba, no un perfil real ni inferido de demografía.')
    return result


def preference(profile, service):
    weight = profile['weights'][service['category']]
    price = Decimal(str(service['base_price']))
    if profile['budget_preference'] == 'economico' and price > 60000:
        weight *= 0.45
    if profile['budget_preference'] == 'premium' and price >= 60000:
        weight *= 1.4
    return weight


def find_slot(provider, client, preferred, duration, requested, state, occupied_p, occupied_c):
    schedules = {(a['provider_id'], a['day']): a for a in state['availability']}
    unavailable = [(a['start_date'], a['final_date']) for a in state['unavailable'] if a['provider_id'] == provider]
    low, high = START + timedelta(days=7), END
    span = (high - low).days + 1
    for offset in range(span):
        day = low + timedelta(days=((preferred - low).days + offset) % span)
        schedule = schedules.get((provider, b.DAYS[day.weekday()]))
        if not schedule:
            continue
        current = datetime.combine(day, max(schedule['initial_time'], time(8)))
        closing = datetime.combine(day, min(schedule['final_time'], time(20)))
        while current + timedelta(minutes=max(duration, requested)) <= closing:
            end = current + timedelta(minutes=max(duration, requested))
            blocked = any(current <= z and a < end for a, z in unavailable)
            if not blocked and not b.overlap(current, end, occupied_p[provider]) and not b.overlap(current, end, occupied_c[client]):
                occupied_p[provider].append((current, end))
                occupied_c[client].append((current, end))
                return current
            current += timedelta(minutes=15)
    raise ValueError(f'No hay capacidad de agenda para {provider} entre enero y octubre. No se alteró disponibilidad.')


def make_orders(state, profiles, rng, namespace):
    users = {u['id']: u for u in state['users']}
    cats = {c['name']: c for c in state['categories']}
    clients = list(profiles)
    providers = {s['provider_id'] for s in state['services']}
    for uid in providers:
        b.require(users[uid]['role'] == 'PROFESSIONAL', 'Proveedor no profesional.')
    for uid in clients:
        b.require(users[uid]['role'] == 'CLIENT', 'Cliente cambió de rol.')
        b.require(users[uid]['birth_date'] <= date(2008, 1, 1), 'Cliente menor de edad al iniciar 2026.')
    occupied_p, occupied_c = {p: [] for p in providers}, {u: [] for u in clients}
    for old in state['existing_orders']:
        if old['order_status'] not in ('REJECTED', 'CANCELLED'):
            interval = (old['booking'], old['booking'] + timedelta(minutes=old['estimated_time'] if old['estimated_time'] is not None else 30))
            occupied_p[old['provider_id']].append(interval)
            if old['client_id'] in occupied_c:
                occupied_c[old['client_id']].append(interval)
    addresses = {uid: b.snapshot_address(users[uid])[0] for uid in clients}
    orders, ratings = [], []
    qualified = b.qualified_rate_ids(state)
    for number, s in enumerate(state['services']):
        cat = cats[s['category']]
        b.require(cat['is_able'] and cat['scheduling_model'] == 'SINGLE_SESSION', 'Categoría inválida.')
        b.require(not any(c['parent_name'] == cat['name'] for c in cats.values()), 'Categoría no hoja.')
        seen = set()
        while cat['parent_name']:
            b.require(cat['name'] not in seen, 'Ciclo de categorías.')
            seen.add(cat['name'])
            cat = cats[cat['parent_name']]
            b.require(cat['is_able'], 'Categoría padre no habilitada.')
        old = [o for o in state['existing_orders'] if o['service'] == s['id']]
        old_rated = sum(o['order_status'] == 'COMPLETED' and o['id'] in qualified for o in old)
        completed = 30 + (number % 5) * 2
        extra = max(0, math.ceil((Decimal('.4') * (len(old) + 50) - old_rated - completed) / Decimal('.6')))
        statuses = ['COMPLETED'] * (completed + extra) + ['REJECTED'] * (45 - completed) + ['CANCELLED'] * 5
        rng.shuffle(statuses)
        for i, status in enumerate(statuses):
            uid = rng.choices(clients, weights=[preference(profiles[u], s) for u in clients])[0]
            user = users[uid]
            duration = s['default_time'] or rng.choice([30, 45, 60, 75])
            b.require(0 < duration <= 1440, 'Duración inválida.')
            requested = s['default_time'] if s['default_time'] is not None else 30
            if 'halloween' in s['title'].lower():
                preferred = date(2026, 10, 1) + timedelta(days=rng.randrange(31))
            else:
                preferred = START + timedelta(days=7 + int(i * ((END - START).days - 7) / len(statuses)))
            booking = find_slot(s['provider_id'], uid, preferred, duration, requested, state, occupied_p, occupied_c)
            created = booking - timedelta(days=rng.randint(1, 6))
            updated = booking + timedelta(minutes=duration + 10) if status == 'COMPLETED' else created + timedelta(hours=rng.randint(3, 8))
            price = s['base_price']
            if s['is_negotiable']:
                factors = [Decimal('.9'), Decimal('.95'), Decimal('1')] if profiles[uid]['budget_preference'] == 'economico' else [Decimal('.95'), Decimal('1'), Decimal('1.05'), Decimal('1.1')]
                price = (price * rng.choice(factors)).quantize(Decimal('.01'))
            addr = addresses[uid]
            oid = str(uuid.uuid5(namespace, f'order2026/{s["id"]}/{i}'))
            o = dict(id=oid, client_id=uid, client_name=user['complete_name'], client_username=user['username'],
                provider_id=s['provider_id'], booking=booking, service=s['id'], negotiated_price=price,
                estimated_time=duration, delivery_city=addr['city'], delivery_state=addr['state'],
                country='Colombia', residence=addr['residence'], answers=b.answers_for(s, rng),
                requirements='Solicitud sintética de prueba para perfilado y reportes 2026.',
                order_status=status, reason={'REJECTED':'Condiciones no compatibles; solicitud sintética.',
                                            'CANCELLED':'Cliente canceló antes de aceptación; solicitud sintética.'}.get(status),
                created_at=created, updated_at=updated, delivery_started_at=booking if status == 'COMPLETED' else None,
                _accepted_at=created + timedelta(hours=2) if status == 'COMPLETED' else None)
            for field in b.ORDER_FIELDS:
                if '_confirmed_by_' in field:
                    o[field] = True if status == 'COMPLETED' else None
            orders.append(o)
            if status == 'COMPLETED':
                latest = datetime.combine(END, time(23, 59, 59))
                max_hours = min(18, int((latest - updated).total_seconds() // 3600))
                b.require(max_hours >= 1, 'Sin margen para calificar antes del corte.')
                for target, tid, rater in [('SERVICE', s['id'], uid), ('PROFESSIONAL', s['provider_id'], uid), ('CLIENT', uid, s['provider_id'])]:
                    high_interest = profiles[uid]['primary_category'] == s['category']
                    score = rng.choices([1, 2, 3, 4, 5], [1, 3, 10, 32, 54] if high_interest else [3, 7, 22, 40, 28])[0]
                    ratings.append(dict(id=str(uuid.uuid5(namespace, f'{oid}/{target}')), order_id=oid,
                        target_type=target, target_id=tid, rater_id=rater, score=score,
                        details={'comment':'Evaluación sintética para prueba del agente.'}, updated=True,
                        created_at=updated.replace(tzinfo=b.ZONE), updated_at=(updated + timedelta(hours=rng.randint(1, max_hours))).replace(tzinfo=b.ZONE)))
    return b.plain(orders), b.plain(ratings)


def make_behavior(state, extra, profiles, orders, rng, namespace):
    services = state['services']
    by_service = {s['id']: s for s in services}
    events = []
    def add(uid, sid, timestamp, seconds, label):
        events.append(dict(id=str(uuid.uuid5(namespace, label)), user_id=uid, service_id=sid,
                           dwell_time_seconds=int(seconds), created_at=timestamp))
    for o in orders:
        creation = dt(o['created_at'])
        # Registrar dos visitas conocidas antes de cada solicitud, no confundir reserva con visita.
        add(o['client_id'], o['service'], creation - timedelta(hours=6), rng.randint(35, 110), f'previsit-a/{o["id"]}')
        add(o['client_id'], o['service'], creation - timedelta(minutes=5), rng.randint(60, 240), f'previsit-b/{o["id"]}')
    for uid, profile in profiles.items():
        for month in range(1, 11):
            for session in range(12 + list(profiles).index(uid) * 2):
                last_day = min(END, date(2026 if month < 12 else 2027, month + 1 if month < 12 else 1, 1) - timedelta(days=1))
                timestamp = datetime(2026, month, rng.randint(1, last_day.day), rng.randint(8, 21), rng.randint(0, 40))
                choices = []
                available = list(services)
                for step in range(rng.randint(3, 7)):
                    s = rng.choices(available, weights=[preference(profile, x) for x in available])[0]
                    available.remove(s)
                    choices.append(s)
                for step, s in enumerate(choices):
                    primary = s['category'] == profile['primary_category']
                    secondary = s['category'] == profile['secondary_category']
                    seconds = rng.randint(70, 260) if primary else rng.randint(25, 140) if secondary else rng.randint(5, 45)
                    timestamp += timedelta(seconds=seconds + rng.randint(15, 50))
                    add(uid, s['id'], timestamp, seconds, f'browse/{uid}/{month}/{session}/{step}')
    existing_pairs = {(f['client_id'], f['service_id']) for f in extra['favorites']}
    favorites = []
    for uid, profile in profiles.items():
        ranked = sorted(services, key=lambda s: (-preference(profile, s), s['id']))
        owned = {sid for user, sid in existing_pairs if user == uid}
        desired = max(0, profile['favorite_target'] - len(owned))
        for s in [s for s in ranked if s['id'] not in owned][:desired]:
            visits = [e for e in events if e['user_id'] == uid and e['service_id'] == s['id']]
            b.require(visits, 'Favorito sin visita previa.')
            first = min(e['created_at'] for e in visits)
            favorites.append(dict(id=str(uuid.uuid5(namespace, f'favorite/{uid}/{s["id"]}')),
                                  client_id=uid, service_id=s['id'], created_at=first + timedelta(minutes=1)))
    return b.plain(events), b.plain(favorites)


def cuts():
    current = START
    result = []
    while current <= END:
        if current.weekday() == 6:
            result.append(current)
        current += timedelta(days=1)
    if not result or result[-1] != END:
        result.append(END)
    return result


def make_snapshots(state, extra, m, namespace):
    result = []
    existing = {(s['service_id'], s['year'], s['week']) for s in extra['snapshots']}
    for s in state['services']:
        seeded = [o for o in m['orders'] if o['service'] == s['id']]
        prior = [o for o in state['existing_orders'] if o['service'] == s['id']]
        new_events = [e for e in m['events'] if e['service_id'] == s['id']]
        all_events = new_events + b.plain([e for e in extra['events'] if e['service_id'] == s['id']])
        all_favorites = m['favorites'] + b.plain(extra['favorites'])
        for cut in cuts():
            end = datetime.combine(cut, time(23, 59, 59))
            start_week = datetime.combine(max(START, cut - timedelta(days=cut.weekday())), time())
            iso_year, week, _ = cut.isocalendar()
            b.require((s['id'], iso_year, week) not in existing, f'Existe snapshot ajeno de {s["title"]} semana {week}; no se sobrescribe.')
            statuses = []
            for o in seeded:
                if dt(o['created_at']) > end:
                    continue
                if dt(o['updated_at']) <= end:
                    status = o['order_status']
                elif o['_accepted_at'] and dt(o['booking']) <= end:
                    status = 'DELIVERING'
                elif o['_accepted_at'] and dt(o['_accepted_at']) <= end:
                    status = 'ACCEPTED'
                else:
                    status = 'PENDING'
                statuses.append(status)
            for o in prior:
                if o['created_at'] > end:
                    continue
                # No inventar el estado histórico intermedio de órdenes ajenas al seed.
                stable_pending = o['order_status'] == 'PENDING' and o['created_at'] == o['updated_at']
                b.require(o['updated_at'] <= end or stable_pending,
                          f'No hay historial exacto de la orden previa {o["id"]} para corte {cut}. No modificarla para esquivar el error.')
                statuses.append(o['order_status'])
            counts = Counter(statuses)
            period_events = [e for e in all_events if start_week <= dt(e['created_at']) <= end]
            row = dict(id=str(uuid.uuid5(namespace, f'snapshot2026/{s["id"]}/{iso_year}/{week}')),
                service_id=s['id'], year=iso_year, week=week, snapshot_date=cut,
                views_in_period=sum(start_week <= dt(e['created_at']) <= end for e in new_events),
                dwell_seconds_in_period=sum(e['dwell_time_seconds'] for e in period_events),
                dwell_events_in_period=len(period_events),
                new_orders_in_period=sum(start_week <= dt(o['created_at']) <= end for o in seeded) + sum(start_week <= o['created_at'] <= end for o in prior),
                favorite_count=sum(f['service_id'] == s['id'] and dt(f['created_at']) <= end for f in all_favorites),
                total_orders=len(statuses), pending_orders=counts['PENDING'], accepted_orders=counts['ACCEPTED'],
                rejected_orders=counts['REJECTED'], delivering_orders=counts['DELIVERING'],
                completed_orders=counts['COMPLETED'], cancelled_orders=counts['CANCELLED'],
                not_delivered_orders=counts['NOT_DELIVERED'],
                dispute_related_orders=sum(counts[k] for k in ('DISPUTED', 'SOLVED', 'REFUNDED')), created_at=end)
            result.append(row)
    return b.plain(result)


def oracle(m):
    output = []
    by_service = {s['id']: s for s in m['services']}
    for uid, profile in m['profiles'].items():
        events = [e for e in m['events'] if e['user_id'] == uid]
        orders = [o for o in m['orders'] if o['client_id'] == uid]
        favorites = [f for f in m['favorites'] if f['client_id'] == uid]
        category_orders = Counter(by_service[o['service']]['category'] for o in orders)
        dwell = Counter()
        for e in events:
            dwell[by_service[e['service_id']]['category']] += e['dwell_time_seconds']
        output.append(dict(profile, realized_orders=len(orders), orders_by_category=dict(category_orders),
                           dwell_seconds_by_category=dict(dwell), new_favorite_service_ids=[f['service_id'] for f in favorites]))
    return {'synthetic': True, 'purpose':'Oráculo de evaluación local. No dar estas etiquetas al agente durante la evaluación.', 'profiles': output}


def plan(args):
    b.require(END < datetime.now(b.ZONE).date(), 'El intervalo solicitado todavía no terminó.')
    b.require(not Path(args.file).exists() and not Path(args.profiles_file).exists(), 'Ya existe plan o archivo de perfiles. No sobrescribir.')
    old = b.load_manifest(args.old_plan)
    b.require(old['year'] == 2025, 'El manifiesto de origen debe corresponder a la carga 2025.')
    validate_scope(old['scope'])
    old_ids = [o['id'] for o in old['orders']]
    with b.db('CATALOG_DSN') as c, b.db('RATE_DSN') as r:
        for conn, table, field in [(c, 'orders', 'id'), (r, 'ratings', 'order_id'), (r, 'order_snapshots', 'order_id')]:
            b.require(not b.rows(conn, f'SELECT {field} FROM {table} WHERE {field}=ANY(%s) LIMIT 1', (old_ids,)),
                      'Aún quedan datos de la carga 2025. Completa retirar_seed_2025.py antes de planificar 2026.')
        old_ns = uuid.UUID(old['namespace'])
        old_snapshot_ids = []
        day = date(2025, 1, 1)
        old_cuts = []
        while day <= date(2025, 12, 31):
            if day.weekday() == 6:
                old_cuts.append(day)
            day += timedelta(days=1)
        old_cuts.append(date(2025, 12, 31))
        for sid in old['scope']['service_ids']:
            for cut in old_cuts:
                y, w, _ = cut.isocalendar()
                old_snapshot_ids.append(str(uuid.uuid5(old_ns, f'snapshot/{sid}/{y}/{w}')))
        b.require(not b.rows(c, 'SELECT id FROM service_metrics_snapshot WHERE id=ANY(%s) LIMIT 1', (old_snapshot_ids,)), 'Quedan snapshots de 2025; completa la retirada.')
    state = b.load_state(old['scope'])
    extra = catalog_state(old['scope'])
    users = {u['id']: u for u in state['users']}
    profiles = profiles_for(users, state['services'], old['scope']['client_ids'])
    rng, namespace = random.Random(args.seed), uuid.uuid4()
    orders, ratings = make_orders(state, profiles, rng, namespace)
    events, favorites = make_behavior(state, extra, profiles, orders, rng, namespace)
    baseline_live = []
    stored_live = {x['service_id']: x for x in extra['live']}
    for s in state['services']:
        current = stored_live.get(s['id']) or {f: (s['id'] if f == 'service_id' else 0) for f in LIVE_FIELDS}
        favorite_count = sum(f['service_id'] == s['id'] for f in extra['favorites'])
        if s['id'] not in stored_live:
            current['favorite_count'] = favorite_count
        b.require(current['favorite_count'] == favorite_count, f'Contador de favoritos previo inconsistente: {s["title"]}.')
        baseline_live.append(current)
    m = b.plain(dict(format=2, year=2026, start_date=START, end_date=END, zone='America/Bogota',
        namespace=str(namespace), created=datetime.now(b.ZONE), scope=old['scope'],
        baseline_hash=b.baseline(state), behavior_baseline_hash=extra_hash(extra),
        databases=state['databases'], users=state['users'], services=state['services'],
        orders=orders, ratings=ratings, events=events, favorites=favorites, profiles=profiles,
        baseline_live=baseline_live,
        assumptions=['Datos sintéticos para pruebas, no comportamiento real.',
                     'Una visita sintética equivale a un GET de detalle y un evento de permanencia.',
                     'Los GET reales antiguos no tienen bitácora individual y no se reconstruyen en snapshots.',
                     'Los contadores de periodo vivo existentes se conservan, no se agregan eventos históricos a la semana actual.']))
    m['snapshots'] = make_snapshots(state, extra, m, namespace)
    m['summary'] = b.report(m, state)
    b.require(all(x['total_final'] >= 50 and x['percent_completed_rated'] >= 40 for x in m['summary']), 'Objetivos incumplidos.')
    validate_dates(m)
    m['checksum'] = b.digest(m)
    private_json(args.file, m)
    private_json(args.profiles_file, oracle(m))
    summary(m)
    announce(f'PLAN creado: {args.file}. Sólo lectura en las BDs. Perfiles de referencia: {args.profiles_file}.')


def load(path):
    m = json.loads(Path(path).read_text(encoding='utf-8'))
    b.require(m.get('checksum') == b.digest({k: v for k, v in m.items() if k != 'checksum'}),
              'El manifiesto fue editado o está incompleto. No se acepta para escribir.')
    b.require(m['format'] == 2 and m['year'] == 2026 and m['start_date'] == START.isoformat()
              and m['end_date'] == END.isoformat() and m['zone'] == 'America/Bogota', 'Manifiesto 2026 incompatible.')
    validate_scope(m['scope'])
    validate_dates(m)
    return m


def validate_dates(m):
    low = datetime.combine(START, time())
    high = datetime.combine(END + timedelta(days=1), time())
    for collection, fields in [('orders', ['created_at', 'updated_at', 'booking', 'delivery_started_at']),
                              ('ratings', ['created_at', 'updated_at']), ('events', ['created_at']), ('favorites', ['created_at'])]:
        ids = [x['id'] for x in m[collection]]
        b.require(len(ids) == len(set(ids)), f'IDs duplicados: {collection}.')
        for row in m[collection]:
            for f in fields:
                if row[f] is not None:
                    value = dt(row[f])
                    value = value.astimezone(b.ZONE).replace(tzinfo=None) if value.tzinfo else value
                    b.require(low <= value < high, f'Fecha fuera del intervalo: {collection} {row["id"]} {f}.')
    for o in m['orders']:
        b.require(o['country'] == 'Colombia' and all(o[f] for f in ['delivery_city','delivery_state','residence']), 'Dirección inválida.')
        b.require(dt(o['booking']) >= dt(o['created_at']) + timedelta(minutes=30), 'Anticipación incoherente.')
        b.require(dt(o['updated_at']) >= dt(o['created_at']), 'Actualización anterior a creación.')
    validate_relations(m)


def validate_relations(m):
    services = {s['id']: s for s in m['services']}
    orders = {o['id']: o for o in m['orders']}
    b.require(set(services) == set(m['scope']['service_ids']), 'Servicios del manifiesto no coinciden con el alcance.')
    grouped = defaultdict(list)
    for r in m['ratings']:
        b.require(r['order_id'] in orders and r['updated'] is True and 1 <= r['score'] <= 5, 'Rating sin orden o no final.')
        o = orders[r['order_id']]
        expected = {'SERVICE': (o['service'], o['client_id']),
                    'PROFESSIONAL': (o['provider_id'], o['client_id']), 'CLIENT': (o['client_id'], o['provider_id'])}
        b.require(r['target_type'] in expected and (r['target_id'], r['rater_id']) == expected[r['target_type']], 'Identidad de rating incoherente.')
        b.require(dt(r['created_at']).astimezone(b.ZONE).replace(tzinfo=None) >= dt(o['updated_at'])
                  and dt(r['updated_at']) >= dt(r['created_at']), 'Rating anterior a la finalización.')
        grouped[r['order_id']].append(r)
    for o in orders.values():
        b.require(o['service'] in services and o['provider_id'] == services[o['service']]['provider_id']
                  and o['client_id'] in m['scope']['client_ids'], 'Identidad de orden fuera del alcance.')
        b.require(o['order_status'] in ('COMPLETED', 'CANCELLED', 'REJECTED'), 'Estado nuevo no soportado.')
        flags = [o[f] for f in b.ORDER_FIELDS if '_confirmed_by_' in f]
        if o['order_status'] == 'COMPLETED':
            b.require(len(grouped[o['id']]) == 3 and {r['target_type'] for r in grouped[o['id']]} == {'SERVICE','PROFESSIONAL','CLIENT'}, 'Una completada debe tener tres ratings finales distintos.')
            b.require(all(f is True for f in flags) and o['delivery_started_at'] is not None and o['_accepted_at'] is not None, 'Confirmaciones de completada incompletas.')
            b.require(dt(o['updated_at']) >= dt(o['delivery_started_at']) + timedelta(minutes=o['estimated_time']), 'Finalización anterior al fin de prestación.')
        else:
            b.require(not grouped[o['id']] and all(f is None for f in flags) and o['delivery_started_at'] is None, 'Orden no completada con ratings/confirmaciones.')
    pairs = [(f['client_id'], f['service_id']) for f in m['favorites']]
    b.require(len(pairs) == len(set(pairs)), 'Favoritos cliente/servicio duplicados.')
    for e in m['events']:
        b.require(e['user_id'] in m['scope']['client_ids'] and e['service_id'] in services
                  and 5 <= e['dwell_time_seconds'] <= 300, 'Evento sintético inválido.')
    for f in m['favorites']:
        b.require(f['client_id'] in m['scope']['client_ids'] and f['service_id'] in services, 'Favorito fuera del alcance.')
        b.require(any(e['user_id'] == f['client_id'] and e['service_id'] == f['service_id']
                      and dt(e['created_at']) <= dt(f['created_at']) for e in m['events']), 'Favorito sin visita previa.')
    for o in orders.values():
        b.require(any(e['user_id'] == o['client_id'] and e['service_id'] == o['service']
                      and dt(e['created_at']) <= dt(o['created_at']) for e in m['events']), 'Solicitud sin visita previa.')
    actual_keys = set()
    snapshot_ids = set()
    for snap in m['snapshots']:
        cut = date.fromisoformat(snap['snapshot_date'])
        y, w, _ = cut.isocalendar()
        key = (snap['service_id'], y, w)
        b.require(START <= cut <= END and snap['year'] == y and snap['week'] == w and snap['service_id'] in services, 'Fecha/semana ISO del snapshot inválida.')
        b.require(key not in actual_keys and snap['id'] not in snapshot_ids, 'Snapshot duplicado.')
        actual_keys.add(key)
        snapshot_ids.add(snap['id'])
        expected_id = str(uuid.uuid5(uuid.UUID(m['namespace']), f'snapshot2026/{snap["service_id"]}/{y}/{w}'))
        b.require(snap['id'] == expected_id, 'UUID de snapshot no pertenece al namespace del plan.')
        for field in b.METRIC_FIELDS:
            if field not in ('id','service_id','year','week','snapshot_date','created_at'):
                b.require(isinstance(snap[field], int) and snap[field] >= 0, f'Contador negativo o inválido: {field}.')
    required_keys = {(sid, cut.isocalendar().year, cut.isocalendar().week) for sid in services for cut in cuts()}
    b.require(actual_keys == required_keys, 'Faltan cortes semanales del intervalo solicitado.')


def summary(m):
    b.print_summary(m)
    announce(f'Visitas con permanencia: {len(m["events"])}; favoritos NUEVOS: {len(m["favorites"])}; snapshots: {len(m["snapshots"])}.')
    announce('Todas las fechas nuevas están entre 2026-01-01 y 2026-10-31. Disponibilidad sólo se lee.')


def read_exact(conn, table, fields, expected, key='id', complete=False):
    if not expected:
        return []
    found = b.rows(conn, f'SELECT {",".join(fields)} FROM {table} WHERE {key}=ANY(%s)', ([x[key] for x in expected],))
    wanted = {x[key]: {f: x[f] for f in fields} for x in expected}
    for x in found:
        b.require(b.plain(x) == wanted[x[key]], f'Fila modificada o conflicto en {table}: {x[key]}. No se sobrescribe.')
    if complete:
        b.require(len(found) == len(expected), f'Faltan filas de {table}. Reanudar con el mismo manifiesto.')
    return found


def bulk_exact(conn, table, fields, expected, key='id'):
    found = read_exact(conn, table, fields, expected, key)
    present = {x[key] for x in found}
    new = [x for x in expected if x[key] not in present]
    sql = f'INSERT INTO {table} ({",".join(fields)}) VALUES ({",".join(["%s"] * len(fields))})'
    for start in range(0, len(new), 500):
        batch = new[start:start + 500]
        with conn.cursor() as cur:
            cur.executemany(sql, [b.values_for(x, fields) for x in batch])
        announce(f'{table}: {min(start + 500, len(new))}/{len(new)} nuevas filas preparadas (aún sin commit).')
    return new


def live_expected(m, event_ids, favorite_ids):
    result = {x['service_id']: dict(x) for x in m['baseline_live']}
    for event in m['events']:
        if event['id'] in event_ids:
            row = result[event['service_id']]
            row['total_views'] += 1
            row['total_dwell_seconds'] += event['dwell_time_seconds']
            row['dwell_event_count'] += 1
    for favorite in m['favorites']:
        if favorite['id'] in favorite_ids:
            result[favorite['service_id']]['favorite_count'] += 1
    return result


def apply(args):
    b.require(args.allow_historical_write and args.test_database, 'apply requiere --allow-historical-write --test-database.')
    with writer_lock():
        apply_locked(args)


def apply_locked(args):
    b.require(args.allow_historical_write and args.test_database, 'apply requiere --allow-historical-write --test-database.')
    m = load(args.file)
    state = b.load_state(m['scope'], [o['id'] for o in m['orders']])
    extra = catalog_state(m['scope'], scope_ids(m))
    b.require(b.baseline(state) == m['baseline_hash'] and extra_hash(extra) == m['behavior_baseline_hash'],
              'Cambió el estado previo desde plan. No se escribe; no generar otro plan tras una carga parcial.')
    summary(m)
    with b.db('CATALOG_DSN', False) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('fatum-seed-catalog'))")
        prior_events = read_exact(c, 'service_view_event', EVENT_FIELDS, m['events'])
        prior_favorites = read_exact(c, 'client_favorites', FAVORITE_FIELDS, m['favorites'])
        expected_before = live_expected(m, {x['id'] for x in prior_events}, {x['id'] for x in prior_favorites})
        actual = {x['service_id']: x for x in b.rows(c, 'SELECT * FROM service_metrics WHERE service_id=ANY(%s) FOR UPDATE', (m['scope']['service_ids'],))}
        for sid, target in expected_before.items():
            if sid in actual:
                b.require(b.plain(actual[sid]) == target, f'Métricas vivas cambiaron para {sid}. No se sobrescriben.')
            else:
                b.require(not prior_events and not prior_favorites, 'Falta métrica tras una carga previa.')
                c.execute('INSERT INTO service_metrics(service_id,favorite_count) VALUES(%s,%s)', (sid, target['favorite_count']))
        new_orders = bulk_exact(c, 'orders', b.ORDER_FIELDS, m['orders'])
        new_events = bulk_exact(c, 'service_view_event', EVENT_FIELDS, m['events'])
        new_favorites = bulk_exact(c, 'client_favorites', FAVORITE_FIELDS, m['favorites'])
        new_snapshots = bulk_exact(c, 'service_metrics_snapshot', b.METRIC_FIELDS, m['snapshots'])
        expected_after = live_expected(m, {e['id'] for e in m['events']}, {f['id'] for f in m['favorites']})
        for row in expected_after.values():
            c.execute('''UPDATE service_metrics SET total_views=%s,favorite_count=%s,
                total_dwell_seconds=%s,dwell_event_count=%s WHERE service_id=%s''',
                (row['total_views'], row['favorite_count'], row['total_dwell_seconds'], row['dwell_event_count'], row['service_id']))
    announce(f'CATALOG COMMIT: {len(new_orders)} órdenes, {len(new_events)} eventos, {len(new_favorites)} favoritos, {len(new_snapshots)} snapshots nuevos.')
    with b.db('RATE_DSN', False) as r:
        r.execute("SELECT pg_advisory_xact_lock(hashtext('fatum-seed-rate'))")
        now = datetime.now(b.ZONE)
        for user in m['users']:
            r.execute('''INSERT INTO user_snapshots(user_id,name,role,total_score,ratings_count,version,created_at,updated_at)
                VALUES(%s,%s,%s,0,0,0,%s,%s) ON CONFLICT(user_id) DO NOTHING''', (user['id'], user['complete_name'], user['role'], now, now))
        snapshots = [dict(order_id=o['id'], service_id=o['service'], client_id=o['client_id'], provider_id=o['provider_id'], is_completed=True, version=0) for o in m['orders'] if o['order_status'] == 'COMPLETED']
        bulk_exact(r, 'order_snapshots', b.SNAP_FIELDS, snapshots, 'order_id')
        new_ratings = bulk_exact(r, 'ratings', b.RATING_FIELDS, m['ratings'])
        changes = defaultdict(lambda: [0, 0])
        for x in new_ratings:
            if x['target_type'] != 'SERVICE':
                changes[x['target_id']][0] += x['score']
                changes[x['target_id']][1] += 1
        for uid, (score, count) in changes.items():
            r.execute('''UPDATE user_snapshots SET total_score=total_score+%s,ratings_count=ratings_count+%s,
                version=version+%s,updated_at=GREATEST(updated_at,%s) WHERE user_id=%s''', (score, count, count, now, uid))
    announce(f'RATE COMMIT: {len(new_ratings)} ratings nuevos. Verificando…')
    verify(args)


def verify(args):
    m = load(args.file)
    state = b.load_state(m['scope'], [o['id'] for o in m['orders']])
    extra = catalog_state(m['scope'], scope_ids(m))
    b.require(b.baseline(state) == m['baseline_hash'] and extra_hash(extra) == m['behavior_baseline_hash'], 'Cambió algún dato previo desde plan; revisar diferencias.')
    with b.db('CATALOG_DSN') as c, b.db('RATE_DSN') as r:
        for table, fields, key, expected in [('orders', b.ORDER_FIELDS, 'id', m['orders']),
                ('service_view_event', EVENT_FIELDS, 'id', m['events']), ('client_favorites', FAVORITE_FIELDS, 'id', m['favorites']),
                ('service_metrics_snapshot', b.METRIC_FIELDS, 'id', m['snapshots'])]:
            read_exact(c, table, fields, expected, key, True)
        read_exact(r, 'ratings', b.RATING_FIELDS, m['ratings'], complete=True)
        snapshots = [dict(order_id=o['id'], service_id=o['service'], client_id=o['client_id'], provider_id=o['provider_id'], is_completed=True, version=0) for o in m['orders'] if o['order_status'] == 'COMPLETED']
        read_exact(r, 'order_snapshots', b.SNAP_FIELDS, snapshots, 'order_id', True)
        live = {x['service_id']: x for x in b.rows(c, 'SELECT * FROM service_metrics WHERE service_id=ANY(%s)', (m['scope']['service_ids'],))}
        wanted = live_expected(m, {e['id'] for e in m['events']}, {f['id'] for f in m['favorites']})
        b.require(b.plain(live) == wanted, 'Contadores vivos no coinciden con el incremento sintético esperado.')
        for sid in m['scope']['service_ids']:
            count = b.rows(c, 'SELECT count(*) AS n FROM client_favorites WHERE service_id=%s', (sid,))[0]['n']
            b.require(live[sid]['favorite_count'] == count, 'Contador de favoritos inconsistente.')
    summary(m)
    announce('VERIFICACIÓN 2026 OK: fechas, filas propias, contadores, favoritos, ratings, snapshots y conservación del estado previo.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['plan', 'apply', 'verify'])
    p.add_argument('--file', default='plan_2026.json')
    p.add_argument('--old-plan', default='plan_2025.json')
    p.add_argument('--profiles-file', default='perfiles_esperados_2026.json')
    p.add_argument('--seed', type=int, default=20261031)
    p.add_argument('--allow-historical-write', action='store_true')
    p.add_argument('--test-database', action='store_true')
    args = p.parse_args()
    {'plan': plan, 'apply': apply, 'verify': verify}[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        announce(f'ERROR: {exc}')
        sys.exit(1)
