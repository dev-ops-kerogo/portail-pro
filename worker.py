"""Worker : traite les demandes une par une, dans un thread dédié (Playwright impose un seul thread).

Pour chaque demande : session portailpro → PDF → email Gmail → archivage Salesforce → notification Slack.
Comptes portail essayés dans l'ordre : celui du cabinet, puis le compte personnel du chargé de mission du client
s'il est déclaré dans .env (PORTAIL_COLLAB<n>_*). Un compte « non habilité » pour le client passe la main au suivant ;
si aucun n'est habilité, la demande passe en NOT_HABILITE (ce n'est pas « aucun document »).
"""
from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
from pathlib import Path

import config
import db
import documents
from config import Cabinet, PortalAccount
from mailer import Mailer
from portailpro import NonHabiliteError, PortailLoginAbandoned, PortailLoginRefused, PortailPro, PortailProError
from salesforce_client import SalesforceClient

log = logging.getLogger("worker")


class Worker(threading.Thread):
    def __init__(self, work_queue: queue.Queue, slack_client, cabinets: dict[str, Cabinet],
                 sf: SalesforceClient, mailer: Mailer, collabs: dict[str, PortalAccount] | None = None):
        super().__init__(name="worker", daemon=True)
        self.queue, self.slack, self.cabinets, self.sf, self.mailer = work_queue, slack_client, cabinets, sf, mailer
        self.collabs = config.load_collab_accounts() if collabs is None else collabs
        self.portails: dict[str, PortailPro] = {}
        self.unusable: dict[str, tuple[str, float]] = {}   # compte écarté → (raison, jusqu'à quand)
        self._last_housekeeping = 0.0

    # ------------------------------------------------------------ boucle
    def run(self) -> None:
        log.info("Worker démarré — comptes portail des chargés de mission : %s",
                 ", ".join(a.nom or a.email for a in self.collabs.values()) or "aucun")
        while True:
            try:
                request_id = self.queue.get(timeout=30)
            except queue.Empty:
                self.housekeeping()
                continue
            try:
                self.process(request_id)
            except Exception:  # ne jamais tuer le thread
                log.exception("Erreur non gérée sur %s", request_id)
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------ portail
    def _session(self, code: str, login: str | None, password: str | None) -> PortailPro:
        """Session portail d'un compte (profil Chrome = code), créée au premier besoin puis réutilisée."""
        p = self.portails.get(code)
        if p is None:
            p = PortailPro(config.PROFILES_DIR / code, headless=config.PORTAIL_HEADLESS,
                           channel=config.BROWSER_CHANNEL, login=login, password=password)
            self.portails[code] = p
        try:
            p.ensure_session()
        except Exception:
            # Session en échec (page morte, délai dépassé, identifiants…) : jetée, recréée proprement au prochain essai.
            self.portails.pop(code, None)
            try:
                p.close()
            except Exception:
                pass
            raise
        return p

    def portail_for(self, cabinet: Cabinet) -> PortailPro:
        return self._session(cabinet.code, cabinet.portal_login, cabinet.portal_password)

    def portail_for_collab(self, acc: PortalAccount) -> PortailPro:
        return self._session(acc.code, acc.login, acc.password)

    def _accounts_for(self, req: dict, cabinet: Cabinet) -> list[tuple]:
        """Comptes à essayer, dans l'ordre : (genre, libellé, code, ouverture de session).
        Cabinet puis chargé de mission ; pour une demande de campagne, le compte sur lequel l'inventaire a trouvé l'avis
        (colonne portail_compte, éventuellement celui d'un autre collaborateur) est essayé en premier."""
        acc = self.collabs.get((req.get("cdm_email") or "").strip().lower())
        accounts = [("cabinet", cabinet.libelle, cabinet.code, lambda: self.portail_for(cabinet))]
        if acc:
            accounts.append(("collab", acc.nom or req.get("cdm_nom") or acc.email, acc.code,
                             lambda: self.portail_for_collab(acc)))
        hinted = next((a for a in self.collabs.values() if a.code == req.get("portail_compte")), None)
        if hinted:
            label = (acc.nom or req.get("cdm_nom") or acc.email) if hinted is acc else (hinted.nom or hinted.email)
            accounts = [("collab", label, hinted.code, lambda: self.portail_for_collab(hinted))] + \
                       [a for a in accounts if a[2] != hinted.code]
        return accounts

    def _unusable(self, code: str) -> str | None:
        entry = self.unusable.get(code)
        if entry and entry[1] > time.time():
            return entry[0]
        self.unusable.pop(code, None)
        return None

    def _disable(self, code: str, label: str, e: Exception) -> str:
        """Écarte un compte dont la CONNEXION échoue (jamais pour une erreur de récupération) : jusqu'au redémarrage si le
        mot de passe est refusé (ne jamais risquer de bloquer le compte), sinon jusqu'à la fin de la suspension des
        connexions (PortailLoginAbandoned) ou 1 h (code de vérification demandé, portail indisponible…). Alerte une fois."""
        refused = isinstance(e, PortailLoginRefused)
        reason = "identifiants refusés" if refused else f"connexion impossible ({e})"[:200]
        first = code not in self.unusable
        self.unusable[code] = (reason, float("inf") if refused else (getattr(e, "until", None) or time.time() + 3600))
        stale = self.portails.pop(code, None)   # session recréée proprement au prochain essai
        if stale is not None:
            try:
                stale.close()
            except Exception:
                pass
        if first:
            self._alert(f":rotating_light: Compte portail de *{label}* écarté "
                        + ("jusqu'au prochain redémarrage du service" if refused
                           else f"jusqu'à {time.strftime('%H:%M', time.localtime(self.unusable[code][1]))}")
                        + f" : {reason}. "
                        + ("Corriger ses identifiants dans .env." if refused
                           else "Vérifier s'il faut se connecter une fois à la main (code de vérification)."))
        return reason

    # ------------------------------------------------------------ traitement
    def process(self, request_id: str) -> None:
        req = db.get_request(request_id)
        if not req or req["status"] not in ("PENDING", "PROCESSING"):
            return
        db.update_request(request_id, status="PROCESSING")
        db.add_event(request_id, "PROCESSING")
        cabinet = self.cabinets.get(req["entite"])
        if not cabinet or not cabinet.active:
            return self._fail(req, f"Cabinet « {req['entite']} » non configuré dans cabinets.json")

        req_dir = config.FILES_DIR / request_id
        req_dir.mkdir(parents=True, exist_ok=True)
        results, used, not_habilite, unavailable = None, None, [], []
        current = cabinet.libelle
        try:
            for kind, label, code, open_session in self._accounts_for(req, cabinet):
                current = label
                reason = self._unusable(code)
                if reason:
                    unavailable.append(f"{label} ({reason})")
                    continue
                # 1. Connexion : seuls ses échecs peuvent écarter un compte.
                try:
                    portail = open_session()
                except Exception as e:   # erreur portail OU du navigateur (délai dépassé, page fermée, profil verrouillé…)
                    if kind == "cabinet" and not isinstance(e, (PortailLoginRefused, PortailLoginAbandoned)):
                        raise                           # comportement historique : alerte + échec, nouvel essai plus tard
                    unavailable.append(f"{label} ({self._disable(code, label, e)})")
                    continue
                # 2. Récupération : une erreur ponctuelle (maintenance, PDF illisible…) fait échouer CETTE demande
                #    sans écarter le compte ; seule une reconnexion refusée ou abandonnée l'écarte.
                try:
                    results = documents.fetch(portail, req["document_type"], req["siren"], req["annee"], req["periode"])
                except NonHabiliteError:
                    not_habilite.append(label)          # ce compte n'a pas accès au client → compte suivant
                    continue
                except (PortailLoginRefused, PortailLoginAbandoned) as e:
                    unavailable.append(f"{label} ({self._disable(code, label, e)})")
                    continue
                used = (kind, label)
                break
        except PortailProError as e:
            account = "" if current == cabinet.libelle else f" (compte portail de {current})"
            self._alert(f":rotating_light: Portailpro ({current}) : {e} — demande `{request_id}` ({req['client_name']})")
            return self._fail(req, f"Portail impots{account} : {e}"
                              + (f" — non habilité(s) : {', '.join(not_habilite)}" if not_habilite else ""))
        except NotImplementedError as e:
            return self._fail(req, str(e))
        except Exception as e:
            log.exception("Téléchargement %s (compte %s)", request_id, current)
            self._alert(f":rotating_light: Erreur technique (compte portail de {current}) sur `{request_id}` "
                        f"({req['client_name']}) : `{type(e).__name__}: {e}`")
            return self._fail(req, f"Erreur technique lors du téléchargement (compte portail de {current}, {type(e).__name__})")

        if results is None:
            if unavailable:
                return self._fail(req, "Vérification impossible, compte(s) portail indisponible(s) : " + ", ".join(unavailable)
                                  + (f" — non habilité(s) : {', '.join(not_habilite)}" if not_habilite else ""))
            return self._not_habilite(req, not_habilite)
        db.add_event(request_id, "COMPTE_PORTAIL", f"{used[0]} : {used[1]}")
        via = used[1] if used[0] == "collab" else None

        files: list[Path] = []
        for r in results:
            path = None
            if r.status == "DOWNLOADED" and r.content:
                path = req_dir / r.filename
                path.write_bytes(r.content)
                files.append(path)
            db.add_document(request_id, r.label, r.status, str(path) if path else None, r.message)

        skipped = [r for r in results if r.status == "SKIPPED"]
        where = f" (compte portail de {via})" if via else ""
        if not files:
            db.update_request(request_id, status="NO_DOCUMENT",
                              error_message=(skipped[0].message if skipped else "Aucun document") + where)
            db.add_event(request_id, "NO_DOCUMENT")
            return self._notify(req, f":warning: *{req['client_name']}* (SIREN {req['siren']}) — {documents.DOCUMENT_TYPES[req['document_type']]['label']} "
                                     f"{req['annee']} : aucun document trouvé sur le portail{where}. *Aucun email envoyé.*")

        if config.DRY_RUN or req.get("dry_run"):
            return self._deliver_dry_run(req, cabinet, files, skipped, via)

        try:
            sent = self.mailer.send(req, cabinet, files)
        except Exception as e:
            log.exception("Envoi email %s", request_id)
            self._alert(f":rotating_light: Échec envoi Gmail sur `{request_id}` ({req['client_name']}) : `{e}`")
            return self._fail(req, f"Échec de l'envoi de l'email ({type(e).__name__})")
        sent_at = db.now_iso()
        db.update_request(request_id, status="SENT", gmail_message_id=sent.gmail_id, sent_at=sent_at)
        db.add_event(request_id, "SENT", f"gmail={sent.gmail_id} fichiers={len(files)}")

        archived = self._archive(req, files, sent, sent_at)

        lines = [f":white_check_mark: *{req['client_name']}* — {documents.DOCUMENT_TYPES[req['document_type']]['label']} {req['annee']} "
                 f"envoyé à {req['client_email']}" + (f" (redirigé vers {config.TEST_RECIPIENT})" if config.TEST_RECIPIENT else ""),
                 "• Pièces jointes : " + ", ".join(p.name for p in files)]
        if via:
            lines.append(f"• Récupéré via le compte portail de {via}")
        if skipped:
            lines.append("• Sans document : " + "; ".join(f"{r.label} ({r.message})" for r in skipped))
        if archived is not None:
            lines.append("• PDF et email enregistrés sur la fiche Salesforce" + (" (compte de test)" if config.ARCHIVE_TEST_ACCOUNT_ID else "")
                         if archived else "• :warning: Archivage Salesforce en échec (email bien envoyé)")
        self._notify(req, "\n".join(lines))

    def _deliver_dry_run(self, req: dict, cabinet: Cabinet, files: list[Path], skipped: list, via: str | None = None) -> None:
        """Mode test : rien ne part chez le client ; PDF + aperçu vont dans DRY_RUN_CHANNEL (sinon en message direct au demandeur)."""
        request_id = req["request_id"]
        db.update_request(request_id, status="DRY_RUN")
        db.add_event(request_id, "DRY_RUN", f"fichiers={len(files)}")
        if req.get("campaign_id"):   # campagne en mode test : on ne dépose pas des centaines de PDF dans Slack
            return
        preview = self.mailer.preview(req, cabinet)
        text = (f":test_tube: *Mode test* — demande de <@{req['slack_user_id']}> — *{req['client_name']}* (SIREN {req['siren']}) : "
                f"{len(files)} PDF récupéré(s), *aucun email envoyé, aucun archivage*.\n"
                + (f"• Récupéré via le compte portail de {via}\n" if via else "")
                + ("• Sans document : " + "; ".join(f"{r.label} ({r.message})" for r in skipped) + "\n" if skipped else "")
                + f"Email qui serait envoyé :\n```{preview[:2500]}```")
        try:
            channel = config.DRY_RUN_CHANNEL or self.slack.conversations_open(users=req["slack_user_id"])["channel"]["id"]
            self.slack.chat_postMessage(channel=channel, text=text)
            for p in files:
                self.slack.files_upload_v2(channel=channel, file=str(p), filename=p.name, title=p.name)
        except Exception as e:
            log.warning("Dépôt Slack (mode test) impossible : %s", e)
            self._notify(req, text + f"\n:warning: Dépôt des fichiers dans Slack impossible ({e}) — ils sont dans {config.FILES_DIR / request_id}")

    def _archive(self, req: dict, files: list[Path], sent, sent_at: str) -> bool | None:
        """Dépose les PDF sur la fiche, puis enregistre l'email envoyé dans sa chronologie d'activités (avec les PDF rattachés)."""
        if not config.ARCHIVE_TO_SALESFORCE or not req.get("sf_account_id"):
            return None
        target = config.ARCHIVE_TEST_ACCOUNT_ID or req["sf_account_id"]   # tests : dossier Salesforce dédié
        try:
            cv_ids = [self.sf.archive_pdf(target, p.name, p.read_bytes()) for p in files]
            db.add_event(req["request_id"], "ARCHIVED", f"{len(files)} fichier(s) → Account {target}"
                         + (" (compte de test)" if config.ARCHIVE_TEST_ACCOUNT_ID else ""))
            email_id = self.sf.log_email(target, sent.subject, sent.html_for_salesforce, sent.text, sent.from_address,
                                         sent.from_name, sent.to_address, sent_at, cv_ids)
            db.add_event(req["request_id"], "EMAIL_LOGGED", f"EmailMessage {email_id}")
            return True
        except Exception as e:
            log.warning("Archivage Salesforce %s : %s", req["request_id"], e)
            db.add_event(req["request_id"], "ARCHIVE_FAILED", str(e))
            return False

    # ------------------------------------------------------------ issues
    def _not_habilite(self, req: dict, accounts: list[str]) -> None:
        """Aucun des comptes essayés n'a accès au client : ce n'est pas « aucun document »."""
        names = ", ".join(accounts)
        message = f"Compte(s) portail non habilité(s) pour ce client : {names}"
        db.update_request(req["request_id"], status="NOT_HABILITE", error_message=message)
        db.add_event(req["request_id"], "NOT_HABILITE", message)
        hint = ""
        if req.get("cdm_nom") and not self.collabs.get((req.get("cdm_email") or "").strip().lower()):
            hint = f"\n• Son chargé de mission ({req['cdm_nom']}) n'a pas de compte portail déclaré dans le service."
        self._notify(req, f":no_entry: *{req['client_name']}* (SIREN {req['siren']}) — "
                          f"{documents.DOCUMENT_TYPES[req['document_type']]['label']} {req['annee']} : "
                          f"nos comptes portail n'ont pas accès à ce client ({names}) — aucun n'a reçu d'habilitation "
                          f"de sa part sur impots.gouv. *Aucun email envoyé.*{hint}")

    def _fail(self, req: dict, message: str) -> None:
        db.update_request(req["request_id"], status="FAILED", error_message=message)
        db.add_event(req["request_id"], "FAILED", message)
        self._notify(req, f":x: *{req['client_name']}* — {req['document_type']} {req['annee']} : échec.\n{message}")

    def _notify(self, req: dict, text: str) -> None:
        if req.get("campaign_id"):   # campagne : le suivi est global (message de progression + rapport), pas de DM par client
            return
        try:
            self.slack.chat_postMessage(channel=req["slack_user_id"], text=text)
        except Exception as e:
            log.warning("Notification Slack impossible pour %s : %s", req["slack_user_id"], e)

    def _alert(self, text: str) -> None:
        if not config.SLACK_ALERT_CHANNEL:
            return
        try:
            self.slack.chat_postMessage(channel=config.SLACK_ALERT_CHANNEL, text=text)
        except Exception as e:
            log.warning("Alerte Slack impossible : %s", e)

    # ------------------------------------------------------------ rétention
    def housekeeping(self) -> None:
        if time.time() - self._last_housekeeping < 3600:
            return
        self._last_housekeeping = time.time()
        for request_id in db.finished_requests_older_than(config.DOC_RETENTION_DAYS):
            d = config.FILES_DIR / request_id
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                db.add_event(request_id, "PURGED")
