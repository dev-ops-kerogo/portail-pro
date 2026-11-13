"""Campagnes : traitement en masse d'un document fiscal pour tous les clients d'un cabinet.

Deux temps, chacun déclenché par un clic d'un administrateur de campagne (CAMPAIGN_ADMINS) dans Slack :
  1. Inventaire  — interroge le portail pour chaque client, sans rien envoyer ; produit un rapport Excel.
  2. Envoi       — après clic « Lancer l'envoi » : une demande par client retenu, traitée par le worker habituel
                   (mêmes garde-fous), au rythme CAMPAIGN_SEND_INTERVAL_S et dans la limite du quota Gmail quotidien.
Rien ne démarre jamais tout seul. Un rapport final Excel est déposé dans CAMPAIGN_CHANNEL.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import accounts
import config
import db
import documents
from config import Cabinet, PortalAccount
import httpx

from portailpro import (NonHabiliteError, PortailLoginAbandoned, PortailLoginRefused, PortailMaintenanceError, PortailPro,
                        PortailProError, login_blocked_until)

log = logging.getLogger("campaign")

ITEM_LABELS = {"FOUND": "Avis trouvé", "NO_DOCUMENT": "Aucun avis", "NO_EMAIL": "Email client manquant",
               "ALREADY_SENT": "Déjà envoyé", "NOT_HABILITE": "Aucun compte habilité",
               "COMPTE_INDISPONIBLE": "Compte portail indisponible (à revérifier)", "ERROR": "Erreur technique",
               "PENDING": "Non traité"}
# Erreurs consécutives d'un même compte : pause d'une minute à partir de ERRORS_PAUSE ; à ERRORS_STOP, le compte du
# cabinet arrête l'inventaire et celui d'un chargé de mission est écarté (ses clients → « compte portail indisponible »).
ERRORS_PAUSE, ERRORS_STOP = 5, 20
LOGIN_STOPPED = (PortailLoginRefused, PortailLoginAbandoned)   # connexion à ne plus jamais retenter sur ce compte


MAINTENANCE_PAUSE_S = 300        # portail en maintenance : nouvel essai du même client dans 5 min
MAX_PAUSE_S = 6 * 3600           # au-delà de 6 h de pause cumulée, l'inventaire s'arrête


class InventoryPause(Exception):
    """Le portail ne peut pas être interrogé pour l'instant (maintenance, connexion du cabinet suspendue) : pause,
    puis nouvel essai du MÊME client — ni erreur comptée, ni client mal classé."""

    def __init__(self, until: float, reason: str):
        super().__init__(reason)
        self.until, self.reason = until, reason


class SessionReopenError(PortailProError):
    """Réouverture passagèrement impossible d'une session déjà connectée dans l'inventaire (erreur sur un client)."""


class InventoryPortals:
    """Comptes portail d'un inventaire de campagne : le compte du cabinet + les comptes des chargés de mission déclarés.

    Option B (14/10/2026) : au début de l'inventaire, la liste FRAÎCHE des entreprises accessibles par chaque compte est
    récupérée (pages de 500) ; chaque client est ensuite interrogé directement sur le bon compte, et un client absent de
    toutes les listes complètes est classé « aucun compte habilité » sans aucune requête. Sans liste (petite campagne,
    liste indisponible ou incomplète), on revient à l'interrogation directe : cabinet puis chargé de mission.
    Sessions propres à l'inventaire (profils « <code>-campagne ») : jamais partagées avec celles du worker.
    """

    def __init__(self, cabinet: Cabinet, collabs: dict[str, PortalAccount], on_disabled=None):
        self.cabinet, self.collabs, self.on_disabled = cabinet, collabs, on_disabled
        self.accounts = {cabinet.code: ("cabinet", cabinet.libelle, cabinet.portal_login, cabinet.portal_password)}
        for a in collabs.values():
            self.accounts[a.code] = ("collab", a.nom or a.email, a.login, a.password)
        self.sessions: dict[str, PortailPro] = {}
        self.unusable: dict[str, str] = {}        # compte écarté pour tout l'inventaire → raison
        self.lists: dict[str, set[str]] = {}      # compte → SIREN accessibles
        self.complete: dict[str, bool] = {}       # compte → liste complète ?
        self.list_errors: dict[str, str] = {}     # compte → pourquoi sa liste n'a pas pu être lue
        self.listing = False                      # listes demandées (grande campagne)
        self.errors: dict[str, int] = {}          # compte → erreurs consécutives
        self.requests = 0                         # interrogations list_avis faites pendant l'inventaire
        self.opened: set[str] = set()             # comptes connectés au moins une fois dans cet inventaire

    def label(self, code: str) -> str:
        return self.accounts[code][1]

    def cdm_account(self, cdm_email: str | None, cdm_nom: str | None = None) -> PortalAccount | None:
        return accounts.find_account(self.collabs, cdm_email, cdm_nom)

    def absent_from_list(self, code: str, siren: str) -> bool:
        """Le compte n'a pas accès au client d'après sa liste complète (conclusion fiable sans requête)."""
        return self.complete.get(code, False) and siren not in self.lists.get(code, ())

    def record_ok(self, code: str) -> None:
        self.errors.pop(code, None)

    def record_error(self, code: str) -> int:
        self.errors[code] = self.errors.get(code, 0) + 1
        return self.errors[code]

    def unverified(self, siren: str, cdm_email: str | None, answered: set[str],
                   cdm_nom: str | None = None) -> list[tuple[str, str]]:
        """(compte, raison) des comptes qui pourraient avoir accès au client sans l'avoir exclu (ni réponse « non
        habilité », ni liste complète sans ce SIREN) : compte écarté dont la liste contient le SIREN, cabinet / chargé de
        mission écarté et — en mode listes — tout compte dont la liste n'a pas pu être lue en entier."""
        cdm = self.cdm_account(cdm_email, cdm_nom)
        relevant = {self.cabinet.code} | ({cdm.code} if cdm else set())
        out = []
        for code in self.accounts:
            if code in answered or self.absent_from_list(code, siren):
                continue
            if code in self.unusable and (self.listing or code in relevant or siren in self.lists.get(code, ())):
                out.append((code, self.unusable[code]))
            elif self.listing and code not in self.unusable:   # compte utilisable mais sans liste probante
                out.append((code, "liste illisible" if code in self.list_errors
                            else "liste incomplète" if code in self.lists else "liste non lue"))
        return out

    def disable(self, code: str, e: Exception | None, reason: str | None = None) -> None:
        if code in self.unusable:
            return
        self.unusable[code] = reason or ("identifiants refusés" if isinstance(e, PortailLoginRefused)
                                         else f"connexion impossible ({e})"[:200])
        self.errors.pop(code, None)
        p = self.sessions.pop(code, None)
        if p is not None:
            try:
                p.close()
            except Exception:
                pass
        if self.on_disabled:
            self.on_disabled(self.label(code), self.unusable[code])

    def session(self, code: str) -> PortailPro | None:
        """Session du compte, ouverte au premier besoin ; None si écarté. Première ouverture en échec : le compte du
        CABINET interrompt l'inventaire (comportement historique), celui d'un chargé de mission est écarté. Réouverture
        (session refermée par la limite de navigateurs) en échec passager : SessionReopenError, traitée comme une erreur
        sur CE client (identifiants refusés ou connexion abandonnée : comme à la première ouverture)."""
        if code in self.unusable:
            return None
        if code in self.sessions:
            self.sessions[code] = self.sessions.pop(code)   # ordre = du moins au plus récemment utilisé
            return self.sessions[code]
        while len(self.sessions) >= max(1, config.PORTAIL_MAX_SESSIONS):   # navigateurs ouverts limités : on referme le
            oldest = next((c for c in self.sessions if c != self.cabinet.code), None)   # moins récent, jamais le cabinet
            if oldest is None:
                break
            try:
                self.sessions.pop(oldest).close()   # son profil garde la connexion
            except Exception:
                pass
        kind, _, login, password = self.accounts[code]
        reopening = code in self.opened
        p = None
        try:
            p = PortailPro(config.PROFILES_DIR / f"{code}-campagne", headless=config.PORTAIL_HEADLESS,
                           channel=config.BROWSER_CHANNEL, login=login, password=password)
            p.block_habilitation_calls()   # le site ne lance pas ses propres appels « liste » (pages de 20)
            p.ensure_session()
        except Exception as e:
            if p is not None:
                try:
                    p.close()
                except Exception:
                    pass
            if reopening and not isinstance(e, LOGIN_STOPPED):
                raise SessionReopenError(f"réouverture de la session impossible ({type(e).__name__}: {str(e)[:200]})") from None
            if kind == "cabinet":
                raise
            self.disable(code, e)
            return None
        self.opened.add(code)
        self.sessions[code] = p
        return p

    def fetch_lists(self, should_stop=None) -> None:
        """Liste fraîche des entreprises de chaque compte (cabinet d'abord). should_stop() : campagne annulée → arrêt."""
        self.listing = True
        for code, (kind, label, _, _) in self.accounts.items():
            if code in self.lists or code in self.list_errors or code in self.unusable:
                continue   # déjà traité (reprise après une pause)
            if should_stop and should_stop():
                return
            try:
                p = self.session(code)
                if p is None:
                    continue
                sirens, complete = p.list_habilitations(page_size=500, pause_s=config.CAMPAIGN_LIST_PAGE_INTERVAL_S,
                                                        max_pages=config.CAMPAIGN_LIST_MAX_PAGES, should_stop=should_stop)
            except PortailMaintenanceError:
                raise InventoryPause(time.time() + MAINTENANCE_PAUSE_S, "portail impots en maintenance") from None
            except LOGIN_STOPPED as e:
                if kind == "cabinet":
                    if isinstance(e, PortailLoginAbandoned):
                        raise InventoryPause(e.until or time.time() + 3600,
                                             "connexion du compte du cabinet suspendue après des échecs répétés") from None
                    raise
                self.disable(code, e)
                continue
            except Exception as e:   # pas de liste : ce compte sera interrogé directement quand il est pertinent
                log.warning("Liste des entreprises indisponible pour %s : %s — interrogation directe", label, e)
                self.list_errors[code] = str(e)[:200]
                continue
            if kind == "cabinet" and not sirens:   # des milliers d'entreprises d'habitude : une liste vide est un incident
                log.warning("Liste des entreprises du cabinet vide : ignorée, interrogation directe")
                self.list_errors[code] = "liste vide"
                continue
            self.lists[code], self.complete[code] = sirens, complete

    def lists_summary(self) -> str:
        parts = []
        for code in self.accounts:
            if code in self.lists:
                parts.append(f"{self.label(code)} {len(self.lists[code])}" + ("" if self.complete.get(code) else " (incomplète)"))
            elif code in self.unusable:
                parts.append(f"{self.label(code)} écarté")
            elif code in self.list_errors:
                parts.append(f"{self.label(code)} liste indisponible")
        return "listes : " + (" · ".join(parts) or "aucune")

    def plan(self, siren: str, cdm_email: str | None, cdm_nom: str | None = None) -> list[str]:
        """Comptes à interroger pour ce client, dans l'ordre : ceux dont la liste contient le SIREN (cabinet, chargé de
        mission, puis autres comptes déclarés), puis — sans liste fiable — le cabinet et le compte du chargé de mission.
        Liste vide = aucun compte n'a accès à ce client (conclusion sans requête)."""
        cdm = self.cdm_account(cdm_email, cdm_nom)
        order = [self.cabinet.code] + ([cdm.code] if cdm else []) + \
                [a.code for a in self.collabs.values() if cdm is None or a.code != cdm.code]
        usable = [c for c in order if c not in self.unusable]
        listed = [c for c in usable if siren in self.lists.get(c, ())]
        relevant = [self.cabinet.code] + ([cdm.code] if cdm else [])
        blind = [c for c in relevant if c in usable and c not in listed and not self.complete.get(c, False)]
        return listed + blind

    def drop_session(self, code: str) -> None:
        p = self.sessions.pop(code, None)
        if p is not None:
            try:
                p.close()
            except Exception:
                pass

    def close(self) -> None:
        for p in self.sessions.values():
            try:
                p.close()
            except Exception:
                pass
        self.sessions.clear()
REQ_LABELS = {"SENT": "Envoyé", "DRY_RUN": "Mode test", "NO_DOCUMENT": "Aucun document", "FAILED": "Échec",
              "NOT_HABILITE": "Non habilité sur le portail", "PENDING": "En attente", "PROCESSING": "En cours"}


class CampaignManager:
    def __init__(self, slack_client, sf, cabinets: dict[str, Cabinet], work_queue: queue.Queue, collabs_provider=None,
                 accounts_unknown=None):
        self.slack, self.sf, self.cabinets, self.queue = slack_client, sf, cabinets, work_queue
        # comptes des chargés de mission, relus (Google Sheet) au début de chaque inventaire ; « inconnus » tant que
        # la source n'a jamais pu être lue (≠ aucun compte : l'inventaire est alors reporté)
        self.collabs_provider = collabs_provider or (lambda: accounts.get_registry().current(force=True))
        self.accounts_unknown = accounts_unknown or (lambda: accounts.get_registry().unknown)
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------ droits
    @staticmethod
    def is_admin(user_id: str) -> bool:
        return user_id in config.CAMPAIGN_ADMINS

    def busy(self) -> dict | None:
        """La campagne en cours d'inventaire ou d'envoi, s'il y en a une (une seule à la fois)."""
        running = db.campaigns_with_status("INVENTORY", "SENDING")
        return running[0] if running else None

    # ------------------------------------------------------------ création + inventaire
    def create(self, user_id: str, user_name: str, entite: str, document_type: str, annee: str,
               source: str, sirens: list[str] | None, send_limit: int | None) -> str:
        campaign_id = db.create_campaign({
            "created_by": user_id, "created_by_name": user_name, "entite": entite, "document_type": document_type,
            "annee": annee, "source": source, "sirens": "\n".join(sirens) if sirens else None, "send_limit": send_limit,
            "slack_channel": config.CAMPAIGN_CHANNEL})
        (config.CAMPAIGNS_DIR / campaign_id).mkdir(parents=True, exist_ok=True)
        t = threading.Thread(target=self._run_inventory, args=(campaign_id,), name=f"campaign-{campaign_id}", daemon=True)
        self._threads[campaign_id] = t
        t.start()
        return campaign_id

    def _run_inventory(self, campaign_id: str) -> None:
        c = db.get_campaign(campaign_id)
        cabinet = self.cabinets[c["entite"]]
        label = documents.DOCUMENT_TYPES[c["document_type"]]["label"]
        title = f"Campagne `{campaign_id}` — {label} {c['annee']} — {cabinet.libelle}"
        db.update_campaign(campaign_id, inventory_started_at=db.now_iso())
        ts = self._post(c["slack_channel"], f":mag: {title}\nInventaire : sélection des clients dans Salesforce…")
        db.update_campaign(campaign_id, slack_ts=ts)
        portals = None
        try:
            # 1. sélection des clients
            if c["source"] == "sirens":
                sf_accounts = self.sf.get_accounts_by_sirens([x for x in (c["sirens"] or "").split() if x])
                sf_accounts = [a for a in sf_accounts if a["entite"] == c["entite"]]
            else:
                sf_accounts = self.sf.list_accounts_for_campaign(c["entite"], config.CAMPAIGN_SF_FILTER)
            seen, items = set(), []
            for a in sf_accounts:
                if a["id"] in seen or not a["siren"]:
                    continue
                seen.add(a["id"])
                items.append({"sf_account_id": a["id"], "client_name": a["name"], "siren": a["siren"], "entite": a["entite"],
                              "client_email": a["email"], "client_civilite": a["civilite"], "client_nom": a["nom"],
                              "client_prenom": a["prenom"], "cdm_nom": a["cdm_nom"], "cdm_email": a["cdm_email"],
                              "cdm_telephone": a["cdm_telephone"]})
            db.add_campaign_items(campaign_id, items)
            total = len(items)
            self._update(c["slack_channel"], ts, f":mag: {title}\nInventaire : {total} clients sélectionnés, interrogation du portail en cours…")

            # 2. comptes portail : cabinet + chargés de mission déclarés ; listes fraîches si la campagne est assez grande
            collabs = self.collabs_provider()
            if self.accounts_unknown():
                raise RuntimeError("Google Sheet des accès illisible : comptes portail des chargés de mission inconnus — "
                                   "inventaire reporté, relancez-le quand le Sheet est de nouveau lisible")
            portals = InventoryPortals(cabinet, collabs, on_disabled=lambda label, reason: self._post(
                c["slack_channel"], f":warning: Campagne `{campaign_id}` : compte portail de *{label}* écarté pour cet "
                                    f"inventaire ({reason}). Les clients non trouvés ailleurs et que ce compte pourrait "
                                    f"voir seront classés « {ITEM_LABELS['COMPTE_INDISPONIBLE'].lower()} »."))
            cancelled = lambda: (db.get_campaign(campaign_id) or {}).get("status") == "CANCELLED"   # noqa: E731
            portals.session(cabinet.code)   # connexion du cabinet : un échec interrompt l'inventaire
            paused = 0.0                     # pauses cumulées (maintenance du portail, connexion du cabinet suspendue)
            if total >= config.CAMPAIGN_LIST_MIN_CLIENTS:
                self._update(c["slack_channel"], ts, f":mag: {title}\nInventaire : {total} clients sélectionnés, "
                                                      f"récupération des listes d'entreprises de chaque compte portail…")
                while True:   # pause (maintenance, connexion du cabinet suspendue) puis reprise là où on en était
                    try:
                        portals.fetch_lists(should_stop=cancelled)
                        break
                    except InventoryPause as pz:
                        paused = self._pause(c, ts, title, pz, paused, cancelled)
                mode = portals.lists_summary()
            else:
                mode = "petite campagne : interrogation directe (cabinet puis chargé de mission)"
            log.info("Campagne %s — %s", campaign_id, mode)
            self._update(c["slack_channel"], ts, f":mag: {title}\nInventaire : {total} clients — {mode}. Interrogation en cours…")

            # 3. interrogation du portail, un client à la fois, sur le bon compte — clients regroupés par compte à
            #    à interroger (clients sans requête d'abord), pour ouvrir chaque navigateur le moins souvent possible
            rank = {code: i for i, code in enumerate(portals.accounts)}
            pending = sorted(db.list_campaign_items(campaign_id, "PENDING"), key=lambda it: (
                tuple(rank[k] for k in portals.plan(it["siren"], it.get("cdm_email"), it.get("cdm_nom"))) or (-1,), it["id"]))
            done, last_update = 0, time.time()
            for it in pending:
                while True:
                    if cancelled():
                        self._update(c["slack_channel"], ts, f":no_entry_sign: {title}\nInventaire annulé après {done}/{total} clients.")
                        return
                    try:
                        status, avis_count, montant, detail, compte = self._inventory_one(portals, c, it)
                        break
                    except InventoryPause as pz:   # ni erreur comptée, ni client mal classé : pause puis même client
                        paused = self._pause(c, ts, title, pz, paused, cancelled)
                db.update_campaign_item(it["id"], status=status, avis_count=avis_count, montant_total=montant, detail=detail,
                                        portail_compte=compte if status in ("FOUND", "NO_DOCUMENT") else None)
                done += 1
                # erreurs comptées PAR COMPTE : un client classé sans requête (absent des listes) ne remet rien à zéro
                errors = portals.errors.get(compte, 0) if status == "ERROR" and compte else 0
                if compte == cabinet.code and errors >= ERRORS_STOP:
                    raise RuntimeError(f"{ERRORS_STOP} erreurs consécutives sur le portail (dernière : {detail})")
                if errors >= ERRORS_PAUSE:
                    time.sleep(60)
                if time.time() - last_update > 60:
                    last_update = time.time()
                    self._update(c["slack_channel"], ts, f":mag: {title}\nInventaire : {done}/{total} clients interrogés — "
                                                          f"{self._summary(db.campaign_item_counts(campaign_id))}")

            # 4. rapport + proposition d'envoi
            log.info("Campagne %s — inventaire terminé : %d interrogation(s) du portail pour %d clients",
                     campaign_id, portals.requests, total)
            counts = db.campaign_item_counts(campaign_id)
            db.update_campaign(campaign_id, status="INVENTORY_DONE", inventory_finished_at=db.now_iso())
            report = self.build_report(campaign_id, final=False)
            found = counts.get("FOUND", 0)
            to_send = min(found, c["send_limit"]) if c.get("send_limit") else found
            text = (f":clipboard: {title}\nInventaire terminé : {total} clients — {self._summary(counts)}.\n"
                    f"*{to_send} email(s)* partiraient si vous lancez l'envoi"
                    + (f" (limite de test : {c['send_limit']})" if c.get("send_limit") else "")
                    + (f" — *tous redirigés vers {config.TEST_RECIPIENT}*" if config.TEST_RECIPIENT else " — *aux clients*")
                    + (f", quota {config.CAMPAIGN_DAILY_LIMIT}/jour, soit environ {max(1, -(-to_send // config.CAMPAIGN_DAILY_LIMIT))} jour(s)." if to_send else "."))
            self._update(c["slack_channel"], ts, text)
            self._upload(c["slack_channel"], report, f"Inventaire campagne {campaign_id}")
            if to_send:
                self._post(c["slack_channel"], "Rien ne partira sans votre validation.", blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"Campagne `{campaign_id}` : lancer l'envoi de *{to_send} email(s)* ?"}},
                    {"type": "actions", "elements": [
                        {"type": "button", "action_id": "campaign_send", "style": "primary", "value": campaign_id,
                         "text": {"type": "plain_text", "text": f"Lancer l'envoi ({to_send})"},
                         "confirm": {"title": {"type": "plain_text", "text": "Confirmer l'envoi"},
                                     "text": {"type": "mrkdwn", "text": f"{to_send} email(s) vont partir"
                                              + (f" vers {config.TEST_RECIPIENT} (mode test)." if config.TEST_RECIPIENT else " *aux clients*.")},
                                     "confirm": {"type": "plain_text", "text": "Oui, lancer"}, "deny": {"type": "plain_text", "text": "Non"}}},
                        {"type": "button", "action_id": "campaign_cancel", "value": campaign_id,
                         "text": {"type": "plain_text", "text": "Annuler la campagne"}}]}])
            else:
                db.update_campaign(campaign_id, status="DONE", message="Aucun envoi à faire")
        except Exception as e:
            log.exception("Inventaire campagne %s", campaign_id)
            db.update_campaign(campaign_id, status="FAILED", message=f"Inventaire interrompu : {e}")
            self._update(c["slack_channel"], ts, f":x: {title}\nInventaire interrompu : `{e}`")
        finally:
            if portals is not None:
                portals.close()

    def _pause(self, c: dict, ts: str | None, title: str, pz: InventoryPause, paused: float, cancelled) -> float:
        """Attend la fin de la pause (annulation vérifiée toutes les 30 s) ; renvoie le cumul des pauses. Au-delà de
        MAX_PAUSE_S cumulées, l'inventaire s'arrête (RuntimeError)."""
        wait = max(30.0, pz.until - time.time())
        if paused + wait > MAX_PAUSE_S:
            raise RuntimeError(f"inventaire en pause depuis trop longtemps ({pz.reason}) — à relancer plus tard")
        log.warning("Campagne %s : pause de %d min (%s)", c["campaign_id"], wait // 60, pz.reason)
        self._update(c["slack_channel"], ts, f":double_vertical_bar: {title}\nInventaire en pause : {pz.reason} — "
                                              f"reprise vers {datetime.fromtimestamp(time.time() + wait):%H:%M}.")
        end = time.time() + wait
        while time.time() < end and not cancelled():
            time.sleep(max(0.0, min(30.0, end - time.time())))
        return paused + wait

    def _inventory_one(self, portals: InventoryPortals, c: dict, it: dict) -> tuple[str, int | None, float | None, str, str | None]:
        """(statut, nb avis, montant, détail, compte portail où le document a été trouvé)."""
        if not it["client_email"] or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", it["client_email"]):
            return "NO_EMAIL", None, None, "Email client vide ou invalide dans Salesforce", None
        prev = db.already_sent(it["sf_account_id"], c["document_type"], c["annee"])
        if prev:
            return "ALREADY_SENT", None, None, f"Envoyé le {(prev['sent_at'] or '')[:10]} par {prev['slack_user_name'] or '?'}", None
        siren = it["siren"]
        answered: list[str] = []   # comptes interrogés qui ont répondu « non habilité »
        for code in portals.plan(siren, it.get("cdm_email"), it.get("cdm_nom")):
            label = portals.label(code)
            try:
                p = portals.session(code)   # réouverture en échec passager → erreur sur ce client (SessionReopenError)
                if p is None:   # compte écarté (entre-temps, ou connexion impossible à la première ouverture)
                    continue
                time.sleep(config.CAMPAIGN_INVENTORY_INTERVAL_S)   # politesse avant CHAQUE requête (aucune si client classé d'après les listes)
                portals.requests += 1
                avis = [a for a in p.list_avis(siren) if a.is_cfe and a.annee == c["annee"]]
            except PortailMaintenanceError:   # tout le portail est concerné : pause, puis ce même client
                raise InventoryPause(time.time() + MAINTENANCE_PAUSE_S, "portail impots en maintenance") from None
            except LOGIN_STOPPED as e:
                if code == portals.cabinet.code:
                    if isinstance(e, PortailLoginAbandoned):   # suspension temporaire : pause jusqu'à la reprise
                        raise InventoryPause(e.until or time.time() + 3600,
                                             "connexion du compte du cabinet suspendue après des échecs répétés") from None
                    raise   # mot de passe du cabinet refusé : arrêt de l'inventaire, jamais resoumis client après client
                portals.disable(code, e)
                continue
            except NonHabiliteError:   # ce compte n'a pas accès au client : pas une erreur technique
                portals.record_ok(code)
                answered.append(code)
                continue
            except Exception as e:
                if not isinstance(e, (PortailProError, httpx.HTTPError)):
                    portals.drop_session(code)   # erreur du navigateur : session rouverte proprement au prochain besoin
                n = portals.record_error(code)
                msg = f"{label} : {e}" if isinstance(e, PortailProError) else f"{label} : {type(e).__name__}: {e}"
                if code != portals.cabinet.code and n >= ERRORS_STOP:
                    portals.disable(code, e, f"{n} erreurs consécutives, dernière : {e}"[:200])
                return "ERROR", None, None, msg[:500], code
            portals.record_ok(code)
            via = "" if code == portals.cabinet.code else f" — via le compte de {label}"
            if not avis:
                return "NO_DOCUMENT", 0, None, f"Aucun avis {c['annee']} sur le portail{via}", code
            montant = sum(float(a.montant) for a in avis if a.montant is not None)
            return "FOUND", len(avis), montant, "; ".join(f"SIRET {a.siret} {a.montant} €" for a in avis) + via, code

        # Personne n'a fourni l'avis : « aucun compte habilité » seulement si chaque compte qui pourrait avoir accès l'a exclu.
        names = lambda codes: ", ".join(portals.label(k) for k in codes)   # noqa: E731
        no = f" — non habilité(s) : {names(answered)}" if answered else ""
        unverified = portals.unverified(siren, it.get("cdm_email"), set(answered), it.get("cdm_nom"))
        if unverified:
            return ("COMPTE_INDISPONIBLE", None, None,
                    ("Client non vérifié sur : " + "; ".join(f"{portals.label(k)} ({why})" for k, why in unverified)
                     + no)[:500], None)
        why = []
        if answered:
            why.append("comptes interrogés, non habilités : " + names(answered))
        absent = [k for k in portals.accounts if k not in answered and portals.absent_from_list(k, siren)]
        if absent:
            why.append("absent des listes de : " + names(absent))
        cdm = portals.cdm_account(it.get("cdm_email"), it.get("cdm_nom"))
        text = "Aucun compte habilité" + (" (" + " ; ".join(why) + ")" if why else "")
        if cdm is None and it.get("cdm_nom"):
            text += f" — {it['cdm_nom']} n'a pas de compte portail déclaré"
        return "NOT_HABILITE", None, None, text[:500], None

    # ------------------------------------------------------------ envoi
    def start_send(self, campaign_id: str, user_id: str) -> str | None:
        """Renvoie un message d'erreur, ou None si l'envoi démarre."""
        with self._lock:
            c = db.get_campaign(campaign_id)
            if not c:
                return "Campagne inconnue."
            if c["status"] != "INVENTORY_DONE":
                return f"Cette campagne est en statut {c['status']}, l'envoi ne peut pas démarrer."
            other = self.busy()
            if other:
                return f"La campagne `{other['campaign_id']}` est déjà en cours ({other['status']}). Une seule à la fois."
            db.update_campaign(campaign_id, status="SENDING", send_started_at=db.now_iso(), send_by=user_id)
        t = threading.Thread(target=self._run_send, args=(campaign_id,), name=f"campaign-send-{campaign_id}", daemon=True)
        self._threads[campaign_id] = t
        t.start()
        return None

    def resume_interrupted(self) -> None:
        """Au démarrage : les inventaires interrompus sont marqués en échec, les envois interrompus reprennent."""
        for c in db.campaigns_with_status("INVENTORY"):
            db.update_campaign(c["campaign_id"], status="FAILED", message="Inventaire interrompu par un redémarrage du service")
            self._post(c["slack_channel"], f":x: Campagne `{c['campaign_id']}` : inventaire interrompu par un redémarrage. Relancez-la.")
        for c in db.campaigns_with_status("SENDING"):
            log.info("Reprise de l'envoi de la campagne %s", c["campaign_id"])
            t = threading.Thread(target=self._run_send, args=(c["campaign_id"],), name=f"campaign-send-{c['campaign_id']}", daemon=True)
            self._threads[c["campaign_id"]] = t
            t.start()

    def _run_send(self, campaign_id: str) -> None:
        c = db.get_campaign(campaign_id)
        cabinet = self.cabinets[c["entite"]]
        label = documents.DOCUMENT_TYPES[c["document_type"]]["label"]
        title = f"Campagne `{campaign_id}` — {label} {c['annee']} — {cabinet.libelle}"
        ts = self._post(c["slack_channel"], f":rocket: {title}\nEnvoi démarré.")
        try:
            items = [it for it in db.list_campaign_items(campaign_id, "FOUND") if not it["request_id"]]
            already = sum(1 for it in db.list_campaign_items(campaign_id, "FOUND") if it["request_id"])
            if c.get("send_limit"):
                items = items[:max(0, c["send_limit"] - already)]
            total = already + len(items)
            last_update = 0.0
            for it in items:
                if (db.get_campaign(campaign_id) or {}).get("status") == "CANCELLED":
                    break
                # quota Gmail quotidien (envois réels uniquement)
                while not config.DRY_RUN and db.sent_today_count() >= config.CAMPAIGN_DAILY_LIMIT:
                    resume = (datetime.now() + timedelta(days=1)).replace(hour=config.CAMPAIGN_RESUME_HOUR, minute=0, second=0)
                    self._update(c["slack_channel"], ts, f":rocket: {title}\nQuota quotidien atteint ({config.CAMPAIGN_DAILY_LIMIT} emails). "
                                                          f"Reprise automatique le {resume:%d/%m à %Hh%M}. {self._progress(campaign_id, total)}")
                    time.sleep(min(600, max(30, (resume - datetime.now()).total_seconds())))
                    if (db.get_campaign(campaign_id) or {}).get("status") == "CANCELLED":
                        break
                # comptes des chargés de mission inconnus (Sheet jamais lu depuis le démarrage) : pause plutôt que des
                # demandes vouées à l'échec « vérification impossible »
                while self.accounts_unknown() and (db.get_campaign(campaign_id) or {}).get("status") != "CANCELLED":
                    self._update(c["slack_channel"], ts, f":rocket: {title}\nEn pause : Google Sheet des accès illisible, "
                                                          f"comptes des chargés de mission inconnus. {self._progress(campaign_id, total)}")
                    time.sleep(60)
                # connexion du cabinet suspendue (échecs répétés) : pause plutôt que des demandes vouées à l'échec
                while (until := login_blocked_until(cabinet.portal_login, cabinet.portal_password)) \
                        and (db.get_campaign(campaign_id) or {}).get("status") != "CANCELLED":
                    self._update(c["slack_channel"], ts, f":rocket: {title}\nEn pause : connexion du compte portail du cabinet "
                                                          f"suspendue jusqu'à {datetime.fromtimestamp(until):%H:%M} après des "
                                                          f"échecs répétés (code de vérification ?). {self._progress(campaign_id, total)}")
                    time.sleep(min(300, max(30, until - time.time())))
                if (db.get_campaign(campaign_id) or {}).get("status") == "CANCELLED":
                    break
                while self.queue.qsize() > 3:   # ne pas noyer les demandes unitaires
                    time.sleep(2)
                request_id = db.create_request({
                    "slack_user_id": c["created_by"], "slack_user_name": c["created_by_name"], "campaign_id": campaign_id,
                    "sf_account_id": it["sf_account_id"], "client_name": it["client_name"], "siren": it["siren"], "entite": it["entite"],
                    "client_email": it["client_email"], "client_civilite": it["client_civilite"], "client_nom": it["client_nom"],
                    "client_prenom": it["client_prenom"], "cdm_nom": it["cdm_nom"], "cdm_email": it["cdm_email"],
                    "cdm_telephone": it["cdm_telephone"], "document_type": c["document_type"], "annee": c["annee"], "periode": None,
                    "dry_run": 1 if config.DRY_RUN else 0,
                    "portail_compte": it.get("portail_compte")})   # compte où l'inventaire a trouvé l'avis : essayé en premier
                db.update_campaign_item(it["id"], request_id=request_id)
                self.queue.put(request_id)
                if time.time() - last_update > 60:
                    last_update = time.time()
                    self._update(c["slack_channel"], ts, f":rocket: {title}\n{self._progress(campaign_id, total)}")
                time.sleep(config.CAMPAIGN_SEND_INTERVAL_S)

            # attendre la fin du traitement des demandes en file
            while True:
                counts = db.campaign_request_counts(campaign_id)
                if not (counts.get("PENDING", 0) + counts.get("PROCESSING", 0)):
                    break
                time.sleep(10)
            cancelled = (db.get_campaign(campaign_id) or {}).get("status") == "CANCELLED"
            db.update_campaign(campaign_id, status="CANCELLED" if cancelled else "DONE", send_finished_at=db.now_iso())
            report = self.build_report(campaign_id, final=True)
            counts = db.campaign_request_counts(campaign_id)
            self._update(c["slack_channel"], ts, f"{':no_entry_sign:' if cancelled else ':checkered_flag:'} {title}\n"
                                                  f"Envoi {'annulé' if cancelled else 'terminé'} : "
                                                  + ", ".join(f"{n} {REQ_LABELS.get(s, s).lower()}" for s, n in sorted(counts.items())) + ".")
            self._upload(c["slack_channel"], report, f"Rapport final campagne {campaign_id}")
        except Exception as e:
            log.exception("Envoi campagne %s", campaign_id)
            db.update_campaign(campaign_id, status="FAILED", message=f"Envoi interrompu : {e}")
            self._update(c["slack_channel"], ts, f":x: {title}\nEnvoi interrompu : `{e}`")

    def cancel(self, campaign_id: str) -> str | None:
        c = db.get_campaign(campaign_id)
        if not c:
            return "Campagne inconnue."
        if c["status"] not in ("INVENTORY", "INVENTORY_DONE", "SENDING"):
            return f"Campagne déjà en statut {c['status']}."
        db.update_campaign(campaign_id, status="CANCELLED", message="Annulée")
        return None

    # ------------------------------------------------------------ rapports
    def build_report(self, campaign_id: str, final: bool) -> Path:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        # cas de paiement lu dans l'avis → libellé lisible pour le rapport
        CAS_LABELS = {"RIEN_A_PAYER": "Rien à payer", "A_PAYER": "À payer (sans prélèvement)",
                      "PRELEVEMENT_ECHEANCE": "Prélèvement à l'échéance", "MENSUALISE": "Mensualisé",
                      "INDETERMINE": "À vérifier"}
        c = db.get_campaign(campaign_id)
        items = db.list_campaign_items(campaign_id)
        reqs = db.campaign_requests(campaign_id) if final else {}
        wb = Workbook()
        ws = wb.active
        ws.title = "Rapport final" if final else "Inventaire"
        headers = ["Client", "SIREN", "Cabinet", "Email client", "Chargé de mission", "Résultat inventaire", "Nb avis", "Montant total €", "Détail"]
        if final:
            headers += ["Cas de paiement", "Action CDM", "Statut envoi", "Envoyé le", "Envoyé à", "Fichiers", "Erreur"]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor="1F3A68")
        for it in items:
            row = [it["client_name"], it["siren"], it["entite"], it["client_email"], it["cdm_nom"], ITEM_LABELS.get(it["status"], it["status"]),
                   it["avis_count"], it["montant_total"], it["detail"]]
            if final:
                r = reqs.get(it["request_id"] or "")
                if r:
                    docs = db.list_documents(r["request_id"])
                    files = ", ".join(Path(d["file_path"]).name for d in docs if d["status"] == "DOWNLOADED" and d["file_path"])
                    cas = CAS_LABELS.get(r["cas_paiement"], r["cas_paiement"] or "")
                    action = "Oui" if r["action_cdm"] else ("Non" if r["cas_paiement"] else "")
                    row += [cas, action, REQ_LABELS.get(r["status"], r["status"]), (r["sent_at"] or "")[:16].replace("T", " "),
                            (config.TEST_RECIPIENT or r["client_email"]) if r["status"] == "SENT" else "", files, r["error_message"]]
                else:
                    row += ["", "", "Non envoyé" if it["status"] == "FOUND" else "", "", "", "", ""]
            ws.append(row)
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(12, max((len(str(ws.cell(row=r, column=i).value or "")) for r in range(1, ws.max_row + 1)), default=10) + 2))
        ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
        # feuille de synthèse
        s = wb.create_sheet("Synthèse")
        s.append(["Campagne", campaign_id]); s.append(["Cabinet", c["entite"]]); s.append(["Document", c["document_type"]]); s.append(["Année", c["annee"]])
        s.append(["Créée par", c["created_by_name"] or c["created_by"]]); s.append(["Créée le", c["created_at"]]); s.append(["Statut", c["status"]])
        s.append([]); s.append(["Inventaire", "Nombre"])
        for k, n in sorted(db.campaign_item_counts(campaign_id).items()):
            s.append([ITEM_LABELS.get(k, k), n])
        if final:
            s.append([]); s.append(["Envoi", "Nombre"])
            for k, n in sorted(db.campaign_request_counts(campaign_id).items()):
                s.append([REQ_LABELS.get(k, k), n])
            if config.TEST_RECIPIENT:
                s.append([]); s.append(["Mode test", f"tous les emails redirigés vers {config.TEST_RECIPIENT}"])
        s.column_dimensions["A"].width = 28; s.column_dimensions["B"].width = 50
        path = config.CAMPAIGNS_DIR / campaign_id / (f"rapport_final_{campaign_id}.xlsx" if final else f"inventaire_{campaign_id}.xlsx")
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return path

    # ------------------------------------------------------------ Slack
    @staticmethod
    def _summary(counts: dict) -> str:
        return ", ".join(f"{n} {ITEM_LABELS.get(k, k).lower()}" for k, n in sorted(counts.items(), key=lambda x: -x[1]) if k != "PENDING") or "rien encore"

    def _progress(self, campaign_id: str, total: int) -> str:
        counts = db.campaign_request_counts(campaign_id)
        finished = sum(n for s, n in counts.items() if s not in ("PENDING", "PROCESSING"))
        return f"{finished}/{total} traités — " + ", ".join(f"{n} {REQ_LABELS.get(s, s).lower()}" for s, n in sorted(counts.items()))

    def _post(self, channel: str | None, text: str, blocks=None) -> str | None:
        if not channel:
            return None
        try:
            return self.slack.chat_postMessage(channel=channel, text=text, blocks=blocks)["ts"]
        except Exception as e:
            log.warning("Message Slack campagne impossible : %s", e)
            return None

    def _update(self, channel: str | None, ts: str | None, text: str) -> None:
        if not channel:
            return
        try:
            if ts:
                self.slack.chat_update(channel=channel, ts=ts, text=text)
            else:
                self.slack.chat_postMessage(channel=channel, text=text)
        except Exception as e:
            log.warning("Mise à jour Slack campagne impossible : %s", e)

    def _upload(self, channel: str | None, path: Path, title: str) -> None:
        if not channel:
            return
        try:
            self.slack.files_upload_v2(channel=channel, file=str(path), filename=path.name, title=title)
        except Exception as e:
            log.warning("Dépôt du rapport Slack impossible : %s (fichier : %s)", e, path)
