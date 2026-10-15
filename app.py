"""Application Slack (Bolt, Socket Mode) : App Home + commande /document → modale → demande traitée par le worker.

Lancement : python app.py
Tests sans Slack : python app.py --test-sf "transport"   |   python app.py --test-portail Keobiz 123456789
"""
from __future__ import annotations

import argparse
import logging
import queue
import re
import sys
from datetime import datetime

import config
import db
from documents import DOCUMENT_TYPES
from mailer import Mailer
from salesforce_client import SalesforceClient
from worker import Worker

log = logging.getLogger("app")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATUS_LABELS = {"PENDING": "en attente", "PROCESSING": "en cours", "SENT": "envoyée", "DRY_RUN": "testée", "NO_DOCUMENT": "sans document",
                 "NOT_HABILITE": "compte portail non habilité", "FAILED": "en échec"}


# ------------------------------------------------------------------ vues Slack
def home_view() -> dict:
    return {"type": "home", "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Documents fiscaux"}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            "Envoyez à un client un document téléchargé depuis *portailpro.gouv.fr* (avis CFE...).\n"
            "Le document part par email au nom du chargé de mission, et vous recevez ici la confirmation."},
         "accessory": {"type": "button", "action_id": "open_request_modal", "style": "primary",
                       "text": {"type": "plain_text", "text": "Nouvelle demande"}}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Raccourci : tapez `/document` dans n'importe quel canal. Administrateurs : `/document campagne` pour un envoi en masse."
            + ("\n:test_tube: *Service en mode test global (DRY_RUN) : aucun email ne part chez les clients.*" if config.DRY_RUN else "")}]},
    ]}


def request_modal(selected_type: str = "CFE") -> dict:
    now = datetime.now().year
    years = [str(y) for y in range(now, now - 6, -1)]
    opt = lambda text, value: {"text": {"type": "plain_text", "text": text}, "value": value}
    doc_options = [opt(v["label"], k) for k, v in DOCUMENT_TYPES.items()]
    blocks = [
        {"type": "input", "block_id": "client_block", "label": {"type": "plain_text", "text": "Client"},
         "element": {"type": "external_select", "action_id": "client_select", "min_query_length": 2,
                     "placeholder": {"type": "plain_text", "text": "Nom du client ou SIREN"}}},
        {"type": "input", "block_id": "document_block", "label": {"type": "plain_text", "text": "Document"},
         "dispatch_action": any(v["needs_periode"] for v in DOCUMENT_TYPES.values()),
         "element": {"type": "static_select", "action_id": "document_select", "options": doc_options,
                     "initial_option": next(o for o in doc_options if o["value"] == selected_type)}},
        {"type": "input", "block_id": "annee_block", "label": {"type": "plain_text", "text": "Année"},
         "element": {"type": "static_select", "action_id": "annee_select",
                     "options": [opt(y, y) for y in years], "initial_option": opt(years[0], years[0])}},
    ]
    if DOCUMENT_TYPES[selected_type]["needs_periode"]:
        blocks.append({"type": "input", "block_id": "periode_block", "label": {"type": "plain_text", "text": "Période"},
                       "element": {"type": "plain_text_input", "action_id": "periode_input",
                                   "placeholder": {"type": "plain_text", "text": "ex. T2 ou 2025-03"}}})
    blocks.append({"type": "input", "block_id": "force_block", "optional": True, "label": {"type": "plain_text", "text": "Options"},
                   "element": {"type": "checkboxes", "action_id": "force", "options": [
                       opt("Mode test : ne rien envoyer au client, me transmettre les PDF et l'aperçu de l'email dans Slack", "dry_run"),
                       opt("Renvoyer même si une demande identique a déjà été traitée récemment", "force")]}})
    return {"type": "modal", "callback_id": "document_request",
            "title": {"type": "plain_text", "text": "Document fiscal"},
            "submit": {"type": "plain_text", "text": "Envoyer au client"},
            "close": {"type": "plain_text", "text": "Annuler"}, "blocks": blocks}


def campaign_modal(cabinets: dict) -> dict:
    now = datetime.now().year
    years = [str(y) for y in range(now, now - 4, -1)]
    opt = lambda text, value: {"text": {"type": "plain_text", "text": text}, "value": value}
    cab_options = [opt(c.libelle, code) for code, c in cabinets.items() if c.active]
    doc_options = [opt(v["label"], k) for k, v in DOCUMENT_TYPES.items()]
    mode = (f"Emails redirigés vers {config.TEST_RECIPIENT}" if config.TEST_RECIPIENT else "Emails envoyés AUX CLIENTS") \
        if not config.DRY_RUN else "Mode test global : aucun email"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            "*1.* L'inventaire interroge le portail pour chaque client et produit un rapport Excel. *Rien n'est envoyé.*\n"
            "*2.* L'envoi ne démarre qu'après votre clic « Lancer l'envoi » sur le rapport.\n"
            f":information_source: Configuration actuelle : *{mode}*, archivage "
            + ("sur le compte de test" if config.ARCHIVE_TEST_ACCOUNT_ID else "sur les fiches clients") + "."}},
        {"type": "input", "block_id": "cabinet_block", "label": {"type": "plain_text", "text": "Cabinet"},
         "element": {"type": "static_select", "action_id": "cabinet_select", "options": cab_options, "initial_option": cab_options[0]}},
        {"type": "input", "block_id": "document_block", "label": {"type": "plain_text", "text": "Document"},
         "element": {"type": "static_select", "action_id": "document_select", "options": doc_options, "initial_option": doc_options[0]}},
        {"type": "input", "block_id": "annee_block", "label": {"type": "plain_text", "text": "Année"},
         "element": {"type": "static_select", "action_id": "annee_select", "options": [opt(y, y) for y in years], "initial_option": opt(years[0], years[0])}},
        {"type": "input", "block_id": "source_block", "label": {"type": "plain_text", "text": "Clients concernés"},
         "element": {"type": "radio_buttons", "action_id": "source", "initial_option": opt("Liste de SIREN ci-dessous (tests)", "sirens"), "options": [
             opt("Liste de SIREN ci-dessous (tests)", "sirens"),
             opt(f"Tous les clients du cabinet ({config.CAMPAIGN_SF_FILTER})", "salesforce")]}},
        {"type": "input", "block_id": "sirens_block", "optional": True, "label": {"type": "plain_text", "text": "SIREN (un par ligne)"},
         "element": {"type": "plain_text_input", "action_id": "sirens", "multiline": True,
                     "placeholder": {"type": "plain_text", "text": "123456789\n987654321"}}},
        {"type": "input", "block_id": "limit_block", "optional": True, "label": {"type": "plain_text", "text": "Limite d'envois (laisser vide = tous)"},
         "element": {"type": "number_input", "action_id": "limit", "is_decimal_allowed": False, "min_value": "1",
                     "placeholder": {"type": "plain_text", "text": "ex. 5 pour un test"}}},
    ]
    return {"type": "modal", "callback_id": "campaign_request", "title": {"type": "plain_text", "text": "Campagne"},
            "submit": {"type": "plain_text", "text": "Lancer l'inventaire"}, "close": {"type": "plain_text", "text": "Annuler"}, "blocks": blocks}


# ------------------------------------------------------------------ handlers
def register_handlers(app, sf: SalesforceClient, cabinets: dict, work_queue: queue.Queue, campaigns=None) -> None:

    def allowed(user_id: str) -> bool:
        return not config.SLACK_ALLOWED_USERS or user_id in config.SLACK_ALLOWED_USERS

    @app.event("app_home_opened")
    def on_home(client, event):
        client.views_publish(user_id=event["user"], view=home_view())

    @app.action("open_request_modal")
    def on_open(ack, body, client):
        ack()
        if not allowed(body["user"]["id"]):
            return client.chat_postMessage(channel=body["user"]["id"], text="Vous n'êtes pas autorisé à utiliser cette application.")
        client.views_open(trigger_id=body["trigger_id"], view=request_modal())

    @app.command("/document")
    def on_command(ack, body, client, respond):
        ack()
        if not allowed(body["user_id"]):
            return respond("Vous n'êtes pas autorisé à utiliser cette application.")
        if (body.get("text") or "").strip().lower().startswith("campagne"):
            if campaigns is None or not campaigns.is_admin(body["user_id"]):
                return respond("Les campagnes sont réservées aux administrateurs (CAMPAIGN_ADMINS).")
            busy = campaigns.busy()
            if busy:
                return respond(f"La campagne `{busy['campaign_id']}` est en cours ({busy['status']}). Une seule campagne à la fois.")
            return client.views_open(trigger_id=body["trigger_id"], view=campaign_modal(cabinets))
        client.views_open(trigger_id=body["trigger_id"], view=request_modal())

    @app.view("campaign_request")
    def on_campaign_submit(ack, body, view, client):
        user_id = body["user"]["id"]
        if campaigns is None or not campaigns.is_admin(user_id):
            return ack(response_action="errors", errors={"cabinet_block": "Réservé aux administrateurs de campagne."})
        v = view["state"]["values"]
        entite = v["cabinet_block"]["cabinet_select"]["selected_option"]["value"]
        doc_type = v["document_block"]["document_select"]["selected_option"]["value"]
        annee = v["annee_block"]["annee_select"]["selected_option"]["value"]
        source = v["source_block"]["source"]["selected_option"]["value"]
        sirens = [x for x in re.split(r"[\s,;]+", v.get("sirens_block", {}).get("sirens", {}).get("value") or "") if x]
        limit_raw = v.get("limit_block", {}).get("limit", {}).get("value")
        send_limit = int(limit_raw) if limit_raw else None
        if source == "sirens":
            bad = [x for x in sirens if not re.fullmatch(r"\d{9}", x)]
            if not sirens:
                return ack(response_action="errors", errors={"sirens_block": "Saisissez au moins un SIREN, ou choisissez « Tous les clients »."})
            if bad:
                return ack(response_action="errors", errors={"sirens_block": f"SIREN invalide(s) : {', '.join(bad[:5])}"})
        if campaigns.busy():
            return ack(response_action="errors", errors={"cabinet_block": "Une campagne est déjà en cours."})
        ack()
        cid = campaigns.create(user_id, body["user"].get("name") or body["user"].get("username"), entite, doc_type, annee,
                               source, sirens if source == "sirens" else None, send_limit)
        client.chat_postMessage(channel=user_id, text=f":mag: Campagne `{cid}` créée — inventaire en cours. Suivi et rapport dans <#{config.CAMPAIGN_CHANNEL}>."
                                if config.CAMPAIGN_CHANNEL else f":mag: Campagne `{cid}` créée — inventaire en cours.")

    @app.action("campaign_send")
    def on_campaign_send(ack, body, client):
        ack()
        user_id, cid = body["user"]["id"], body["actions"][0]["value"]
        if campaigns is None or not campaigns.is_admin(user_id):
            return client.chat_postEphemeral(channel=body["channel"]["id"], user=user_id, text="Réservé aux administrateurs de campagne.")
        err = campaigns.start_send(cid, user_id)
        if err:
            return client.chat_postEphemeral(channel=body["channel"]["id"], user=user_id, text=f":warning: {err}")
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=f":rocket: Envoi de la campagne `{cid}` lancé par <@{user_id}>.", blocks=[])

    @app.action("campaign_cancel")
    def on_campaign_cancel(ack, body, client):
        ack()
        user_id, cid = body["user"]["id"], body["actions"][0]["value"]
        if campaigns is None or not campaigns.is_admin(user_id):
            return client.chat_postEphemeral(channel=body["channel"]["id"], user=user_id, text="Réservé aux administrateurs de campagne.")
        err = campaigns.cancel(cid)
        if err:
            return client.chat_postEphemeral(channel=body["channel"]["id"], user=user_id, text=f":warning: {err}")
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=f":no_entry_sign: Campagne `{cid}` annulée par <@{user_id}>.", blocks=[])

    @app.options("client_select")
    def on_client_options(ack, payload):
        try:
            results = sf.search_accounts(payload.get("value", ""))
        except Exception as e:
            log.warning("Recherche Salesforce : %s", e)
            results = []
        ack(options=[{"text": {"type": "plain_text", "text": f"{r['name']} — {r['siren']}"[:75]}, "value": r["id"]}
                     for r in results])

    @app.action("document_select")
    def on_document_change(ack, body, client):
        ack()
        selected = body["actions"][0]["selected_option"]["value"]
        client.views_update(view_id=body["view"]["id"], hash=body["view"]["hash"], view=request_modal(selected))

    @app.view("document_request")
    def on_submit(ack, body, view, client):
        user_id = body["user"]["id"]
        v = view["state"]["values"]
        account_id = v["client_block"]["client_select"].get("selected_option", {}).get("value")
        doc_type = v["document_block"]["document_select"]["selected_option"]["value"]
        annee = v["annee_block"]["annee_select"]["selected_option"]["value"]
        periode = (v.get("periode_block", {}).get("periode_input", {}).get("value") or "").strip() or None
        options = {o["value"] for o in v.get("force_block", {}).get("force", {}).get("selected_options") or []}
        force, dry_run = "force" in options, ("dry_run" in options or config.DRY_RUN)

        errors = {}
        acc = None
        if not allowed(user_id):
            errors["client_block"] = "Vous n'êtes pas autorisé à utiliser cette application."
        elif not account_id:
            errors["client_block"] = "Choisissez un client."
        else:
            try:
                acc = sf.get_account(account_id)
            except Exception as e:
                log.warning("Salesforce get_account : %s", e)
            if not acc:
                errors["client_block"] = "Client introuvable dans Salesforce (réessayez)."
            elif not acc["siren"] or len(acc["siren"]) != 9:
                errors["client_block"] = "SIREN manquant ou invalide dans Salesforce."
            elif not EMAIL_RE.match(acc["email"]):
                errors["client_block"] = "Adresse email du contact manquante ou invalide dans Salesforce (ContactEmail__c)."
            elif acc["entite"] not in cabinets or not cabinets[acc["entite"]].active:
                errors["client_block"] = f"Cabinet « {acc['entite'] or '?'} » non configuré dans cabinets.json."
        if DOCUMENT_TYPES[doc_type]["needs_periode"] and not periode:
            errors["periode_block"] = "Période requise pour ce document."
        if not errors and not force and not dry_run:
            dup = db.find_recent_duplicate(account_id, doc_type, annee, periode, config.DEDUP_DAYS)
            if dup:
                when = dup["created_at"][:16].replace("T", " à ")
                errors["client_block"] = (f"Demande identique {STATUS_LABELS.get(dup['status'], dup['status'])} le {when} "
                                          f"par {dup['slack_user_name'] or 'un collaborateur'}. Cochez « Renvoyer » pour forcer.")
        if errors:
            return ack(response_action="errors", errors=errors)
        ack()

        request_id = db.create_request({
            "slack_user_id": user_id, "slack_user_name": body["user"].get("name") or body["user"].get("username"),
            "sf_account_id": acc["id"], "client_name": acc["name"], "siren": acc["siren"], "entite": acc["entite"],
            "client_email": acc["email"], "client_civilite": acc["civilite"], "client_nom": acc["nom"],
            "client_prenom": acc["prenom"], "cdm_nom": acc["cdm_nom"], "cdm_email": acc["cdm_email"],
            "cdm_telephone": acc["cdm_telephone"], "document_type": doc_type, "annee": annee, "periode": periode,
            "dry_run": 1 if dry_run else 0,
        })
        work_queue.put(request_id)
        client.chat_postMessage(channel=user_id, text=(
            f":hourglass_flowing_sand: Demande enregistrée : *{DOCUMENT_TYPES[doc_type]['label']} {annee}* pour "
            f"*{acc['name']}* (SIREN {acc['siren']}) → {acc['email']}."
            + (" *Mode test : le client ne recevra rien, vous aurez les PDF ici.*" if dry_run else "")
            + " Vous serez notifié ici du résultat."))


# ------------------------------------------------------------------ démarrage
def build_services():
    config.check_required()
    db.init_db()
    cabinets = config.load_cabinets()
    sf = SalesforceClient(config.sf_auth())
    mailer = Mailer(config.GOOGLE_SA_KEY_PATH, config.GOOGLE_CLIENT_SECRET_FILE, config.GOOGLE_TOKEN_FILE,
                    config.TEMPLATE_FILE, config.TEST_RECIPIENT)
    return cabinets, sf, mailer


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):   # Windows : console en UTF-8 pour les accents et émojis du log
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app.log", encoding="utf-8")])
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-sf", metavar="TERME", help="teste la recherche client Salesforce et s'arrête")
    ap.add_argument("--test-portail", nargs=2, metavar=("ENTITE", "SIREN"), help="teste la connexion portailpro d'un cabinet et liste les avis d'un SIREN")
    ap.add_argument("--test-compte", nargs=2, metavar=("EMAIL_CDM", "SIREN"),
                    help="teste le compte portail d'un chargé de mission (déclaré dans .env) et liste les avis d'un SIREN")
    ap.add_argument("--test-mysql", action="store_true", help="teste le journal MySQL : crée la table si besoin et rejoue les demandes récentes")
    args = ap.parse_args()

    cabinets, sf, mailer = build_services()

    if args.test_sf:
        for r in sf.search_accounts(args.test_sf):
            print(f"{r['name']:40} {r['siren']}  {r['entite']:15} {r['email']:35} CDM={r['cdm_nom']}")
        return 0

    if args.test_portail:
        from portailpro import PortailPro
        entite, siren = args.test_portail
        cab = cabinets[entite]
        p = PortailPro(config.PROFILES_DIR / cab.code, headless=config.PORTAIL_HEADLESS, channel=config.BROWSER_CHANNEL,
                       login=cab.portal_login, password=cab.portal_password)
        try:
            p.login()
            for a in p.list_avis(re.sub(r"\D", "", siren)):
                print(f"{a.libelle:40} {a.annee}  SIRET {a.siret}  {a.montant} €  lu={a.deja_lu}  suite={bool(a.id_suite)}")
        finally:
            p.close()
        return 0

    if args.test_compte:
        from portailpro import PortailPro
        email, siren = args.test_compte
        acc = config.load_collab_accounts().get(email.strip().lower())
        if not acc:
            print(f"Aucun compte actif déclaré dans .env pour {email} (PORTAIL_COLLAB<n>_LOGIN / _PASSWORD / _EMAIL_SF)"); return 1
        p = PortailPro(config.PROFILES_DIR / acc.code, headless=config.PORTAIL_HEADLESS, channel=config.BROWSER_CHANNEL,
                       login=acc.login, password=acc.password)
        try:
            p.login()
            for a in p.list_avis(re.sub(r"\D", "", siren)):
                print(f"{a.libelle:40} {a.annee}  SIRET {a.siret}  {a.montant} €  lu={a.deja_lu}  suite={bool(a.id_suite)}")
        finally:
            p.close()
        return 0

    if args.test_mysql:
        import mysql_journal
        if not mysql_journal.enabled():
            print("MYSQL_HOST / MYSQL_DATABASE / MYSQL_USER non renseignés dans .env"); return 1
        mysql_journal.journal.ensure_table()
        n = mysql_journal.journal.resync_all(365)
        print(f"Table `{config.MYSQL_TABLE}` prête sur {config.MYSQL_HOST}/{config.MYSQL_DATABASE} — {n} demande(s) synchronisée(s), "
              f"{mysql_journal.journal.count()} ligne(s) au total")
        return 0

    import mysql_journal
    mysql_journal.install()

    from slack_bolt import App
    try:  # client websocket-client : reconnexion plus fiable après une coupure réseau ou une mise en veille
        from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler
    except ImportError:
        from slack_bolt.adapter.socket_mode import SocketModeHandler

    from campaign import CampaignManager

    app = App(token=config.SLACK_BOT_TOKEN)
    work_queue: queue.Queue = queue.Queue()
    campaigns = CampaignManager(app.client, sf, cabinets, work_queue)
    register_handlers(app, sf, cabinets, work_queue, campaigns)

    try:  # réchauffer la session Salesforce (le login SOAP prend 1-2 s, à éviter pendant une soumission)
        sf.search_accounts("keobiz")
    except Exception as e:
        log.warning("Salesforce indisponible au démarrage : %s", e)

    Worker(work_queue, app.client, cabinets, sf, mailer).start()
    for rid in db.pending_request_ids():   # reprise des demandes interrompues par un redémarrage
        work_queue.put(rid)
    campaigns.resume_interrupted()

    log.info("Cabinets configurés : %s — connexion Slack (Socket Mode)...", ", ".join(cabinets))
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
