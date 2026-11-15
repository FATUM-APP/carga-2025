#!/usr/bin/env python3
"""Retira exclusivamente el seed sintético 2025 descrito por plan_2025.json.

El archivo de cleanup es un backup privado, no un journal: contiene las filas exactas
que el plan autorizó a retirar y sus checksums.  El procedimiento nunca elimina por
fecha.  Rate se confirma primero y Catalog después, por lo que una ejecución
interrumpida puede reanudarse con los mismos dos archivos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql

# Importación intencional: las columnas, serialización y conexión son las del seed
# vigente. Este módulo no modifica seed_fatum.py.
import seed_fatum as base


YEAR = 2025
FORMAT = 1
ZONE = "America/Bogota"
USER_SNAPSHOT_FIELDS = (
    "user_id name role total_score ratings_count version created_at updated_at"
).split()


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def progress(message: str) -> None:
    print(message, flush=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_checksum(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def plain(value: Any) -> Any:
    return base.plain(value)


def project(row: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {field: row[field] for field in fields}


def canonical_rows(rows: Iterable[dict[str, Any]], fields: Iterable[str], key_fields: Iterable[str]) -> list[dict[str, Any]]:
    fields = tuple(fields)
    key_fields = tuple(key_fields)
    result = [plain(project(row, fields)) for row in rows]
    return sorted(result, key=lambda row: tuple(str(row.get(k)) for k in key_fields))


def rows_checksum(rows: Iterable[dict[str, Any]], fields: Iterable[str], key_fields: Iterable[str]) -> str:
    payload = json.dumps(
        canonical_rows(rows, fields, key_fields),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def row_equal(actual: dict[str, Any], expected: dict[str, Any], fields: Iterable[str]) -> bool:
    fields = tuple(fields)
    return plain(project(actual, fields)) == plain(project(expected, fields))


def require_exact_rows(
    actual: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    fields: Iterable[str],
    key: str,
    label: str,
) -> None:
    fields = tuple(fields)
    actual_by_key = {row[key]: row for row in actual}
    expected_by_key = {row[key]: row for row in expected}
    extra = sorted(set(actual_by_key) - set(expected_by_key))
    missing = sorted(set(expected_by_key) - set(actual_by_key))
    require(not extra, f"{label}: filas extra no pertenecientes al manifiesto: {extra[:5]}")
    require(not missing, f"{label}: faltan filas del manifiesto: {missing[:5]}")
    changed = [k for k in expected_by_key if not row_equal(actual_by_key[k], expected_by_key[k], fields)]
    require(not changed, f"{label}: filas modificadas: {changed[:5]}")


def load_source(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    manifest = json.loads(raw.decode("utf-8"))
    require(manifest.get("format") == FORMAT, "Solo se admite format=1 en el manifiesto de origen.")
    require(manifest.get("year") == YEAR, "Este rollback solo admite year=2025.")
    require(manifest.get("zone") == ZONE, "Zona del manifiesto incompatible.")
    require(isinstance(manifest.get("namespace"), str), "Falta namespace UUID en el manifiesto.")
    try:
        uuid.UUID(manifest["namespace"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("namespace no es un UUID válido.") from exc
    require(isinstance(manifest.get("orders"), list), "El manifiesto no contiene orders.")
    require(isinstance(manifest.get("ratings"), list), "El manifiesto no contiene ratings.")
    order_ids = [row.get("id") for row in manifest["orders"]]
    rating_ids = [row.get("id") for row in manifest["ratings"]]
    require(all(order_ids), "Hay órdenes sin id en el manifiesto.")
    require(all(rating_ids), "Hay ratings sin id en el manifiesto.")
    require(len(order_ids) == len(set(order_ids)), "IDs de órdenes duplicados en el manifiesto.")
    require(len(rating_ids) == len(set(rating_ids)), "IDs de ratings duplicados en el manifiesto.")
    for row in manifest["orders"]:
        require(set(base.ORDER_FIELDS).issubset(row), f"Orden {row.get('id')} no tiene todos los ORDER_FIELDS.")
    for row in manifest["ratings"]:
        require(set(base.RATING_FIELDS).issubset(row), f"Rating {row.get('id')} no tiene todos los RATING_FIELDS.")
    return manifest, sha256_bytes(raw)


def completed_orders(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in manifest["orders"] if row["order_status"] == "COMPLETED"]


def expected_rate_snapshots(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "order_id": order["id"],
            "service_id": order["service"],
            "client_id": order["client_id"],
            "provider_id": order["provider_id"],
            "is_completed": True,
            "version": 0,
        }
        for order in completed_orders(manifest)
    ]


def cuts_2025() -> list[date]:
    cuts: list[date] = []
    current = date(YEAR, 1, 1)
    last = date(YEAR, 12, 31)
    while current <= last:
        if current.weekday() == 6:
            cuts.append(current)
        current += timedelta(days=1)
    if not cuts or cuts[-1] != last:
        cuts.append(last)
    return cuts


def parse_naive_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value)).replace(tzinfo=None)


def expected_catalog_snapshots(
    manifest: dict[str, Any],
    favorite_counts: dict[tuple[str, date], int],
) -> list[dict[str, Any]]:
    namespace = uuid.UUID(manifest["namespace"])
    cuts = cuts_2025()
    expected: list[dict[str, Any]] = []
    services = manifest.get("services") or []
    require(services, "El manifiesto no contiene servicios para reconstruir snapshots.")
    orders_by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in manifest["orders"]:
        orders_by_service[order["service"]].append(order)

    for service in services:
        service_id = service["id"]
        for cut in cuts:
            end = datetime.combine(cut, time(23, 59, 59))
            week_start = datetime.combine(cut - timedelta(days=cut.weekday()), time())
            history: list[str] = []
            relevant = orders_by_service.get(service_id, [])
            for order in relevant:
                created = parse_naive_datetime(order["created_at"])
                updated = parse_naive_datetime(order["updated_at"])
                if created > end:
                    continue
                accepted = order.get("_accepted_at")
                accepted_dt = parse_naive_datetime(accepted) if accepted else None
                booking = parse_naive_datetime(order["booking"])
                if updated <= end:
                    status = order["order_status"]
                elif accepted_dt and booking <= end:
                    status = "DELIVERING"
                elif accepted_dt and accepted_dt <= end:
                    status = "ACCEPTED"
                else:
                    status = "PENDING"
                history.append(status)
            counts: dict[str, int] = defaultdict(int)
            for status in history:
                counts[status] += 1
            iso_year, iso_week, _ = cut.isocalendar()
            row = {
                "id": str(uuid.uuid5(namespace, f"snapshot/{service_id}/{iso_year}/{iso_week}")),
                "service_id": service_id,
                "year": iso_year,
                "week": iso_week,
                "snapshot_date": cut.isoformat(),
                "views_in_period": 0,
                "dwell_seconds_in_period": 0,
                "dwell_events_in_period": 0,
                "new_orders_in_period": sum(
                    week_start <= parse_naive_datetime(order["created_at"]) <= end
                    for order in relevant
                ),
                "favorite_count": favorite_counts[(service_id, cut)],
                "total_orders": len(history),
                "pending_orders": counts["PENDING"],
                "accepted_orders": counts["ACCEPTED"],
                "rejected_orders": counts["REJECTED"],
                "delivering_orders": counts["DELIVERING"],
                "completed_orders": counts["COMPLETED"],
                "cancelled_orders": counts["CANCELLED"],
                "not_delivered_orders": 0,
                "dispute_related_orders": 0,
                "created_at": end.isoformat(),
            }
            expected.append(row)
    require(
        len({row["id"] for row in expected}) == len(expected),
        "Los IDs derivados de snapshots Catalog no son únicos.",
    )
    return expected


def favorite_counts(conn: Any, manifest: dict[str, Any]) -> dict[tuple[str, date], int]:
    result: dict[tuple[str, date], int] = {}
    service_ids = [service["id"] for service in manifest.get("services", [])]
    for index, service_id in enumerate(service_ids, 1):
        for cut in cuts_2025():
            end = datetime.combine(cut, time(23, 59, 59))
            row = base.rows(
                conn,
                "SELECT count(*) AS n FROM client_favorites WHERE service_id=%s AND created_at<=%s",
                (service_id, end),
            )[0]
            result[(service_id, cut)] = int(row["n"])
        if index == len(service_ids) or index % 5 == 0:
            progress(f"PLAN snapshots/favoritos: {index}/{len(service_ids)} servicios")
    return result


def validate_origin_manifest_shape(manifest: dict[str, Any]) -> None:
    order_by_id = {row["id"]: row for row in manifest["orders"]}
    ratings_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rating in manifest["ratings"]:
        ratings_by_order[rating["order_id"]].append(rating)
    require(set(ratings_by_order) <= set(order_by_id), "El manifiesto tiene ratings de órdenes ajenas.")
    for order in manifest["orders"]:
        expected_count = 3 if order["order_status"] == "COMPLETED" else 0
        require(
            len(ratings_by_order.get(order["id"], [])) == expected_count,
            f"Orden {order['id']} no tiene exactamente los ratings esperados del seed.",
        )
        if expected_count:
            types = {rating["target_type"] for rating in ratings_by_order[order["id"]]}
            require(types == {"SERVICE", "PROFESSIONAL", "CLIENT"}, f"Ratings incompletos para {order['id']}.")


def fetch_orders(conn: Any, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    return base.rows(conn, f"SELECT {','.join(base.ORDER_FIELDS)} FROM orders WHERE id=ANY(%s)", (ids,))


def fetch_ratings(conn: Any, order_ids: list[str]) -> list[dict[str, Any]]:
    if not order_ids:
        return []
    return base.rows(conn, f"SELECT {','.join(base.RATING_FIELDS)} FROM ratings WHERE order_id=ANY(%s)", (order_ids,))


def fetch_rate_snapshots(conn: Any, order_ids: list[str]) -> list[dict[str, Any]]:
    if not order_ids:
        return []
    return base.rows(conn, f"SELECT {','.join(base.SNAP_FIELDS)} FROM order_snapshots WHERE order_id=ANY(%s)", (order_ids,))


def fetch_user_snapshots(conn: Any, user_ids: list[str]) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    return base.rows(
        conn,
        f"SELECT {','.join(USER_SNAPSHOT_FIELDS)} FROM user_snapshots WHERE user_id=ANY(%s)",
        (user_ids,),
    )


def aggregate_by_user(conn: Any) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for row in base.rows(
        conn,
        """SELECT target_id, COALESCE(sum(score),0)::bigint AS total_score,
                  count(*)::integer AS ratings_count
           FROM ratings
           WHERE target_type IN ('CLIENT','PROFESSIONAL')
           GROUP BY target_id""",
    ):
        result[row["target_id"]] = (int(row["total_score"]), int(row["ratings_count"]))
    return result


def validate_all_aggregates(conn: Any) -> dict[str, dict[str, Any]]:
    aggregates = aggregate_by_user(conn)
    snapshots = fetch_user_snapshots_all(conn)
    by_user = {row["user_id"]: row for row in snapshots}
    missing = sorted(set(aggregates) - set(by_user))
    require(not missing, f"Rate tiene ratings dirigidos a usuarios sin user_snapshot: {missing[:5]}.")
    mismatches: list[str] = []
    for user_id, row in by_user.items():
        total, count = aggregates.get(user_id, (0, 0))
        if int(row["total_score"]) != total or int(row["ratings_count"]) != count:
            mismatches.append(user_id)
    require(not mismatches, f"user_snapshots inconsistente contra TODAS las ratings: {mismatches[:5]}.")
    return by_user


def fetch_user_snapshots_all(conn: Any) -> list[dict[str, Any]]:
    return base.rows(conn, f"SELECT {','.join(USER_SNAPSHOT_FIELDS)} FROM user_snapshots")


def dependencies_for_orders(conn: Any, order_ids: list[str]) -> list[dict[str, Any]]:
    """Encuentra hijos SQL y tablas convencionales order_id sin eliminar ninguna."""
    if not order_ids:
        return []
    dependencies: list[dict[str, Any]] = []
    fk_rows = base.rows(
        conn,
        """SELECT child_ns.nspname AS child_schema, child.relname AS child_table,
                  con.conname,
                  array_agg(child_att.attname ORDER BY key.ord) AS child_columns,
                  array_agg(parent_att.attname ORDER BY key.ord) AS parent_columns
           FROM pg_constraint con
           JOIN pg_class child ON child.oid=con.conrelid
           JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
           JOIN pg_class parent ON parent.oid=con.confrelid
           JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
           JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key(attnum,ord) ON TRUE
           JOIN pg_attribute child_att ON child_att.attrelid=child.oid AND child_att.attnum=key.attnum
           JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS pkey(attnum,ord)
             ON pkey.ord=key.ord
           JOIN pg_attribute parent_att ON parent_att.attrelid=parent.oid AND parent_att.attnum=pkey.attnum
           WHERE con.contype='f' AND parent_ns.nspname='public' AND parent.relname='orders'
           GROUP BY child_ns.nspname, child.relname, con.conname""",
    )
    seen: set[tuple[str, str, str]] = set()
    for fk in fk_rows:
        child_schema, child_table, conname = fk["child_schema"], fk["child_table"], fk["conname"]
        if child_schema == "public" and child_table == "orders":
            continue
        child_columns = list(fk["child_columns"])
        parent_columns = list(fk["parent_columns"])
        join = sql.SQL(" AND ").join(
            sql.SQL("c.{} = p.{}").format(sql.Identifier(child), sql.Identifier(parent))
            for child, parent in zip(child_columns, parent_columns)
        )
        query = sql.SQL(
            "SELECT count(*) AS n FROM {}.{} c JOIN public.orders p ON {} WHERE p.id=ANY(%s)"
        ).format(sql.Identifier(child_schema), sql.Identifier(child_table), join)
        n = conn.execute(query, (order_ids,)).fetchone()["n"]
        if n:
            key = (child_schema, child_table, conname)
            seen.add(key)
            dependencies.append({"table": f"{child_schema}.{child_table}", "constraint": conname, "count": int(n)})

    # Algunas versiones/esquemas de la aplicación pudieron tener order_disputes u
    # otras tablas sin FK. Revisar columnas convencionales evita que un DELETE de
    # orders provoque pérdida lógica aun cuando el DDL no declaró la relación.
    column_rows = base.rows(
        conn,
        """SELECT table_schema, table_name, column_name
           FROM information_schema.columns
           WHERE table_schema='public' AND table_name <> 'orders'
             AND lower(column_name) IN ('order_id','orderid','order_aws_id')
           ORDER BY table_schema, table_name, column_name""",
    )
    for item in column_rows:
        table = f"{item['table_schema']}.{item['table_name']}"
        column = item["column_name"]
        query = sql.SQL("SELECT count(*) AS n FROM {}.{} WHERE {}=ANY(%s)").format(
            sql.Identifier(item["table_schema"]),
            sql.Identifier(item["table_name"]),
            sql.Identifier(column),
        )
        n = conn.execute(query, (order_ids,)).fetchone()["n"]
        if n:
            key = (item["table_schema"], item["table_name"], column)
            if key not in seen:
                dependencies.append({"table": table, "column": column, "count": int(n)})
    return dependencies


def relevant_snapshot_rows(
    conn: Any,
    service_ids: list[str],
    snapshot_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not service_ids:
        return []
    id_clause = ""
    params: tuple[Any, ...] = (service_ids, YEAR, YEAR + 1)
    if snapshot_ids:
        id_clause = " OR id=ANY(%s)"
        params = (service_ids, YEAR, YEAR + 1, snapshot_ids)
    return base.rows(
        conn,
        f"""SELECT {','.join(base.METRIC_FIELDS)}
              FROM service_metrics_snapshot
             WHERE (service_id=ANY(%s)
               AND ((year=%s AND week BETWEEN 1 AND 53) OR (year=%s AND week=1)){id_clause})""",
        params,
    )


def validate_catalog_snapshot_set(
    conn: Any,
    manifest: dict[str, Any],
    expected: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]] | None = None,
    selected_ids: set[str] | None = None,
    phase: str = "plan",
) -> list[dict[str, Any]]:
    """Valida por identidad service/year/week; nunca acepta otra fuente."""
    expected_by_id = {row["id"]: row for row in expected}
    expected_by_identity = {(row["service_id"], row["year"], row["week"]): row for row in expected}
    current = relevant_snapshot_rows(
        conn,
        [s["id"] for s in manifest.get("services", [])],
        snapshot_ids=list(expected_by_id),
    )
    current_by_identity = {(row["service_id"], row["year"], row["week"]): row for row in current}
    require(
        len(current_by_identity) == len(current),
        "Hay snapshots Catalog duplicados por identidad; no se puede atribuir ownership.",
    )
    selected_rows = selected_rows or []
    selected_by_id = {row["id"]: row for row in selected_rows}
    selected_ids = selected_ids if selected_ids is not None else set(selected_by_id)
    found: list[dict[str, Any]] = []
    for identity, row in current_by_identity.items():
        owned = expected_by_identity.get(identity)
        require(owned is not None, f"Snapshot Catalog ajeno dentro del alcance 2025: {identity}.")
        expected_id = owned["id"]
        require(row["id"] == expected_id, f"Snapshot {identity} tiene ID de otra fuente; no se sobrescribe.")
        if phase == "plan":
            require(
                row_equal(row, owned, base.METRIC_FIELDS),
                f"Snapshot Catalog {row['id']} modificado o con favoritos/cronología distinta; abortando.",
            )
            found.append(row)
        else:
            require(
                expected_id in selected_ids,
                f"Apareció después del plan el snapshot Catalog {expected_id}; no se borra.",
            )
            saved = selected_by_id[expected_id]
            require(
                row_equal(row, saved, base.METRIC_FIELDS),
                f"Snapshot Catalog {expected_id} fue modificado desde plan; no se borra.",
            )
            require(
                row_equal(row, owned, base.METRIC_FIELDS),
                f"Favoritos/cronología actuales ya no coinciden con snapshot {expected_id}; no se borra.",
            )
            found.append(row)
    if phase == "plan":
        return found
    missing = selected_ids - {row["id"] for row in found}
    # La ausencia de una fila seleccionada es admisible: snapshots es opcional y
    # una ejecución previa pudo haber confirmado exactamente su DELETE.
    if missing:
        progress(f"CATALOG snapshots ya ausentes o no creados: {len(missing)}")
    return found


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    require(not path.exists(), f"El cleanup ya existe: {path}. No se sobrescribe.")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=base.serial)
            handle.write("\n")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def cleanup_checksum(cleanup: dict[str, Any]) -> str:
    data = dict(cleanup)
    data.pop("cleanup_checksum", None)
    return sha256_bytes(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=base.serial).encode()
    )


def load_cleanup(path: Path) -> dict[str, Any]:
    cleanup = json.loads(path.read_text(encoding="utf-8"))
    require(cleanup.get("cleanup_format") == FORMAT, "Formato de cleanup incompatible; solo format=1.")
    require(cleanup.get("year") == YEAR, "El cleanup no es de year=2025.")
    require(cleanup.get("zone") == ZONE, "Zona del cleanup incompatible.")
    try:
        uuid.UUID(cleanup["namespace"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("namespace del cleanup no es un UUID válido.") from exc
    require(cleanup.get("cleanup_checksum") == cleanup_checksum(cleanup), "Checksum del cleanup inválido.")
    required = {
        "source_manifest_sha256",
        "source_file",
        "namespace",
        "owned_ids",
        "exports",
        "checksums",
        "catalog_snapshot_expected",
    }
    require(required <= set(cleanup), "Cleanup incompleto; falta backup/contrato obligatorio.")
    return cleanup


def verify_source_unchanged(path: Path, cleanup: dict[str, Any]) -> dict[str, Any]:
    manifest, checksum = load_source(path)
    require(checksum == cleanup["source_manifest_sha256"], "Cambió plan_2025.json desde plan; no se escribe.")
    require(str(path.resolve()) == cleanup["source_file"], "El --file no coincide con el origen del cleanup.")
    return manifest


def validate_cleanup_matches_source(cleanup: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Impide que un cleanup privado sea editado para apuntar a otro conjunto."""
    require(cleanup["namespace"] == manifest["namespace"], "Namespace del cleanup no coincide con el manifiesto.")
    order_ids = [row["id"] for row in manifest["orders"]]
    rating_ids = [row["id"] for row in manifest["ratings"]]
    completed_ids = [row["id"] for row in completed_orders(manifest)]
    expected_orders = [project(row, base.ORDER_FIELDS) for row in manifest["orders"]]
    expected_ratings = [project(row, base.RATING_FIELDS) for row in manifest["ratings"]]
    expected_snaps = expected_rate_snapshots(manifest)
    exports = cleanup["exports"]
    require(cleanup["owned_ids"]["order_ids"] == order_ids, "owned_ids de órdenes no coincide con plan.")
    require(cleanup["owned_ids"]["rating_ids"] == rating_ids, "owned_ids de ratings no coincide con plan.")
    require(cleanup["owned_ids"]["rate_order_snapshot_ids"] == completed_ids, "owned_ids de snapshots Rate no coincide con plan.")
    require_exact_rows(exports["orders"], expected_orders, base.ORDER_FIELDS, "id", "backup orders contra plan")
    require_exact_rows(exports["ratings"], expected_ratings, base.RATING_FIELDS, "id", "backup ratings contra plan")
    require_exact_rows(exports["rate_order_snapshots"], expected_snaps, base.SNAP_FIELDS, "order_id", "backup snapshots Rate contra plan")
    expected_catalog_ids = {
        row["id"] for row in cleanup["catalog_snapshot_expected"]
    }
    namespace = uuid.UUID(manifest["namespace"])
    derived_catalog_ids = {
        str(uuid.uuid5(namespace, f"snapshot/{service['id']}/{iso_year}/{iso_week}"))
        for service in (manifest.get("services") or [])
        for cut in cuts_2025()
        for iso_year, iso_week, _ in (cut.isocalendar(),)
    }
    require(expected_catalog_ids == derived_catalog_ids, "IDs Catalog no son exactamente los UUID5 derivados del plan.")
    require(
        set(cleanup["owned_ids"]["catalog_metric_snapshot_ids"]) == expected_catalog_ids,
        "IDs derivados de snapshots Catalog fueron alterados.",
    )
    require(
        set(cleanup["owned_ids"]["catalog_metric_snapshot_selected_ids"])
        == {row["id"] for row in exports["catalog_metric_snapshots"]},
        "Selección de snapshots Catalog no coincide con su backup.",
    )
    expected_services = {service["id"] for service in (manifest.get("services") or [])}
    require(expected_services, "El manifiesto no tiene servicios.")
    require(
        all(row["service_id"] in expected_services for row in cleanup["catalog_snapshot_expected"]),
        "Backup Catalog contiene un servicio ajeno al manifiesto.",
    )
    touched = {
        rating["target_id"]
        for rating in manifest["ratings"]
        if rating["target_type"] in ("CLIENT", "PROFESSIONAL")
    }
    require(set(cleanup["owned_ids"]["touched_user_ids"]) == touched, "Usuarios tocados del cleanup no coinciden con ratings.")


def plan(args: argparse.Namespace) -> None:
    source_path = Path(args.file).resolve()
    cleanup_path = Path(args.cleanup_file).resolve()
    manifest, source_hash = load_source(source_path)
    validate_origin_manifest_shape(manifest)
    order_ids = [row["id"] for row in manifest["orders"]]
    completed_ids = [row["id"] for row in completed_orders(manifest)]
    touched_user_ids = sorted(
        {rating["target_id"] for rating in manifest["ratings"] if rating["target_type"] in ("CLIENT", "PROFESSIONAL")}
    )
    expected_orders = [project(row, base.ORDER_FIELDS) for row in manifest["orders"]]
    expected_ratings = [project(row, base.RATING_FIELDS) for row in manifest["ratings"]]
    expected_snaps = expected_rate_snapshots(manifest)

    progress("PLAN comprobando que el seed 2025 quedó completamente aplicado...")
    with base.db("CATALOG_DSN") as catalog, base.db("RATE_DSN") as rate:
        current_orders = fetch_orders(catalog, order_ids)
        require(len(current_orders) == len(order_ids), "Origen parcial: faltan órdenes del seed; no se crea cleanup.")
        require_exact_rows(current_orders, expected_orders, base.ORDER_FIELDS, "id", "orders")
        dependencies = dependencies_for_orders(catalog, order_ids)
        require(not dependencies, f"Hay tablas hijas de órdenes; no se puede atribuir ownership: {dependencies[:5]}")

        current_ratings = fetch_ratings(rate, order_ids)
        require_exact_rows(current_ratings, expected_ratings, base.RATING_FIELDS, "id", "ratings")
        current_snaps = fetch_rate_snapshots(rate, order_ids)
        require_exact_rows(current_snaps, expected_snaps, base.SNAP_FIELDS, "order_id", "order_snapshots")
        require(all(row["is_completed"] and int(row["version"]) == 0 for row in current_snaps), "Snapshot Rate no es completed/version=0.")
        touched = fetch_user_snapshots(rate, touched_user_ids)
        validate_all_aggregates(rate)
        require(
            set(row["user_id"] for row in touched) == set(touched_user_ids),
            "Falta proyección user_snapshot para un usuario tocado por el seed.",
        )

        counts = favorite_counts(catalog, manifest)
        expected_catalog = expected_catalog_snapshots(manifest, counts)
        catalog_rows = validate_catalog_snapshot_set(catalog, manifest, expected_catalog, phase="plan")
        progress(f"PLAN snapshots Catalog existentes y atribuibles: {len(catalog_rows)}/{len(expected_catalog)}")

        exports = {
            "orders": [plain(row) for row in expected_orders],
            "ratings": [plain(row) for row in expected_ratings],
            "rate_order_snapshots": [plain(project(row, base.SNAP_FIELDS)) for row in current_snaps],
            "catalog_metric_snapshots": [plain(project(row, base.METRIC_FIELDS)) for row in catalog_rows],
            "touched_user_snapshots": [plain(project(row, USER_SNAPSHOT_FIELDS)) for row in touched],
        }
        checksums = {
            "orders": rows_checksum(exports["orders"], base.ORDER_FIELDS, ("id",)),
            "ratings": rows_checksum(exports["ratings"], base.RATING_FIELDS, ("id",)),
            "rate_order_snapshots": rows_checksum(exports["rate_order_snapshots"], base.SNAP_FIELDS, ("order_id",)),
            "catalog_metric_snapshots": rows_checksum(exports["catalog_metric_snapshots"], base.METRIC_FIELDS, ("id",)),
            "touched_user_snapshots": rows_checksum(exports["touched_user_snapshots"], USER_SNAPSHOT_FIELDS, ("user_id",)),
        }
        cleanup = {
            "cleanup_format": FORMAT,
            "year": YEAR,
            "zone": ZONE,
            "source_file": str(source_path),
            "source_manifest_sha256": source_hash,
            "namespace": manifest["namespace"],
            "owned_ids": {
                "order_ids": order_ids,
                "rating_ids": [row["id"] for row in expected_ratings],
                "rate_order_snapshot_ids": completed_ids,
                "catalog_metric_snapshot_ids": [row["id"] for row in expected_catalog],
                "catalog_metric_snapshot_selected_ids": [row["id"] for row in catalog_rows],
                "touched_user_ids": touched_user_ids,
            },
            "exports": exports,
            "catalog_snapshot_expected": [project(row, base.METRIC_FIELDS) for row in expected_catalog],
            "favorite_counts_as_of_cut": {
                f"{service_id}|{cut.isoformat()}": count
                for (service_id, cut), count in counts.items()
            },
            "checksums": checksums,
            "contract": {
                "rate_must_be_complete_before_plan": True,
                "catalog_metric_snapshots_optional": True,
                "delete_order_ids_only": True,
                "delete_rating_ids_only": True,
                "never_delete": [
                    "users", "addresses", "owner_availability", "provider_unavailable_dates",
                    "services", "service_portafolio", "categories", "client_favorites",
                    "service_view_event", "service_metrics", "user_snapshots",
                ],
            },
        }
    cleanup["cleanup_checksum"] = cleanup_checksum(cleanup)
    write_private_json(cleanup_path, cleanup)
    progress(
        f"PLAN OK: backup privado guardado en {cleanup_path} "
        f"(órdenes={len(exports['orders'])}, "
        f"ratings={len(expected_ratings)}, snapshotsRate={len(expected_snaps)}, "
        f"snapshotsCatalog={len(catalog_rows)} seleccionados)."
    )


def validate_cleanup_exports(cleanup: dict[str, Any]) -> None:
    exports = cleanup["exports"]
    checksums = cleanup["checksums"]
    specs = {
        "orders": (base.ORDER_FIELDS, ("id",)),
        "ratings": (base.RATING_FIELDS, ("id",)),
        "rate_order_snapshots": (base.SNAP_FIELDS, ("order_id",)),
        "catalog_metric_snapshots": (base.METRIC_FIELDS, ("id",)),
        "touched_user_snapshots": (USER_SNAPSHOT_FIELDS, ("user_id",)),
    }
    for name, (fields, keys) in specs.items():
        require(name in exports and name in checksums, f"Cleanup sin export/checksum de {name}.")
        require(checksums[name] == rows_checksum(exports[name], fields, keys), f"Checksum de export {name} inválido.")
    require(len(exports["orders"]) == 950 or len(exports["orders"]) > 0, "Cleanup sin órdenes.")
    require(len(exports["ratings"]) > 0, "Cleanup sin ratings; el origen no está completamente aplicado.")
    require(all(row.get("is_completed") and int(row.get("version", -1)) == 0 for row in exports["rate_order_snapshots"]), "Backup Rate inválido: snapshots no son completed/version=0.")
    expected_ids = {row["id"] for row in cleanup["catalog_snapshot_expected"]}
    selected_ids = set(cleanup["owned_ids"]["catalog_metric_snapshot_selected_ids"])
    exported_ids = {row["id"] for row in exports["catalog_metric_snapshots"]}
    require(exported_ids == selected_ids, "Los snapshots Catalog exportados no coinciden con IDs seleccionados.")
    require(selected_ids <= expected_ids, "Hay snapshot Catalog seleccionado fuera del conjunto derivado.")


def expected_catalog_from_cleanup(cleanup: dict[str, Any]) -> list[dict[str, Any]]:
    return list(cleanup["catalog_snapshot_expected"])


def current_rate_stage(conn: Any, cleanup: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    order_ids = cleanup["owned_ids"]["order_ids"]
    expected_ratings = cleanup["exports"]["ratings"]
    expected_snaps = cleanup["exports"]["rate_order_snapshots"]
    current_ratings = fetch_ratings(conn, order_ids)
    expected_rating_ids = {row["id"] for row in expected_ratings}
    extra_rating_rows = [row for row in current_ratings if row["id"] not in expected_rating_ids]
    require(not extra_rating_rows, "Hay ratings extra no pertenecientes al manifiesto para órdenes seed; abortando.")
    current_snaps = fetch_rate_snapshots(conn, order_ids)
    expected_snap_ids = {row["order_id"] for row in expected_snaps}
    extra_snap_rows = [row for row in current_snaps if row["order_id"] not in expected_snap_ids]
    require(not extra_snap_rows, "Hay order_snapshots extra para órdenes seed; abortando.")
    rating_ids = {row["id"] for row in current_ratings}
    snap_ids = {row["order_id"] for row in current_snaps}
    if not current_ratings and not current_snaps:
        return current_ratings, current_snaps
    require(
        rating_ids == expected_rating_ids and snap_ids == expected_snap_ids,
        "Rate quedó parcial; se esperaba todo el conjunto o todo ausente. No se adivina el estado.",
    )
    require_exact_rows(current_ratings, expected_ratings, base.RATING_FIELDS, "id", "ratings actuales")
    require_exact_rows(current_snaps, expected_snaps, base.SNAP_FIELDS, "order_id", "order_snapshots actuales")
    require(all(row["is_completed"] and int(row["version"]) == 0 for row in current_snaps), "Rate snapshot alterado: exige is_completed=true y version=0.")
    return current_ratings, current_snaps


def projection_expected_after(cleanup: dict[str, Any]) -> dict[str, dict[str, Any]]:
    before = {row["user_id"]: row for row in cleanup["exports"]["touched_user_snapshots"]}
    deltas: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for rating in cleanup["exports"]["ratings"]:
        if rating["target_type"] in ("CLIENT", "PROFESSIONAL"):
            total, count = deltas[rating["target_id"]]
            deltas[rating["target_id"]] = (total + int(rating["score"]), count + 1)
    result: dict[str, dict[str, Any]] = {}
    for user_id, row in before.items():
        total, count = deltas.get(user_id, (0, 0))
        expected = dict(row)
        expected["total_score"] = int(row["total_score"]) - total
        expected["ratings_count"] = int(row["ratings_count"]) - count
        expected["version"] = int(row["version"]) + (1 if count else 0)
        result[user_id] = expected
    return result


def validate_projection_after(conn: Any, cleanup: dict[str, Any]) -> None:
    all_snapshots = validate_all_aggregates(conn)
    expected = projection_expected_after(cleanup)
    for user_id, wanted in expected.items():
        actual = all_snapshots.get(user_id)
        require(actual is not None, f"Falta user_snapshot tocado después de Rate: {user_id}.")
        for field in ("user_id", "name", "role", "total_score", "ratings_count", "version", "created_at"):
            require(
                plain(actual[field]) == plain(wanted[field]),
                f"user_snapshot {user_id} no coincide con el agregado esperado después de borrar ratings ({field}).",
            )
        require(actual["updated_at"] is not None, f"user_snapshot {user_id} quedó sin updated_at.")


def apply_rate(cleanup: dict[str, Any]) -> None:
    progress("APPLY Rate: tomando lock y validando conjunto completo/ausente...")
    with base.db("RATE_DSN", False) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('fatum-seed-rate'))")
        current_ratings, current_snaps = current_rate_stage(conn, cleanup)
        touched_ids = cleanup["owned_ids"]["touched_user_ids"]
        if not current_ratings and not current_snaps:
            validate_projection_after(conn, cleanup)
            progress("APPLY Rate: ya estaba vacío; proyección y agregados siguen consistentes.")
            return

        # Antes de tocar filas, todos los acumulados deben representar TODAS las
        # ratings actuales, y la proyección tocada debe ser exactamente el backup.
        validate_all_aggregates(conn)
        before = {row["user_id"]: row for row in cleanup["exports"]["touched_user_snapshots"]}
        actual_touched = fetch_user_snapshots(conn, touched_ids)
        require_exact_rows(actual_touched, list(before.values()), USER_SNAPSHOT_FIELDS, "user_id", "user_snapshots antes de borrar")

        rating_ids = cleanup["owned_ids"]["rating_ids"]
        deleted_ratings = conn.execute(
            "DELETE FROM ratings WHERE id=ANY(%s) RETURNING " + ",".join(base.RATING_FIELDS),
            (rating_ids,),
        ).fetchall()
        require(len(deleted_ratings) == len(rating_ids), "No se borró exactamente el conjunto de ratings autorizado.")
        progress(f"APPLY Rate ratings: borradas {len(deleted_ratings)}/{len(rating_ids)} filas")

        # Solo CLIENT/PROFESSIONAL afectan user_snapshots. Una sola sentencia por
        # usuario da exactamente un incremento de version por update, no por rating.
        deltas: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
        for rating in deleted_ratings:
            if rating["target_type"] in ("CLIENT", "PROFESSIONAL"):
                total, count = deltas[rating["target_id"]]
                deltas[rating["target_id"]] = (total + int(rating["score"]), count + 1)
        now_updates = 0
        for user_id, (total, count) in sorted(deltas.items()):
            updated = conn.execute(
                """UPDATE user_snapshots
                      SET total_score=total_score-%s,
                          ratings_count=ratings_count-%s,
                          version=version+1,
                          updated_at=NOW()
                    WHERE user_id=%s
                    RETURNING user_id""",
                (total, count, user_id),
            ).fetchone()
            require(updated is not None, f"Falta user_snapshot para rating dirigido a {user_id}; rollback transaccional.")
            now_updates += 1
        progress(f"APPLY Rate proyecciones: {now_updates} usuarios actualizados; version +1 por usuario")

        snapshot_ids = cleanup["owned_ids"]["rate_order_snapshot_ids"]
        deleted_snaps = conn.execute(
            "DELETE FROM order_snapshots WHERE order_id=ANY(%s) RETURNING " + ",".join(base.SNAP_FIELDS),
            (snapshot_ids,),
        ).fetchall()
        require(len(deleted_snaps) == len(snapshot_ids), "No se borró exactamente el conjunto autorizado de order_snapshots.")
        validate_projection_after(conn, cleanup)
        progress(f"APPLY Rate OK: {len(deleted_ratings)} ratings y {len(deleted_snaps)} snapshots en una transacción")


def current_catalog_orders(conn: Any, cleanup: dict[str, Any]) -> list[dict[str, Any]]:
    return fetch_orders(conn, cleanup["owned_ids"]["order_ids"])


def apply_catalog(cleanup: dict[str, Any], manifest: dict[str, Any]) -> None:
    progress("APPLY Catalog: tomando lock, revisando hijos y snapshots atribuibles...")
    with base.db("CATALOG_DSN", False) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('fatum-seed-catalog'))")
        order_ids = cleanup["owned_ids"]["order_ids"]
        expected_orders = cleanup["exports"]["orders"]
        current_orders = current_catalog_orders(conn, cleanup)
        current_order_ids = {row["id"] for row in current_orders}
        expected_order_ids = set(order_ids)
        require(
            not current_order_ids or current_order_ids == expected_order_ids,
            "Catalog quedó parcial; se esperaba todo orders o todo ausente. No se adivina el estado.",
        )
        dependencies = dependencies_for_orders(conn, order_ids)
        require(not dependencies, f"Hay hijas nuevas de orders; no se aplica CASCADE ni se borra: {dependencies[:5]}")
        if current_orders:
            require_exact_rows(current_orders, expected_orders, base.ORDER_FIELDS, "id", "orders actuales")

        expected_snapshots = expected_catalog_from_cleanup(cleanup)
        selected_rows = cleanup["exports"]["catalog_metric_snapshots"]
        selected_ids = set(cleanup["owned_ids"]["catalog_metric_snapshot_selected_ids"])
        # Recalcular favoritos as-of-cut desde la BD viva para detectar cambios
        # después del plan. No se modifica ni se reconstruye ningún snapshot.
        counts = favorite_counts(conn, manifest)
        regenerated = expected_catalog_snapshots(manifest, counts)
        regenerated_by_id = {row["id"]: row for row in regenerated}
        saved_expected_by_id = {row["id"]: row for row in expected_snapshots}
        for snapshot_id in selected_ids:
            require(snapshot_id in saved_expected_by_id, f"Snapshot seleccionado fuera del expected derivado: {snapshot_id}")
            require(
                row_equal(regenerated_by_id[snapshot_id], saved_expected_by_id[snapshot_id], base.METRIC_FIELDS),
                f"Favoritos/cronología cambiaron desde plan para snapshot {snapshot_id}; no se borra.",
            )
        validate_catalog_snapshot_set(
            conn,
            manifest,
            regenerated,
            selected_rows=selected_rows,
            selected_ids=selected_ids,
            phase="apply",
        )
        if selected_ids:
            deleted_metric = conn.execute(
                "DELETE FROM service_metrics_snapshot WHERE id=ANY(%s) RETURNING id",
                (list(selected_ids),),
            ).fetchall()
            actual_metric_ids = {row["id"] for row in deleted_metric}
            require(actual_metric_ids <= selected_ids, "DELETE de snapshots devolvió IDs fuera del plan.")
            progress(f"APPLY Catalog snapshots: retirados {len(actual_metric_ids)}/{len(selected_ids)}")

        if current_orders:
            deleted_orders = conn.execute(
                "DELETE FROM orders WHERE id=ANY(%s) RETURNING id",
                (order_ids,),
            ).fetchall()
            require({row["id"] for row in deleted_orders} == expected_order_ids, "No se borró exactamente orders del plan.")
            progress(f"APPLY Catalog orders: retiradas {len(deleted_orders)}")
        else:
            progress("APPLY Catalog orders: ya estaban ausentes; no se borra por fecha.")
    progress("APPLY Catalog OK: transacción confirmada; service_metrics y datos protegidos intactos.")


def verify_absence(cleanup: dict[str, Any], manifest: dict[str, Any]) -> None:
    order_ids = cleanup["owned_ids"]["order_ids"]
    with base.db("RATE_DSN") as rate:
        remaining_ratings = fetch_ratings(rate, order_ids)
        require(not remaining_ratings, "VERIFY: aún hay ratings para órdenes del plan.")
        remaining_snaps = fetch_rate_snapshots(rate, order_ids)
        require(not remaining_snaps, "VERIFY: aún hay order_snapshots para órdenes del plan.")
        validate_all_aggregates(rate)
        validate_projection_after(rate, cleanup)
    with base.db("CATALOG_DSN") as catalog:
        remaining_orders = current_catalog_orders(catalog, cleanup)
        require(not remaining_orders, "VERIFY: aún hay orders del plan.")
        expected = expected_catalog_from_cleanup(cleanup)
        current = relevant_snapshot_rows(
            catalog,
            [s["id"] for s in manifest.get("services", [])],
            snapshot_ids=[row["id"] for row in expected],
        )
        expected_identities = {(row["service_id"], row["year"], row["week"]) for row in expected}
        expected_ids = {row["id"] for row in expected}
        unexpected = [
            row for row in current
            if (row["service_id"], row["year"], row["week"]) in expected_identities
            or row["id"] in expected_ids
        ]
        require(not unexpected, "VERIFY: aún hay snapshots Catalog con identidades derivadas del plan.")
        dependencies = dependencies_for_orders(catalog, order_ids)
        require(not dependencies, f"VERIFY: aparecieron hijas para órdenes ya retiradas: {dependencies[:5]}")
    progress(
        f"VERIFY OK: ausentes orders={len(order_ids)}, ratings={len(cleanup['owned_ids']['rating_ids'])}, "
        f"snapshotsRate={len(cleanup['owned_ids']['rate_order_snapshot_ids'])}; agregados contra TODAS las ratings consistentes."
    )


def apply(args: argparse.Namespace) -> None:
    require(args.confirm_delete_seed_2025 and args.test_database, "apply requiere --confirm-delete-seed-2025 --test-database.")
    cleanup = load_cleanup(Path(args.cleanup_file).resolve())
    validate_cleanup_exports(cleanup)
    manifest = verify_source_unchanged(Path(args.file).resolve(), cleanup)
    validate_origin_manifest_shape(manifest)
    validate_cleanup_matches_source(cleanup, manifest)
    progress("APPLY: Rate primero; ningún DELETE se basa en fechas.")
    apply_rate(cleanup)
    apply_catalog(cleanup, manifest)
    verify_absence(cleanup, manifest)


def verify(args: argparse.Namespace) -> None:
    cleanup = load_cleanup(Path(args.cleanup_file).resolve())
    validate_cleanup_exports(cleanup)
    manifest = verify_source_unchanged(Path(args.file).resolve(), cleanup)
    validate_origin_manifest_shape(manifest)
    validate_cleanup_matches_source(cleanup, manifest)
    verify_absence(cleanup, manifest)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("plan", "apply", "verify"))
    result.add_argument("--file", default="plan_2025.json", help="Manifiesto de seed, únicamente year=2025 format=1.")
    result.add_argument("--cleanup-file", default="retiro_2025.json", help="Backup/contrato privado de cleanup.")
    result.add_argument("--confirm-delete-seed-2025", action="store_true")
    result.add_argument("--test-database", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.command == "plan":
        plan(args)
    elif args.command == "apply":
        apply(args)
    else:
        verify(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
