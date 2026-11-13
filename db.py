"""Suivi des demandes en SQLite (une demande → N sous-documents → journal d'événements).

Statuts d'une demande : PENDING → PROCESSING → SENT | DRY_RUN | NO_DOCUMENT | FAILED
Statuts d'un sous-document : DOWNLOADED | SKIPPED (absent, normal) | FAILED (erreur technique)
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS document_requests (
  request_id TEXT PRIMARY KEY,
  slack_user_id TEXT NOT NULL,
  slack_user_name TEXT,
  sf_account_id TEXT,
  client_name TEXT,
  siren TEXT,
  entite TEXT,
  client_email TEXT,
  client_civilite TEXT,
  client_nom TEXT,
  client_prenom TEXT,
  cdm_nom TEXT,
  cdm_email TEXT,
  cdm_telephone TEXT,
  agence TEXT,
  campaign_id TEXT,
  document_type TEXT NOT NULL,
  annee TEXT NOT NULL,
  periode TEXT,
  dry_run INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'PENDING',
  error_message TEXT,
  gmail_message_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_status ON document_requests(status);
CREATE INDEX IF NOT EXISTS idx_req_dedup ON document_requests(sf_account_id, document_type, annee, periode, created_at);

CREATE TABLE IF NOT EXISTS request_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES document_requests(request_id),
  label TEXT,
  status TEXT NOT NULL,
  file_path TEXT,
  message TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_request ON request_documents(request_id);

CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_by_name TEXT,
  entite TEXT NOT NULL,
  document_type TEXT NOT NULL,
  annee TEXT NOT NULL,
  source TEXT NOT NULL,       -- salesforce | sirens
  sirens TEXT,                -- liste saisie (source = sirens)
  send_limit INTEGER,         -- nombre max d'envois (tests)
  status TEXT NOT NULL,       -- INVENTORY | INVENTORY_DONE | SENDING | DONE | CANCELLED | FAILED
  message TEXT,
  slack_channel TEXT,
  slack_ts TEXT,
  inventory_started_at TEXT, inventory_finished_at TEXT,
  send_started_at TEXT, send_finished_at TEXT,
  send_by TEXT
);
CREATE TABLE IF NOT EXISTS campaign_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
  sf_account_id TEXT,
  client_name TEXT,
  siren TEXT,
  entite TEXT,
  client_email TEXT,
  client_civilite TEXT, client_nom TEXT, client_prenom TEXT,
  cdm_nom TEXT, cdm_email TEXT, cdm_telephone TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING | FOUND | NO_DOCUMENT | NO_EMAIL | ALREADY_SENT | NOT_HABILITE | COMPTE_INDISPONIBLE | ERROR
  avis_count INTEGER,
  montant_total REAL,
  detail TEXT,
  request_id TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_citem_campaign ON campaign_items(campaign_id, status);
CREATE TABLE IF NOT EXISTS request_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  event TEXT NOT NULL,
  details TEXT,
  created_at TEXT NOT NULL
);
"""

# Appelé après chaque écriture avec l'identifiant de la demande (branché par mysql_journal.install()).
on_change = None


def _changed(request_id: str) -> None:
    if on_change is not None:
        try:
            on_change(request_id)
        except Exception:   # jamais bloquant
            pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(document_requests)")}
        if "dry_run" not in cols:   # base créée avant l'ajout du mode test
            con.execute("ALTER TABLE document_requests ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
        if "agence" not in cols:
            con.execute("ALTER TABLE document_requests ADD COLUMN agence TEXT")
        if "campaign_id" not in cols:
            con.execute("ALTER TABLE document_requests ADD COLUMN campaign_id TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS idx_req_campaign ON document_requests(campaign_id, status)")
        if "portail_compte" not in cols:   # compte portail sur lequel l'inventaire de campagne a trouvé le document
            con.execute("ALTER TABLE document_requests ADD COLUMN portail_compte TEXT")
        if "cas_paiement" not in cols:     # cas de paiement lu dans l'avis CFE (RIEN_A_PAYER / A_PAYER / PRELEVEMENT_ECHEANCE / MENSUALISE / INDETERMINE)
            con.execute("ALTER TABLE document_requests ADD COLUMN cas_paiement TEXT")
        if "action_cdm" not in cols:       # 1 si le chargé de mission doit aller payer (cas A_PAYER)
            con.execute("ALTER TABLE document_requests ADD COLUMN action_cdm INTEGER")
        item_cols = {r["name"] for r in con.execute("PRAGMA table_info(campaign_items)")}
        if "portail_compte" not in item_cols:
            con.execute("ALTER TABLE campaign_items ADD COLUMN portail_compte TEXT")


def create_request(fields: dict) -> str:
    request_id = str(uuid.uuid4())
    fields = {**fields, "request_id": request_id, "status": "PENDING", "created_at": now_iso(), "updated_at": now_iso()}
    cols = ", ".join(fields)
    with connect() as con:
        con.execute(f"INSERT INTO document_requests ({cols}) VALUES ({', '.join('?' * len(fields))})",
                    list(fields.values()))
    add_event(request_id, "CREATED", f"{fields.get('document_type')} {fields.get('annee')} — {fields.get('client_name')}")
    return request_id


def update_request(request_id: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE document_requests SET {sets} WHERE request_id = ?", [*fields.values(), request_id])
    _changed(request_id)


def get_request(request_id: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM document_requests WHERE request_id = ?", (request_id,)).fetchone()
    return dict(row) if row else None


def add_document(request_id: str, label: str, status: str, file_path: str | None = None, message: str = "") -> None:
    with connect() as con:
        con.execute("INSERT INTO request_documents (request_id, label, status, file_path, message, created_at) VALUES (?,?,?,?,?,?)",
                    (request_id, label, status, file_path, message, now_iso()))
    _changed(request_id)


def list_documents(request_id: str) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM request_documents WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
    return [dict(r) for r in rows]


def add_event(request_id: str, event: str, details: str = "") -> None:
    with connect() as con:
        con.execute("INSERT INTO request_events (request_id, event, details, created_at) VALUES (?,?,?,?)",
                    (request_id, event, details[:2000], now_iso()))
    _changed(request_id)


def list_events(request_id: str) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM request_events WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
    return [dict(r) for r in rows]


def recent_request_ids(days: int) -> list[str]:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as con:
        rows = con.execute("SELECT request_id FROM document_requests WHERE created_at >= ? ORDER BY created_at", (since,)).fetchall()
    return [r["request_id"] for r in rows]


def find_recent_duplicate(sf_account_id: str, document_type: str, annee: str, periode: str | None, days: int) -> dict | None:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as con:
        row = con.execute(
            """SELECT request_id, status, created_at, slack_user_name FROM document_requests
               WHERE sf_account_id = ? AND document_type = ? AND annee = ? AND IFNULL(periode,'') = IFNULL(?,'')
                 AND created_at >= ? AND dry_run = 0 AND status IN ('PENDING','PROCESSING','SENT')
               ORDER BY created_at DESC LIMIT 1""",
            (sf_account_id, document_type, annee, periode, since)).fetchone()
    return dict(row) if row else None


def pending_request_ids() -> list[str]:
    """Demandes non terminées au démarrage (reprise après redémarrage du service)."""
    with connect() as con:
        rows = con.execute("SELECT request_id FROM document_requests WHERE status IN ('PENDING','PROCESSING') ORDER BY created_at").fetchall()
    return [r["request_id"] for r in rows]


def finished_requests_older_than(days: int) -> list[str]:
    limit = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as con:
        rows = con.execute(
            "SELECT request_id FROM document_requests WHERE status IN ('SENT','DRY_RUN','NO_DOCUMENT','NOT_HABILITE','FAILED') AND updated_at < ?",
            (limit,)).fetchall()
    return [r["request_id"] for r in rows]


# ------------------------------------------------------------------ campagnes
def create_campaign(fields: dict) -> str:
    campaign_id = str(uuid.uuid4())[:8]
    fields = {**fields, "campaign_id": campaign_id, "status": "INVENTORY", "created_at": now_iso(), "updated_at": now_iso()}
    with connect() as con:
        con.execute(f"INSERT INTO campaigns ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", list(fields.values()))
    return campaign_id


def update_campaign(campaign_id: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    with connect() as con:
        con.execute(f"UPDATE campaigns SET {', '.join(f'{k} = ?' for k in fields)} WHERE campaign_id = ?", [*fields.values(), campaign_id])


def get_campaign(campaign_id: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)).fetchone()
    return dict(row) if row else None


def campaigns_with_status(*statuses: str) -> list[dict]:
    with connect() as con:
        rows = con.execute(f"SELECT * FROM campaigns WHERE status IN ({', '.join('?' * len(statuses))}) ORDER BY created_at", statuses).fetchall()
    return [dict(r) for r in rows]


def add_campaign_items(campaign_id: str, items: list[dict]) -> None:
    cols = ("sf_account_id", "client_name", "siren", "entite", "client_email", "client_civilite", "client_nom", "client_prenom",
            "cdm_nom", "cdm_email", "cdm_telephone")
    with connect() as con:
        con.executemany(f"INSERT INTO campaign_items (campaign_id, {', '.join(cols)}, status, updated_at) VALUES (?, {', '.join('?' * len(cols))}, 'PENDING', ?)",
                        [(campaign_id, *[it.get(c) for c in cols], now_iso()) for it in items])


def list_campaign_items(campaign_id: str, status: str | None = None) -> list[dict]:
    with connect() as con:
        if status:
            rows = con.execute("SELECT * FROM campaign_items WHERE campaign_id = ? AND status = ? ORDER BY id", (campaign_id, status)).fetchall()
        else:
            rows = con.execute("SELECT * FROM campaign_items WHERE campaign_id = ? ORDER BY id", (campaign_id,)).fetchall()
    return [dict(r) for r in rows]


def update_campaign_item(item_id: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    with connect() as con:
        con.execute(f"UPDATE campaign_items SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), item_id])


def campaign_item_counts(campaign_id: str) -> dict[str, int]:
    with connect() as con:
        rows = con.execute("SELECT status, COUNT(*) n FROM campaign_items WHERE campaign_id = ? GROUP BY status", (campaign_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def campaign_request_counts(campaign_id: str) -> dict[str, int]:
    with connect() as con:
        rows = con.execute("SELECT status, COUNT(*) n FROM document_requests WHERE campaign_id = ? GROUP BY status", (campaign_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def campaign_requests(campaign_id: str) -> dict[str, dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM document_requests WHERE campaign_id = ?", (campaign_id,)).fetchall()
    return {r["request_id"]: dict(r) for r in rows}


def already_sent(sf_account_id: str, document_type: str, annee: str) -> dict | None:
    """Une demande réelle (hors mode test) déjà envoyée pour ce client / document / année, quelle que soit la date."""
    with connect() as con:
        row = con.execute("""SELECT request_id, sent_at, slack_user_name FROM document_requests
                             WHERE sf_account_id = ? AND document_type = ? AND annee = ? AND dry_run = 0 AND status = 'SENT'
                             ORDER BY sent_at DESC LIMIT 1""", (sf_account_id, document_type, annee)).fetchone()
    return dict(row) if row else None


def sent_today_count() -> int:
    """Emails réels envoyés aujourd'hui (toutes demandes), pour respecter le quota Gmail quotidien."""
    today = datetime.now().strftime("%Y-%m-%d")
    with connect() as con:
        row = con.execute("SELECT COUNT(*) n FROM document_requests WHERE status = 'SENT' AND dry_run = 0 AND sent_at >= ?", (today,)).fetchone()
    return row["n"]
