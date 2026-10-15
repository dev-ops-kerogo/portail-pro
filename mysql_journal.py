"""Journal d'activité dans MySQL (OVH) : une ligne par demande, mise à jour à chaque changement.

Sert au suivi / monitoring (qui a demandé quoi, quand, pour quel client, avec quel résultat).
Le fonctionnement du service ne dépend jamais de MySQL : la base opérationnelle reste SQLite (db.py) ;
si MySQL est injoignable, on journalise un avertissement et on continue.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import config
import db

log = logging.getLogger("mysql_journal")

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `{table}` (
  request_id        CHAR(36)      NOT NULL,
  created_at        DATETIME      NOT NULL,
  updated_at        DATETIME      NOT NULL,
  sent_at           DATETIME      NULL,
  status            VARCHAR(20)   NOT NULL,
  dry_run           TINYINT(1)    NOT NULL DEFAULT 0,
  slack_user_id     VARCHAR(30)   NOT NULL,
  slack_user_name   VARCHAR(100)  NULL,
  cabinet           VARCHAR(100)  NULL,
  campaign_id       VARCHAR(12)   NULL,
  sf_account_id     VARCHAR(18)   NULL,
  client_name       VARCHAR(255)  NULL,
  siren             VARCHAR(9)    NULL,
  client_email      VARCHAR(255)  NULL,
  cdm_nom           VARCHAR(150)  NULL,
  cdm_email         VARCHAR(255)  NULL,
  document_type     VARCHAR(20)   NOT NULL,
  annee             VARCHAR(4)    NOT NULL,
  periode           VARCHAR(20)   NULL,
  files_count       INT           NOT NULL DEFAULT 0,
  files             TEXT          NULL,
  skipped_count     INT           NOT NULL DEFAULT 0,
  gmail_message_id  VARCHAR(40)   NULL,
  sf_email_id       VARCHAR(18)   NULL,
  archived          TINYINT(1)    NOT NULL DEFAULT 0,
  test_recipient    VARCHAR(255)  NULL,
  error_message     TEXT          NULL,
  last_event        VARCHAR(30)   NULL,
  host              VARCHAR(100)  NULL,
  PRIMARY KEY (request_id),
  KEY idx_created (created_at),
  KEY idx_user (slack_user_id),
  KEY idx_client (siren),
  KEY idx_status (status),
  KEY idx_campaign (campaign_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

COLUMNS = ("request_id", "created_at", "updated_at", "sent_at", "status", "dry_run", "slack_user_id", "slack_user_name",
           "cabinet", "campaign_id", "sf_account_id", "client_name", "siren", "client_email", "cdm_nom", "cdm_email", "document_type",
           "annee", "periode", "files_count", "files", "skipped_count", "gmail_message_id", "sf_email_id", "archived",
           "test_recipient", "error_message", "last_event", "host")


def enabled() -> bool:
    return bool(config.MYSQL_HOST and config.MYSQL_DATABASE and config.MYSQL_USER)


class Journal:
    def __init__(self):
        self._con = None
        self._lock = threading.Lock()
        self._last_warning = 0.0
        self._host = __import__("socket").gethostname()[:100]

    # ------------------------------------------------------------ connexion
    def _connect(self):
        import pymysql
        kwargs = dict(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user=config.MYSQL_USER, password=config.MYSQL_PASSWORD,
                      database=config.MYSQL_DATABASE, charset="utf8mb4", autocommit=True, connect_timeout=10,
                      read_timeout=20, write_timeout=20)
        if config.MYSQL_SSL:
            kwargs["ssl"] = {"ssl": True}
        return pymysql.connect(**kwargs)

    def _conn(self):
        if self._con is None:
            self._con = self._connect()
        else:
            try:
                self._con.ping(reconnect=True)
            except Exception:
                self._con = self._connect()
        return self._con

    def ensure_table(self) -> None:
        with self._lock:
            with self._conn().cursor() as cur:
                cur.execute(TABLE_SQL.format(table=config.MYSQL_TABLE))
                cur.execute(f"SHOW COLUMNS FROM `{config.MYSQL_TABLE}`")
                if "campaign_id" not in {r[0] for r in cur.fetchall()}:
                    cur.execute(f"ALTER TABLE `{config.MYSQL_TABLE}` ADD COLUMN campaign_id VARCHAR(12) NULL AFTER cabinet, ADD KEY idx_campaign (campaign_id)")

    def count(self) -> int:
        with self._lock:
            with self._conn().cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM `{config.MYSQL_TABLE}`")
                return int(cur.fetchone()[0])

    # ------------------------------------------------------------ synchronisation
    def row_for(self, request_id: str) -> dict | None:
        req = db.get_request(request_id)
        if not req:
            return None
        docs = db.list_documents(request_id)
        events = db.list_events(request_id)
        files = [d["file_path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for d in docs if d["status"] == "DOWNLOADED" and d["file_path"]]
        sf_email_id = next((e["details"].split()[-1] for e in reversed(events) if e["event"] == "EMAIL_LOGGED"), None)
        return {
            "request_id": req["request_id"], "created_at": req["created_at"], "updated_at": req["updated_at"], "sent_at": req.get("sent_at"),
            "status": req["status"], "dry_run": int(bool(req.get("dry_run"))),
            "slack_user_id": req["slack_user_id"], "slack_user_name": (req.get("slack_user_name") or "")[:100] or None,
            "cabinet": req.get("entite"), "campaign_id": req.get("campaign_id"), "sf_account_id": req.get("sf_account_id"), "client_name": (req.get("client_name") or "")[:255] or None,
            "siren": req.get("siren"), "client_email": req.get("client_email"), "cdm_nom": req.get("cdm_nom"), "cdm_email": req.get("cdm_email"),
            "document_type": req["document_type"], "annee": req["annee"], "periode": req.get("periode"),
            "files_count": len(files), "files": json.dumps(files, ensure_ascii=False) if files else None,
            "skipped_count": sum(1 for d in docs if d["status"] == "SKIPPED"),
            "gmail_message_id": req.get("gmail_message_id"), "sf_email_id": sf_email_id,
            "archived": int(any(e["event"] == "ARCHIVED" for e in events)),
            "test_recipient": config.TEST_RECIPIENT if req["status"] == "SENT" and config.TEST_RECIPIENT else None,
            "error_message": req.get("error_message"), "last_event": events[-1]["event"] if events else None, "host": self._host,
        }

    def sync(self, request_id: str) -> None:
        """Insère ou met à jour la ligne de la demande. Ne lève jamais : un échec MySQL ne doit pas bloquer une demande."""
        try:
            row = self.row_for(request_id)
            if not row:
                return
            cols = ", ".join(f"`{c}`" for c in COLUMNS)
            marks = ", ".join(["%s"] * len(COLUMNS))
            updates = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in COLUMNS if c not in ("request_id", "created_at"))
            with self._lock:
                with self._conn().cursor() as cur:
                    cur.execute(f"INSERT INTO `{config.MYSQL_TABLE}` ({cols}) VALUES ({marks}) ON DUPLICATE KEY UPDATE {updates}",
                                [row[c] for c in COLUMNS])
        except Exception as e:
            self._con = None
            if time.time() - self._last_warning > 300:   # au plus un avertissement toutes les 5 min
                self._last_warning = time.time()
                log.warning("Journal MySQL indisponible (%s: %s) — le service continue sans lui", type(e).__name__, e)

    def resync_all(self, since_days: int = 90) -> int:
        """Rejoue toutes les demandes récentes de SQLite vers MySQL (rattrapage après une coupure)."""
        n = 0
        for rid in db.recent_request_ids(since_days):
            self.sync(rid)
            n += 1
        return n


journal = Journal()


def install() -> None:
    """Branche la synchronisation sur chaque écriture SQLite (voir db.on_change)."""
    if not enabled():
        log.info("Journal MySQL désactivé (MYSQL_HOST non renseigné)")
        return
    try:
        journal.ensure_table()
        log.info("Journal MySQL actif : %s/%s.%s", config.MYSQL_HOST, config.MYSQL_DATABASE, config.MYSQL_TABLE)
    except Exception as e:
        log.warning("Journal MySQL : table non vérifiée (%s) — nouvelles tentatives à chaque écriture", e)
    db.on_change = journal.sync
