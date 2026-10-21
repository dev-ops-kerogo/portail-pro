"""Comptes portail des chargés de mission : Google Sheet des accès (source principale, relue régulièrement) + .env.

Le Sheet (CREDENTIALS_SHEET_ID, onglet CREDENTIALS_SHEET_TAB ou le premier) est lu en LECTURE SEULE par le compte
technique Google (CREDENTIALS_SA_KEY_PATH), avec qui il doit être partagé. Colonnes reconnues par leur intitulé (casse et
accents indifférents) : « Mail de connexion » et « Mot de passe » (obligatoires), « Chargé de mission » (nom affiché, et
rattachement par le nom quand le mail diffère de celui de Salesforce), « Actif » (facultatif : non / false / 0 = ignoré).
Les autres colonnes (« Manager »…) sont ignorées.

Règles : une ligne par compte ; un mail présent plusieurs fois → la ligne la plus basse décide (même inactive ou sans
mot de passe : elle coupe alors le compte) ; un compte désactivé dans .env (PORTAIL_COLLAB<n>_ACTIF=false) le reste.
Sheet configuré → il est la SEULE source : les identifiants PORTAIL_COLLAB<n>_* du .env ne sont plus utilisés (jamais
de retour à un ancien mot de passe), et tant que le Sheet n'a jamais pu être lu les comptes sont « inconnus » (aucun
client n'est alors déclaré « non habilité »). Sheet illisible ensuite → derniers comptes lus conservés. Chute brutale
(0 compte, ou plus de la moitié retirée d'un coup) → derniers comptes conservés et alerte ; la baisse n'est acceptée que
si elle persiste DROP_CONFIRM_S (1 h).
Les mots de passe ne sont gardés qu'en mémoire : jamais écrits, jamais journalisés, jamais dans les alertes.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable

import config
from config import PortalAccount

log = logging.getLogger("accounts")

SOURCE_SHEET = "Google Sheet"
SHEETS_READONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"
# Mail strict : un mot de passe collé par erreur dans la cellule (« x@keobiz.fr;Secret! ») est rejeté, jamais recopié.
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
DROP_CONFIRM_S = 3600    # durée pendant laquelle une chute brutale du nombre de comptes doit persister pour être acceptée
INACTIVE = {"non", "no", "n", "false", "faux", "0", "off", "inactif", "inactive", "desactive", "desactivee"}


def _norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def name_key(name: str | None) -> str:
    """Nom comparable : sans accents ni casse, mots triés (« DURAND Camille » = « Camille Durand »)."""
    return " ".join(sorted(_norm(name).split()))


def find_account(accounts: dict[str, PortalAccount], cdm_email: str | None, cdm_nom: str | None = None) -> PortalAccount | None:
    """Compte portail du chargé de mission : par son email Salesforce, sinon par son nom s'il n'y a aucune ambiguïté."""
    acc = accounts.get((cdm_email or "").strip().lower())
    if acc or not name_key(cdm_nom):
        return acc
    hits = [a for a in accounts.values() if a.nom and name_key(a.nom) == name_key(cdm_nom)]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ lecture du Sheet
COLUMNS = {"login": ("mail de connexion", "mail", "email", "identifiant", "login"),
           "password": ("mot de passe", "mdp", "password"),
           "nom": ("charge de mission", "nom"),
           "actif": ("actif",)}


def parse_rows(rows: list[list[str]]) -> tuple[dict[str, PortalAccount], list[str]]:
    """Lignes brutes du Sheet (1re = intitulés) → ({email: compte}, remarques à signaler, SANS aucun mot de passe)."""
    if not rows:
        raise ValueError("Sheet vide (aucune ligne d'intitulés)")
    header = [_norm(h) for h in rows[0]]
    cols = {}
    for key, names in COLUMNS.items():   # intitulé exact d'abord, puis « commence par »
        idx = next((i for n in names for i, h in enumerate(header) if h == n), None)
        if idx is None:
            idx = next((i for n in names for i, h in enumerate(header) if h.startswith(n)), None)
        cols[key] = idx
    if cols["login"] is None or cols["password"] is None:
        raise ValueError("colonnes « Mail de connexion » et/ou « Mot de passe » introuvables dans la ligne d'intitulés")

    def cell(row: list[str], key: str) -> str:
        i = cols[key]
        return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

    found: dict[str, PortalAccount] = {}
    last: dict[str, str] = {}               # email → état de sa ligne la PLUS BASSE (ok, inactif, sans mot de passe)
    lines: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    notes: list[str] = []                   # n° de ligne et nom seulement : jamais le contenu brut d'une cellule
    for n, row in enumerate(rows[1:], start=2):   # n = numéro de ligne dans le Sheet
        login, password, nom = cell(row, "login"), cell(row, "password"), cell(row, "nom")
        if not login and not password:
            continue                                  # ligne vide ou ligne « manager » seule
        who = nom or "sans nom"
        if not EMAIL.fullmatch(login):
            notes.append(f"ligne {n} ({who}) : mail de connexion absent ou invalide")
            continue
        email = login.lower()
        lines.setdefault(email, []).append(n)
        names[email] = nom or names.get(email, "")
        if _norm(cell(row, "actif")) in INACTIVE:
            last[email] = "inactif"
            continue
        if not password:
            last[email] = "sans mot de passe"
            notes.append(f"ligne {n} ({who}) : mot de passe manquant")
            continue
        last[email] = "ok"
        found[email] = PortalAccount(code=config.collab_code(email), nom=nom, email=email, login=login,
                                     password=password, source=SOURCE_SHEET)
    # La ligne la plus basse décide, même inactive ou sans mot de passe : couper un compte en bas du Sheet ne
    # réactive jamais une ligne plus ancienne (et son ancien mot de passe).
    accounts = {e: a for e, a in found.items() if last.get(e) == "ok"}
    for email, ns in lines.items():
        if len(ns) > 1:
            notes.append(f"{names.get(email) or 'sans nom'} présent {len(ns)} fois (lignes {', '.join(map(str, ns))}) : "
                         + (f"la ligne {ns[-1]} est utilisée" if last.get(email) == "ok"
                            else f"la ligne {ns[-1]} ({last.get(email)}) coupe le compte"))
    return accounts, notes


def fetch_sheet(sheet_id: str, tab: str | None, key_path: str) -> list[list[str]]:
    """Valeurs de l'onglet, lues par le compte technique Google en lecture seule."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(key_path, scopes=[SHEETS_READONLY])
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    if not tab:
        meta = sheets.get(spreadsheetId=sheet_id, fields="sheets(properties(title,index))").execute()
        tab = min(meta["sheets"], key=lambda s: s["properties"].get("index", 0))["properties"]["title"]
    return sheets.values().get(spreadsheetId=sheet_id, range=f"'{tab}'").execute().get("values", [])


# ------------------------------------------------------------------ registre partagé (worker + campagnes)
class AccountRegistry:
    """Comptes à jour : relit le Sheet au plus toutes les refresh_s secondes (ou sur demande). Sheet configuré → seule
    source (le .env ne fournit plus que les désactivations ACTIF=false) ; pas de Sheet → comptes du .env."""

    def __init__(self, fetch_rows: Callable[[], list[list[str]]] | None = None,
                 load_env: Callable[[], dict[str, PortalAccount]] = config.load_collab_accounts,
                 disabled_env: Callable[[], set[str]] = config.disabled_collab_emails,
                 refresh_s: int = 900, on_notice: Callable[[str], None] | None = None,
                 count_file: Path | None = None):
        self.fetch_rows, self.load_env, self.disabled_env = fetch_rows, load_env, disabled_env
        self.refresh_s, self.on_notice, self.count_file = refresh_s, on_notice, count_file
        self._lock = threading.Lock()
        self._accounts: dict[str, PortalAccount] | None = None
        self._sheet: dict[str, PortalAccount] | None = None   # derniers comptes du Sheet retenus
        self._loaded_at = 0.0
        self._sheet_failing = False
        self._last_notes: tuple[str, ...] = ()
        self._drop_since: float | None = None                  # début d'une « chute brutale » en cours

    @property
    def unknown(self) -> bool:
        """Sheet configuré mais jamais lu depuis le démarrage : les comptes des chargés de mission sont INCONNUS (ce
        n'est pas « aucun compte ») — ne jamais conclure « non habilité » dans cet état."""
        if self._accounts is None:
            self.current()
        return self.fetch_rows is not None and self._sheet is None

    def current(self, force: bool = False) -> dict[str, PortalAccount]:
        with self._lock:
            # Sheet jamais lu : nouvel essai chaque minute (comptes inconnus), sinon toutes les refresh_s
            every = min(60, self.refresh_s) if self.fetch_rows is not None and self._sheet is None else self.refresh_s
            if force or self._accounts is None or time.time() - self._loaded_at >= every:
                self._reload()
            return dict(self._accounts or {})

    def summary(self) -> str:
        accs = self.current()
        if self.unknown:
            return "comptes des chargés de mission INCONNUS (Google Sheet des accès illisible)"
        n_sheet = sum(a.source == SOURCE_SHEET for a in accs.values())
        return f"{len(accs)} compte(s) de chargés de mission ({n_sheet} du Google Sheet, {len(accs) - n_sheet} du .env)"

    def _notify(self, text: str) -> None:
        log.warning(text)
        if self.on_notice:
            try:
                self.on_notice(text)
            except Exception as e:
                log.warning("Alerte impossible : %s", e)

    def _reload(self) -> None:
        self._loaded_at = time.time()
        disabled = self.disabled_env()
        if self.fetch_rows:
            try:
                sheet, notes = parse_rows(self.fetch_rows())
                if self._sheet_failing:
                    self._notify(":white_check_mark: Google Sheet des accès portail de nouveau lisible.")
                self._sheet_failing = False
                if tuple(notes) != self._last_notes and notes:
                    self._notify(":memo: Google Sheet des accès portail — à corriger :\n• " + "\n• ".join(notes[:20])
                                 + (f"\n• … et {len(notes) - 20} autre(s)" if len(notes) > 20 else ""))
                self._last_notes = tuple(notes)
                self._accept_sheet({e: a for e, a in sheet.items() if e not in disabled})
            except Exception as e:   # message d'erreur seul : jamais le contenu des cellules
                if not self._sheet_failing:
                    self._notify(f":warning: Google Sheet des accès portail illisible ({type(e).__name__}: {str(e)[:200]}) — "
                                 + ("derniers comptes lus conservés." if self._sheet is not None
                                    else "comptes des chargés de mission inconnus tant qu'il ne l'est pas "
                                         "(aucun client ne sera déclaré « non habilité »)."))
                self._sheet_failing = True
            merged = dict(self._sheet or {})   # jamais les mots de passe du .env quand un Sheet est configuré
        else:
            merged = {e: a for e, a in self.load_env().items() if e not in disabled and a.login.strip().lower() not in disabled}
        if self._accounts is not None:
            self._report_changes(self._accounts, merged)
        self._accounts = merged

    def _accept_sheet(self, sheet: dict[str, PortalAccount]) -> None:
        """Retient la lecture du Sheet, sauf chute brutale (Sheet vidé, filtre, suppression massive) : derniers comptes
        conservés et alerte ; la baisse n'est acceptée que si elle persiste DROP_CONFIRM_S (durée, quel que soit le
        nombre de relectures). Le nombre de comptes retenus est mémorisé (sans aucune donnée) pour signaler une chute
        survenue pendant un arrêt du service."""
        prev = self._sheet
        prev_n = len(prev) if prev is not None else self._stored_count()
        drop = prev_n > 0 and (len(sheet) == 0 or (prev_n >= 4 and len(sheet) < prev_n / 2))
        if drop and prev is None:   # premier lecture après redémarrage : rien à conserver, on signale
            self._notify(f":warning: Google Sheet des accès portail : {len(sheet)} compte(s) au lieu de {prev_n} avant le "
                         f"redémarrage du service — vérifier qu'aucune ligne n'a été supprimée par erreur.")
            drop = False
        if not drop:
            if self._drop_since is not None:
                self._notify(":white_check_mark: Google Sheet des accès portail : nombre de comptes revenu à la normale.")
            self._drop_since, self._sheet = None, sheet
            if prev is None and not sheet:
                self._notify(":warning: Google Sheet des accès portail : aucun compte utilisable.")
            self._store_count(len(sheet))
            return
        if self._drop_since is None:
            self._drop_since = time.time()
            self._notify(f":warning: Google Sheet des accès portail : {len(sheet)} compte(s) au lieu de {prev_n} — "
                         f"baisse suspecte, les {prev_n} derniers comptes sont conservés. Si c'est voulu, elle sera "
                         f"acceptée dans {DROP_CONFIRM_S // 60} min.")
        elif time.time() - self._drop_since >= DROP_CONFIRM_S:
            self._notify(f":information_source: Google Sheet des accès portail : baisse confirmée, {len(sheet)} compte(s) retenu(s).")
            self._drop_since, self._sheet = None, sheet
            self._store_count(len(sheet))

    def _stored_count(self) -> int:
        try:
            return int(json.loads(self.count_file.read_text(encoding="utf-8"))["comptes"]) if self.count_file else 0
        except Exception:
            return 0

    def _store_count(self, n: int) -> None:
        if self.count_file:
            try:
                self.count_file.parent.mkdir(parents=True, exist_ok=True)
                self.count_file.write_text(json.dumps({"comptes": n}), encoding="utf-8")
            except Exception as e:
                log.warning("Nombre de comptes non mémorisé (%s)", type(e).__name__)

    def _report_changes(self, old: dict[str, PortalAccount], new: dict[str, PortalAccount]) -> None:
        """Changements de comptes (adresses validées seulement, jamais de mot de passe) : journal + canal d'alertes."""
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(e for e in set(old) & set(new) if (old[e].login, old[e].password) != (new[e].login, new[e].password))
        if added or removed or changed:
            parts = [f"{label} : {', '.join(v[:15])}" + (f" … (+{len(v) - 15})" if len(v) > 15 else "")
                     for label, v in (("ajoutés", added), ("retirés", removed), ("mot de passe modifié", changed)) if v]
            self._notify(":arrows_counterclockwise: Comptes portail des chargés de mission mis à jour — " + " ; ".join(parts))


_REGISTRY: AccountRegistry | None = None


def get_registry(on_notice: Callable[[str], None] | None = None) -> AccountRegistry:
    """Registre du service (créé au premier appel) : Sheet si CREDENTIALS_SHEET_ID est renseigné, sinon .env seul."""
    global _REGISTRY
    if _REGISTRY is None:
        fetch = None
        if config.CREDENTIALS_SHEET_ID:
            if not config.CREDENTIALS_SA_KEY_PATH:
                raise RuntimeError("CREDENTIALS_SHEET_ID renseigné mais aucune clé de compte technique Google "
                                   "(CREDENTIALS_SA_KEY_PATH)")
            fetch = lambda: fetch_sheet(config.CREDENTIALS_SHEET_ID, config.CREDENTIALS_SHEET_TAB,   # noqa: E731
                                        config.CREDENTIALS_SA_KEY_PATH)
        _REGISTRY = AccountRegistry(fetch, refresh_s=config.CREDENTIALS_REFRESH_S, on_notice=on_notice,
                                    count_file=config.DATA_DIR / "comptes-portail-nombre.json" if fetch else None)
    elif on_notice and _REGISTRY.on_notice is None:
        _REGISTRY.on_notice = on_notice
    return _REGISTRY
