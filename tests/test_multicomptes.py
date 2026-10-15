"""Tests du multi-comptes portailpro — sans portail, sans Slack, sur une base de test temporaire.

Lancer :  .venv/bin/python -m unittest discover -s tests -v
(SKIP_BROWSER=1 pour sauter le test qui ouvre réellement Chrome, sans portail.)
"""
from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Base, fichiers et profils de test : JAMAIS ceux du service (à fixer AVANT d'importer config).
_TMP = tempfile.mkdtemp(prefix="cfe-tests-")
os.environ["DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import worker as worker_mod  # noqa: E402
from campaign import CampaignManager  # noqa: E402
from documents import DocResult  # noqa: E402
from portailpro import (_FAILED, _REFUSED, LOGIN_COOLDOWN_S, MAX_FAILED_LOGINS, NonHabiliteError,  # noqa: E402
                        PortailLoginAbandoned, PortailLoginRefused, PortailMaintenanceError, PortailPro,
                        PortailProError, _credentials_key, extract_pdf, json_or_maintenance, non_habilite_message)

assert str(config.DB_PATH).startswith(_TMP), "les tests doivent utiliser une base temporaire"
db.init_db()

NON_HABILITE_JSON = {"resultat": [], "infos": [{"infoCode": "131105", "infoMessage": "Vous n'êtes pas habilité à voir cette fonctionnalité",
                                                 "infoLevel": "INFO", "infoType": "FUNCTIONAL"}]}
AVIS_JSON = {"resultat": [{"libelle": "Cotisation foncière des entreprises", "annee": 2025, "typeAvis": "Avis",
                           "numeroFiscal": "12345678900011", "referenceAvis": "R1", "commune": "ROUEN", "montant": 120.0,
                           "idEnsuaAvisPrincipal": "ID1", "idEnsuaAvisSuite": None, "topLectureAvisPrincipal": 0}], "infos": []}


def resp(status=200, json=None, text=None) -> httpx.Response:
    req = httpx.Request("GET", "https://services.portailpro.gouv.fr/x")
    return httpx.Response(status, json=json, request=req) if json is not None else httpx.Response(status, text=text or "", request=req)


def fake_portail(response: httpx.Response) -> PortailPro:
    """PortailPro sans navigateur : seul _get est remplacé (list_avis réel)."""
    p = object.__new__(PortailPro)
    p._get = lambda *a, **k: response
    return p


# ---------------------------------------------------------------------------------------------- réponses du portail
class ReponsesPortail(unittest.TestCase):
    def test_non_habilite_detecte(self):
        self.assertIn("pas habilité", non_habilite_message(NON_HABILITE_JSON))

    def test_resultat_non_vide_n_est_pas_non_habilite(self):
        self.assertIsNone(non_habilite_message(AVIS_JSON))
        self.assertIsNone(non_habilite_message({"resultat": [], "infos": []}))   # habilité mais aucun avis

    def test_list_avis_leve_non_habilite_sur_131105(self):
        with self.assertRaises(NonHabiliteError):
            fake_portail(resp(json=NON_HABILITE_JSON)).list_avis("123456789")

    def test_list_avis_403_non_habilite_seulement_avec_131105(self):
        with self.assertRaises(NonHabiliteError):
            fake_portail(resp(403, json=NON_HABILITE_JSON)).list_avis("123456789")
        for r in (resp(403, text="<html>Forbidden</html>"), resp(403, json={"message": "Too many requests"})):
            with self.assertRaises(PortailProError) as ctx:   # limitation / blocage : une erreur, pas « non habilité »
                fake_portail(r).list_avis("123456789")
            self.assertNotIsInstance(ctx.exception, NonHabiliteError)

    def test_list_avis_normal(self):
        avis = fake_portail(resp(json=AVIS_JSON)).list_avis("123456789")
        self.assertEqual(len(avis), 1)
        self.assertTrue(avis[0].is_cfe)

    def test_habilite_sans_avis_renvoie_liste_vide(self):
        self.assertEqual(fake_portail(resp(json={"resultat": [], "infos": []})).list_avis("123456789"), [])

    def test_page_de_maintenance(self):
        with self.assertRaises(PortailMaintenanceError):
            fake_portail(resp(text="<html>Le site est en maintenance</html>")).list_avis("123456789")

    def test_html_inattendu_n_est_pas_maintenance(self):
        with self.assertRaises(PortailProError) as ctx:
            json_or_maintenance(resp(text="<html>autre chose</html>"))
        self.assertNotIsInstance(ctx.exception, PortailMaintenanceError)

    def test_non_habilite_est_une_erreur_portail(self):   # compatibilité avec les except PortailProError existants
        self.assertTrue(issubclass(NonHabiliteError, PortailProError))

    def test_pdf_page_de_maintenance(self):
        r = httpx.Response(200, text="<html>Site en maintenance</html>", headers={"content-type": "text/html"},
                           request=httpx.Request("GET", "https://services.portailpro.gouv.fr/x"))
        with self.assertRaises(PortailMaintenanceError):
            extract_pdf(r)


class OuvertureInterrompue(unittest.TestCase):
    def test_rien_ne_reste_ouvert_si_l_ouverture_echoue(self):
        import portailpro

        class Ctx:
            pages, closed = [], False

            def new_page(self):
                raise RuntimeError("navigateur planté juste après son lancement")

            def close(self):
                self.closed = True

        class Chromium:
            ctx = None

            def launch_persistent_context(self, *a, **k):
                Chromium.ctx = Ctx()
                return Chromium.ctx

        class Engine:
            chromium, stopped = Chromium(), False

            def stop(self):
                Engine.stopped = True

        saved = portailpro.sync_playwright
        portailpro.sync_playwright = lambda: type("S", (), {"start": lambda self: Engine()})()
        out = {}

        def run():   # thread neuf : moteur partagé vierge
            try:
                PortailPro("/nonexistent", headless=True)
            except RuntimeError:
                out["raised"] = True
            out["users"] = getattr(portailpro._tls, "users", 0)
        try:
            t = threading.Thread(target=run)
            t.start(); t.join(10)
        finally:
            portailpro.sync_playwright = saved
        self.assertTrue(out.get("raised"))
        self.assertTrue(Chromium.ctx.closed)    # contexte Chrome refermé
        self.assertEqual(out["users"], 0)       # moteur partagé décompté…
        self.assertTrue(Engine.stopped)         # …et arrêté


class IdentifiantsRefuses(unittest.TestCase):
    def test_identifiants_refuses_ne_sont_plus_jamais_resoumis(self):
        p = object.__new__(PortailPro)   # aucun navigateur : toute tentative de saisie lèverait AttributeError
        p.login_id, p.password = "refuse@keobiz.fr", "mauvais"
        key = _credentials_key(p.login_id, p.password)
        _REFUSED.add(key)
        try:
            with self.assertRaises(PortailLoginRefused):
                p.login()
        finally:
            _REFUSED.discard(key)

    def test_connexions_en_echec_a_repetition_suspendues_pour_toutes_les_sessions(self):
        visits = []

        class Page:
            def goto(self, *a, **k):
                visits.append(1)
                raise RuntimeError("Timeout 30000ms exceeded")

        def session():   # session neuve à chaque fois, comme le worker après un échec
            p = object.__new__(PortailPro)
            p.login_id, p.password, p.token, p.page = "mfa@keobiz.fr", "bon", None, Page()
            return p
        key = _credentials_key("mfa@keobiz.fr", "bon")
        try:
            for _ in range(MAX_FAILED_LOGINS):
                with self.assertRaises(RuntimeError):
                    session().login()
            with self.assertRaises(PortailLoginAbandoned) as ctx:
                session().login()
            self.assertEqual(len(visits), MAX_FAILED_LOGINS)    # plus aucune tentative, même sur une session neuve
            self.assertGreater(ctx.exception.until, time.time() + 3000)
            n, last = _FAILED[key]
            _FAILED[key] = (n, last - LOGIN_COOLDOWN_S - 1)       # suspension écoulée : UNE nouvelle tentative…
            with self.assertRaises(RuntimeError):
                session().login()
            with self.assertRaises(PortailLoginAbandoned):        # …puis de nouveau suspendu
                session().login()
            self.assertEqual(len(visits), MAX_FAILED_LOGINS + 1)
        finally:
            _FAILED.pop(key, None)

    def test_connexion_reussie_remet_le_compteur_a_zero(self):
        key = _credentials_key("ok@keobiz.fr", "bon")
        _FAILED[key] = (1, time.time())
        p = object.__new__(PortailPro)
        p.login_id, p.password, p.token = "ok@keobiz.fr", "bon", None

        class Page:
            def goto(self, *a, **k):
                p.token = "Bearer x"   # jeton capturé : connexion réussie
        p.page, p._login_form_visible = Page(), lambda timeout_ms: False
        p.login()
        self.assertNotIn(key, _FAILED)


# ---------------------------------------------------------------------------------------------- comptes dans .env
class ComptesCollaborateurs(unittest.TestCase):
    def setUp(self):
        self.saved = {k: v for k, v in os.environ.items() if k.startswith("PORTAIL_COLLAB")}
        for k in self.saved:
            del os.environ[k]

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("PORTAIL_COLLAB")]:
            del os.environ[k]
        os.environ.update(self.saved)

    def test_lecture_des_comptes(self):
        os.environ.update({
            "PORTAIL_COLLAB2_LOGIN": "C.Durand@Keobiz.fr", "PORTAIL_COLLAB2_PASSWORD": "x",              # email déduit du login
            "PORTAIL_COLLAB7_LOGIN": "identifiant", "PORTAIL_COLLAB7_PASSWORD": "x",
            "PORTAIL_COLLAB7_EMAIL_SF": "a.b@keobiz.fr", "PORTAIL_COLLAB7_NOM": "A B",                 # email explicite
            "PORTAIL_COLLAB3_LOGIN": "x.y@keobiz.fr", "PORTAIL_COLLAB3_PASSWORD": "x", "PORTAIL_COLLAB3_ACTIF": "false",
            "PORTAIL_COLLAB4_LOGIN": "sans-mot-de-passe@keobiz.fr",
            "PORTAIL_COLLAB5_LOGIN": "pas-un-email", "PORTAIL_COLLAB5_PASSWORD": "x",                  # impossible à rattacher
        })
        accs = config.load_collab_accounts()
        self.assertEqual(sorted(accs), ["a.b@keobiz.fr", "c.durand@keobiz.fr"])
        self.assertEqual(accs["c.durand@keobiz.fr"].code, "collab-c.durand")   # partie locale de l'email
        self.assertEqual(accs["a.b@keobiz.fr"].nom, "A B")

    def test_codes_de_session_uniques_et_un_compte_par_email(self):
        os.environ.update({
            "PORTAIL_COLLAB1_LOGIN": "j.martin@keobiz.fr", "PORTAIL_COLLAB1_PASSWORD": "x",
            "PORTAIL_COLLAB2_LOGIN": "j.martin@autre-cabinet.fr", "PORTAIL_COLLAB2_PASSWORD": "x",   # même début d'email
            "PORTAIL_COLLAB3_LOGIN": "j.martin@keobiz.fr", "PORTAIL_COLLAB3_PASSWORD": "y",          # doublon d'email
        })
        accs = config.load_collab_accounts()
        self.assertEqual(len(accs), 2)
        self.assertEqual(len({a.code for a in accs.values()}), 2)
        self.assertEqual(accs["j.martin@keobiz.fr"].password, "x")   # le premier déclaré est gardé
        self.assertEqual(accs["j.martin@keobiz.fr"].code, "collab-j.martin")
        self.assertTrue(accs["j.martin@autre-cabinet.fr"].code.startswith("collab-j.martin-"))

    def test_desactiver_un_compte_ne_donne_pas_sa_session_a_un_autre(self):
        os.environ.update({"PORTAIL_COLLAB1_LOGIN": "j.martin@keobiz.fr", "PORTAIL_COLLAB1_PASSWORD": "x",
                           "PORTAIL_COLLAB2_LOGIN": "j.martin@autre-cabinet.fr", "PORTAIL_COLLAB2_PASSWORD": "x"})
        avant = config.load_collab_accounts()["j.martin@autre-cabinet.fr"].code
        os.environ["PORTAIL_COLLAB1_ACTIF"] = "false"
        apres = config.load_collab_accounts()["j.martin@autre-cabinet.fr"].code
        self.assertEqual(avant, apres)
        self.assertNotEqual(apres, "collab-j.martin")

    def test_aucune_collision_de_noms_de_session(self):
        emails = ["j.martin_3@keobiz.fr", "j.martin@keobiz.fr", "j.martin@autre-cabinet.fr", "j-martin@keobiz.fr",
                  "j_martin@keobiz.fr", "@keobiz.fr", "c.durand@keobiz.fr"]
        codes = [config.collab_code(e) for e in emails]
        self.assertEqual(len(set(codes)), len(codes))
        self.assertEqual(config.collab_code("c.durand@keobiz.fr"), "collab-c.durand")   # règle simple pour une adresse @keobiz.fr

    def test_numerotation_avec_zero_devant(self):
        os.environ.update({"PORTAIL_COLLAB01_LOGIN": "a.zero@keobiz.fr", "PORTAIL_COLLAB01_PASSWORD": "x"})
        self.assertIn("a.zero@keobiz.fr", config.load_collab_accounts())


# ---------------------------------------------------------------------------------------------- cascade du worker
class FakeSlack:
    def __init__(self):
        self.posts, self.uploads = [], []

    def chat_postMessage(self, channel, text, **kw):
        self.posts.append((channel, text))
        return {"ts": str(len(self.posts))}

    def chat_update(self, channel, ts, text, **kw):
        self.posts.append((channel, text))

    def conversations_open(self, users):
        return {"channel": {"id": f"D-{users}"}}

    def files_upload_v2(self, **kw):
        self.uploads.append(kw)


class FakeMailer:
    def preview(self, req, cabinet):
        return "aperçu de l'email"


CAB, COLLAB = object(), object()   # sessions factices du cabinet et du chargé de mission


class Cascade(unittest.TestCase):
    def setUp(self):
        self.slack = FakeSlack()
        self.cabinet = config.Cabinet(code="Keobiz", libelle="Keobiz", portal_login="x", portal_password="y",
                                      sender_address="contact@exemple.fr", reply_to="contact@exemple.fr", signature_html="")
        self.camille = config.PortalAccount(code="collab-c.durand", nom="", email="c.durand@keobiz.fr", login="c.durand@keobiz.fr", password="z")
        self.saved_fetch, self.saved_alert = worker_mod.documents.fetch, config.SLACK_ALERT_CHANNEL
        config.SLACK_ALERT_CHANNEL = "ALERTES"
        self.calls = []

    def tearDown(self):
        worker_mod.documents.fetch, config.SLACK_ALERT_CHANNEL = self.saved_fetch, self.saved_alert

    def make_worker(self, collabs, cabinet_session=lambda: CAB, collab_session=lambda: COLLAB):
        w = worker_mod.Worker(queue.Queue(), self.slack, {"Keobiz": self.cabinet}, sf=None, mailer=FakeMailer(), collabs=collabs)
        w.portail_for = lambda cab: (self.calls.append("cabinet"), cabinet_session())[1]
        w.portail_for_collab = lambda acc: (self.calls.append("collab"), collab_session())[1]
        return w

    def behave(self, cabinet: str, collab: str = "non"):
        def fetch(portail, doc_type, siren, annee, periode):
            action = cabinet if portail is CAB else collab
            if action == "non":
                raise NonHabiliteError(f"SIREN {siren} : compte non habilité")
            if action == "maintenance":
                raise PortailMaintenanceError("le portail impots est en maintenance")
            if action == "refus":
                raise PortailLoginRefused("identifiant ou mot de passe refusé")
            if action == "crash":
                raise RuntimeError("panne inattendue")
            if action == "vide":
                return [DocResult(label=f"SIREN {siren}", filename="", content=None, status="SKIPPED",
                                  message=f"Aucun avis CFE {annee} disponible sur le portail")]
            return [DocResult(label="SIRET 12345678900011 – ROUEN", filename=f"CFE_{annee}_12345678900011.pdf",
                              content=b"%PDF-1.4 test", status="DOWNLOADED")]
        worker_mod.documents.fetch = fetch

    def new_request(self, cdm_email="C.Durand@Keobiz.fr") -> str:
        return db.create_request({"slack_user_id": "U1", "slack_user_name": "testeur", "sf_account_id": "001TEST",
                                  "client_name": "ACME", "siren": "123456789", "entite": "Keobiz", "client_email": "c@x.fr",
                                  "cdm_nom": "Camille DURAND", "cdm_email": cdm_email, "document_type": "CFE", "annee": "2025",
                                  "periode": None, "dry_run": 1})

    def dm_texts(self):
        return "\n".join(t for ch, t in self.slack.posts if ch != "ALERTES")

    def alerts(self):
        return [t for ch, t in self.slack.posts if ch == "ALERTES"]

    def events(self, rid):
        return [(e["event"], e["details"]) for e in db.list_events(rid)]

    # -- cas nominaux
    def test_cabinet_non_habilite_puis_compte_du_cdm_trouve(self):
        self.behave(cabinet="non", collab="doc")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        self.assertEqual(db.get_request(rid)["status"], "DRY_RUN")
        self.assertEqual(self.calls, ["cabinet", "collab"])
        self.assertIn(("COMPTE_PORTAIL", "collab : Camille DURAND"), self.events(rid))
        self.assertIn("via le compte portail de Camille DURAND", self.dm_texts())

    def test_cabinet_trouve_compte_du_cdm_pas_sollicite(self):
        self.behave(cabinet="doc")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        self.assertEqual(db.get_request(rid)["status"], "DRY_RUN")
        self.assertEqual(self.calls, ["cabinet"])
        self.assertNotIn("via le compte portail", self.dm_texts())

    def test_aucun_compte_du_cdm_declare(self):
        self.behave(cabinet="non")
        w, rid = self.make_worker({}), self.new_request()
        w.process(rid)
        r = db.get_request(rid)
        self.assertEqual(r["status"], "NOT_HABILITE")
        self.assertIn("Keobiz", r["error_message"])
        self.assertIn("n'ont pas accès", self.dm_texts())
        self.assertIn("pas de compte portail déclaré", self.dm_texts())

    def test_aucun_des_deux_comptes_habilite(self):
        self.behave(cabinet="non", collab="non")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        r = db.get_request(rid)
        self.assertEqual(r["status"], "NOT_HABILITE")
        self.assertIn("Keobiz, Camille DURAND", r["error_message"])
        self.assertNotIn("pas de compte portail déclaré", self.dm_texts())

    def test_habilite_mais_sans_avis_reste_sans_document(self):
        self.behave(cabinet="vide")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        self.assertEqual(db.get_request(rid)["status"], "NO_DOCUMENT")
        self.assertEqual(self.calls, ["cabinet"])   # habilité : on ne cherche pas ailleurs

    def test_cdm_sans_avis_precise_le_compte(self):
        self.behave(cabinet="non", collab="vide")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        r = db.get_request(rid)
        self.assertEqual(r["status"], "NO_DOCUMENT")
        self.assertIn("compte portail de Camille DURAND", r["error_message"])

    # -- garde-fous
    def test_mot_de_passe_refuse_compte_ecarte_une_seule_tentative_une_seule_alerte(self):
        self.behave(cabinet="non")

        def refused():
            raise PortailLoginRefused("identifiant ou mot de passe refusé")
        w = self.make_worker({"c.durand@keobiz.fr": self.camille}, collab_session=refused)
        rid1, rid2 = self.new_request(), self.new_request()
        w.process(rid1)
        w.process(rid2)
        for rid in (rid1, rid2):
            r = db.get_request(rid)
            self.assertEqual(r["status"], "FAILED")
            self.assertIn("indisponible", r["error_message"])
        self.assertEqual(self.calls.count("collab"), 1)      # jamais de 2e tentative de connexion
        self.assertEqual(len(self.alerts()), 1)               # une seule alerte

    def test_mot_de_passe_du_cabinet_refuse_compte_du_cdm_prend_le_relais(self):
        self.behave(cabinet="doc", collab="doc")

        def refused():
            raise PortailLoginRefused("identifiant ou mot de passe refusé")
        w = self.make_worker({"c.durand@keobiz.fr": self.camille}, cabinet_session=refused)
        rid = self.new_request()
        w.process(rid)
        self.assertEqual(db.get_request(rid)["status"], "DRY_RUN")
        self.assertIn("Keobiz", w.unusable)
        self.assertEqual(len(self.alerts()), 1)

    def test_connexion_du_cabinet_suspendue_compte_ecarte_une_seule_alerte(self):
        self.behave(cabinet="doc", collab="doc")
        until = time.time() + 1800

        def suspended():
            raise PortailLoginAbandoned("connexion suspendue", until)
        w = self.make_worker({"c.durand@keobiz.fr": self.camille}, cabinet_session=suspended)
        rid1, rid2 = self.new_request(), self.new_request()
        w.process(rid1)
        w.process(rid2)
        self.assertEqual([db.get_request(r)["status"] for r in (rid1, rid2)], ["DRY_RUN", "DRY_RUN"])   # via Camille
        self.assertEqual(w.unusable["Keobiz"][1], until)   # écarté jusqu'à la fin de la suspension
        self.assertEqual(self.calls.count("cabinet"), 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_erreur_portail_du_cabinet_comportement_historique(self):
        self.behave(cabinet="maintenance")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        r = db.get_request(rid)
        self.assertEqual(r["status"], "FAILED")
        self.assertIn("maintenance", r["error_message"])
        self.assertEqual(self.calls, ["cabinet"])

    def test_erreur_de_recuperation_du_cdm_n_ecarte_pas_son_compte(self):
        self.behave(cabinet="non", collab="maintenance")
        w = self.make_worker({"c.durand@keobiz.fr": self.camille})
        rid1, rid2 = self.new_request(), self.new_request()
        w.process(rid1)
        r = db.get_request(rid1)
        self.assertEqual(r["status"], "FAILED")
        self.assertIn("compte portail de Camille DURAND", r["error_message"])
        self.assertNotIn("connexion impossible", r["error_message"])
        self.assertEqual(w.unusable, {})                       # pas écarté pour une erreur ponctuelle
        self.behave(cabinet="non", collab="doc")
        w.process(rid2)
        self.assertEqual(db.get_request(rid2)["status"], "DRY_RUN")   # le client suivant passe par Camille
        self.assertEqual(self.calls.count("collab"), 2)

    def test_mot_de_passe_refuse_lors_d_une_reconnexion_ecarte_le_compte(self):
        self.behave(cabinet="non", collab="refus")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        self.assertEqual(db.get_request(rid)["status"], "FAILED")
        self.assertIn("collab-c.durand", w.unusable)

    def test_erreur_technique_indique_le_compte(self):
        self.behave(cabinet="non", collab="crash")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request()
        w.process(rid)
        self.assertEqual(db.get_request(rid)["status"], "FAILED")
        self.assertTrue(any("Camille DURAND" in a for a in self.alerts()))
        self.assertEqual(w.unusable, {})

    def test_panne_du_navigateur_a_la_connexion_ecarte_le_compte_du_cdm(self):
        self.behave(cabinet="non", collab="doc")

        def crash():
            raise RuntimeError("Timeout 30000ms exceeded (page.goto)")   # erreur Playwright, pas PortailProError
        w = self.make_worker({"c.durand@keobiz.fr": self.camille}, collab_session=crash)
        rid1, rid2 = self.new_request(), self.new_request()
        w.process(rid1)
        w.process(rid2)
        for rid in (rid1, rid2):
            r = db.get_request(rid)
            self.assertEqual(r["status"], "FAILED")
            self.assertIn("indisponible", r["error_message"])
        self.assertIn("collab-c.durand", w.unusable)
        self.assertEqual(self.calls.count("collab"), 1)   # pas de nouvelle tentative pendant l'écartement
        self.assertEqual(len(self.alerts()), 1)

    def test_session_en_echec_jetee_puis_recreee(self):
        created = []

        class FakeSession:
            def __init__(self, *a, **k):
                self.closed = False
                created.append(self)

            def ensure_session(self):
                if len(created) == 1:
                    raise RuntimeError("Target page, context or browser has been closed")

            def close(self):
                self.closed = True
        saved = worker_mod.PortailPro
        worker_mod.PortailPro = FakeSession
        try:
            w = worker_mod.Worker(queue.Queue(), self.slack, {"Keobiz": self.cabinet}, sf=None, mailer=FakeMailer(), collabs={})
            with self.assertRaises(RuntimeError):
                w.portail_for(self.cabinet)
            self.assertNotIn("Keobiz", w.portails)          # session morte jetée…
            self.assertTrue(created[0].closed)
            self.assertIs(w.portail_for(self.cabinet), created[1])   # …puis recréée
        finally:
            worker_mod.PortailPro = saved

    def test_email_du_cdm_insensible_a_la_casse(self):
        self.behave(cabinet="non", collab="doc")
        w, rid = self.make_worker({"c.durand@keobiz.fr": self.camille}), self.new_request(cdm_email="  C.DURAND@KEOBIZ.FR ")
        w.process(rid)
        self.assertEqual(db.get_request(rid)["status"], "DRY_RUN")


# ---------------------------------------------------------------------------------------------- campagne (option B)
from campaign import InventoryPortals  # noqa: E402
from portailpro import Avis  # noqa: E402


def avis_cfe(annee="2025"):
    return [Avis(libelle="Cotisation foncière des entreprises", annee=annee, type_avis="Avis", siret="12345678900011",
                 reference="R", commune="ROUEN", montant=100.0, id_principal="ID", id_suite=None, deja_lu=False)]


class FakeListPortail:
    """Portail factice : réponse de list_avis (liste d'avis ou exception), avec relevé des appels."""
    def __init__(self, result):
        self.result, self.calls = result, []

    def list_avis(self, siren):
        self.calls.append(siren)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


CAMILLE = config.PortalAccount(code="collab-c.durand", nom="", email="c.durand@keobiz.fr", login="v", password="p")
AUTRE = config.PortalAccount(code="collab-a.ancien", nom="Ancien CDM", email="a.ancien@keobiz.fr", login="a", password="p")


def make_portals(sessions: dict, lists=None, complete=None, collabs=None):
    cab = config.Cabinet(code="Keobiz", libelle="Keobiz", portal_login="x", portal_password="y",
                         sender_address="contact@exemple.fr", reply_to="contact@exemple.fr", signature_html="")
    p = InventoryPortals(cab, collabs if collabs is not None else {CAMILLE.email: CAMILLE, AUTRE.email: AUTRE})
    p.session = lambda code: None if code in p.unusable else sessions.get(code)   # aucun navigateur
    p.lists, p.complete = lists or {}, complete or {}
    return p


class Campagne(unittest.TestCase):
    C = {"document_type": "CFE", "annee": "2025"}

    def setUp(self):
        self.saved_pause, config.CAMPAIGN_INVENTORY_INTERVAL_S = config.CAMPAIGN_INVENTORY_INTERVAL_S, 0
        self.cm = CampaignManager(FakeSlack(), None, {}, queue.Queue())

    def tearDown(self):
        config.CAMPAIGN_INVENTORY_INTERVAL_S = self.saved_pause

    def item(self, siren="123456789", cdm="c.durand@keobiz.fr"):
        return {"client_email": "c@x.fr", "sf_account_id": f"001-{siren}-{cdm}", "siren": siren, "cdm_email": cdm,
                "cdm_nom": "Camille DURAND"}

    # -- ordre des comptes (plan)
    def test_plan_selon_les_listes(self):
        full = {"Keobiz": True, "collab-c.durand": True, "collab-a.ancien": True}
        p = make_portals({}, lists={"Keobiz": {"111111111"}, "collab-c.durand": {"222222222"},
                                    "collab-a.ancien": {"333333333"}}, complete=full)
        self.assertEqual(p.plan("111111111", CAMILLE.email), ["Keobiz"])
        self.assertEqual(p.plan("222222222", CAMILLE.email), ["collab-c.durand"])
        self.assertEqual(p.plan("333333333", CAMILLE.email), ["collab-a.ancien"])   # dossier chez un autre collaborateur
        self.assertEqual(p.plan("999999999", CAMILLE.email), [])                   # personne : 0 requête

    def test_plan_sans_liste_revient_a_l_interrogation_directe(self):
        p = make_portals({})                                                       # petite campagne : aucune liste
        self.assertEqual(p.plan("111111111", CAMILLE.email), ["Keobiz", "collab-c.durand"])
        self.assertEqual(p.plan("111111111", "inconnu@keobiz.fr"), ["Keobiz"])
        p = make_portals({}, lists={"Keobiz": {"111111111"}}, complete={"Keobiz": False})   # liste cabinet incomplète
        self.assertEqual(p.plan("555555555", CAMILLE.email), ["Keobiz", "collab-c.durand"])

    def test_plan_ignore_un_compte_ecarte(self):
        p = make_portals({}, lists={"collab-c.durand": {"222222222"}}, complete={"Keobiz": True, "collab-c.durand": True})
        p.unusable["collab-c.durand"] = "identifiants refusés"
        self.assertEqual(p.plan("222222222", CAMILLE.email), [])

    # -- inventaire d'un client
    def test_trouve_via_le_compte_du_cdm_et_retient_le_compte(self):
        camille = FakeListPortail(avis_cfe())
        p = make_portals({"collab-c.durand": camille}, lists={"Keobiz": set(), "collab-c.durand": {"222222222"}, "collab-a.ancien": set()},
                         complete={"Keobiz": True, "collab-c.durand": True, "collab-a.ancien": True})
        status, n, montant, detail, compte = self.cm._inventory_one(p, self.C, self.item("222222222"))
        self.assertEqual((status, n, compte), ("FOUND", 1, "collab-c.durand"))
        self.assertIn("via le compte de", detail)

    def test_absent_de_toutes_les_listes_aucune_requete(self):
        cab, camille = FakeListPortail(avis_cfe()), FakeListPortail(avis_cfe())
        p = make_portals({"Keobiz": cab, "collab-c.durand": camille}, lists={"Keobiz": set(), "collab-c.durand": set(), "collab-a.ancien": set()},
                         complete={"Keobiz": True, "collab-c.durand": True, "collab-a.ancien": True})
        status, *_, detail, compte = self.cm._inventory_one(p, self.C, self.item("999999999"))
        self.assertEqual(status, "NOT_HABILITE")
        self.assertEqual(cab.calls + camille.calls, [])   # zéro requête portail
        self.assertIn("absent des listes", detail)

    def test_liste_optimiste_puis_non_habilite_passe_au_compte_suivant(self):
        cab, camille = FakeListPortail(NonHabiliteError("non habilité")), FakeListPortail(avis_cfe())
        p = make_portals({"Keobiz": cab, "collab-c.durand": camille}, lists={"Keobiz": {"444444444"}, "collab-c.durand": {"444444444"}},
                         complete={"Keobiz": True, "collab-c.durand": True})
        status, *_, compte = self.cm._inventory_one(p, self.C, self.item("444444444"))
        self.assertEqual((status, compte), ("FOUND", "collab-c.durand"))

    def test_petite_campagne_cascade(self):
        cab, camille = FakeListPortail(NonHabiliteError("non habilité")), FakeListPortail(avis_cfe())
        p = make_portals({"Keobiz": cab, "collab-c.durand": camille})
        status, *_, compte = self.cm._inventory_one(p, self.C, self.item("123456789"))
        self.assertEqual((status, compte), ("FOUND", "collab-c.durand"))
        self.assertEqual((len(cab.calls), len(camille.calls)), (1, 1))

    def test_non_habilite_partout_et_cdm_non_declare(self):
        p = make_portals({"Keobiz": FakeListPortail(NonHabiliteError("non habilité"))}, collabs={})
        status, *_, detail, _ = self.cm._inventory_one(p, self.C, self.item(cdm="inconnu@keobiz.fr"))
        self.assertEqual(status, "NOT_HABILITE")
        self.assertIn("pas de compte portail déclaré", detail)

    def test_compte_du_cdm_indisponible(self):
        p = make_portals({"Keobiz": FakeListPortail(NonHabiliteError("non habilité"))},
                         lists={"collab-c.durand": {"222222222"}}, complete={"collab-c.durand": True})
        p.unusable["collab-c.durand"] = "identifiants refusés"
        status, *_ = self.cm._inventory_one(p, self.C, self.item("222222222"))
        self.assertEqual(status, "COMPTE_INDISPONIBLE")

    def test_erreur_technique(self):
        p = make_portals({"Keobiz": FakeListPortail(PortailProError("panne"))})
        status, *_, compte = self.cm._inventory_one(p, self.C, self.item())
        self.assertEqual((status, compte), ("ERROR", "Keobiz"))   # le compte en erreur est connu de la boucle

    def test_mot_de_passe_du_cabinet_refuse_arrete_l_inventaire(self):
        p = make_portals({"Keobiz": FakeListPortail(PortailLoginRefused("refusé"))})
        with self.assertRaises(PortailLoginRefused):
            self.cm._inventory_one(p, self.C, self.item())

    def test_mot_de_passe_du_cdm_refuse_ecarte_son_compte_seulement(self):
        p = make_portals({"Keobiz": FakeListPortail(NonHabiliteError("non habilité")),
                          "collab-c.durand": FakeListPortail(PortailLoginRefused("refusé"))})
        status, *_ = self.cm._inventory_one(p, self.C, self.item())
        self.assertEqual(status, "COMPTE_INDISPONIBLE")
        self.assertIn("collab-c.durand", p.unusable)

    # -- revue option B : jamais « aucun compte habilité » à tort
    FULL = {"Keobiz": True, "collab-c.durand": True, "collab-a.ancien": True}

    def test_client_d_un_autre_collaborateur_ecarte_n_est_pas_non_habilite(self):
        p = make_portals({}, lists={"Keobiz": set(), "collab-c.durand": set(), "collab-a.ancien": {"333333333"}},
                         complete=self.FULL)
        p.unusable["collab-a.ancien"] = "identifiants refusés"
        status, *_, detail, _ = self.cm._inventory_one(p, self.C, self.item("333333333"))
        self.assertEqual(status, "COMPTE_INDISPONIBLE")
        self.assertIn("Ancien CDM", detail)

    def test_absent_des_listes_ne_cite_que_les_listes_qui_excluent_le_client(self):
        cab = FakeListPortail(NonHabiliteError("non habilité"))
        p = make_portals({"Keobiz": cab}, lists={"Keobiz": {"444444444"}, "collab-c.durand": set(), "collab-a.ancien": set()},
                         complete=self.FULL)
        status, *_, detail, _ = self.cm._inventory_one(p, self.C, self.item("444444444"))
        self.assertEqual(status, "NOT_HABILITE")
        self.assertIn("non habilités : Keobiz", detail)
        self.assertIn("absent des listes de : c.durand@keobiz.fr, Ancien CDM", detail)
        self.assertNotIn("listes de : Keobiz", detail)

    def test_compte_sans_liste_complete_rend_le_client_a_reverifier(self):
        for lists, complete, list_errors, why in (
                ({}, {}, {"collab-a.ancien": "HTTP 500"}, "liste illisible"),
                ({"collab-a.ancien": {"111111111"}}, {"collab-a.ancien": False}, {}, "liste incomplète")):
            p = make_portals({}, lists={"Keobiz": set(), "collab-c.durand": set(), **lists},
                             complete={"Keobiz": True, "collab-c.durand": True, **complete})
            p.listing, p.list_errors = True, list_errors
            status, *_, detail, _ = self.cm._inventory_one(p, self.C, self.item("999999999"))
            self.assertEqual(status, "COMPTE_INDISPONIBLE")
            self.assertIn(f"Ancien CDM ({why})", detail)

    def test_compte_ecarte_avant_sa_liste_rend_le_client_a_reverifier(self):
        p = make_portals({}, lists={"Keobiz": set(), "collab-c.durand": set()}, complete={"Keobiz": True, "collab-c.durand": True})
        p.listing, p.unusable["collab-a.ancien"] = True, "identifiants refusés"
        status, *_ = self.cm._inventory_one(p, self.C, self.item("999999999"))
        self.assertEqual(status, "COMPTE_INDISPONIBLE")

    def test_toutes_les_listes_completes_excluent_le_client(self):
        p = make_portals({}, lists={"Keobiz": set(), "collab-c.durand": set(), "collab-a.ancien": set()}, complete=self.FULL)
        p.listing, p.unusable["collab-a.ancien"] = True, "identifiants refusés"   # écarté APRÈS lecture de sa liste
        status, *_, detail, _ = self.cm._inventory_one(p, self.C, self.item("999999999"))
        self.assertEqual(status, "NOT_HABILITE")
        self.assertIn("absent des listes de : Keobiz, c.durand@keobiz.fr, Ancien CDM", detail)

    def test_petite_campagne_ne_signale_pas_les_autres_collaborateurs(self):
        p = make_portals({"Keobiz": FakeListPortail(NonHabiliteError("non")), "collab-c.durand": FakeListPortail(NonHabiliteError("non"))})
        status, *_, detail, _ = self.cm._inventory_one(p, self.C, self.item())
        self.assertEqual(status, "NOT_HABILITE")
        self.assertNotIn("non vérifié", detail)

    # -- revue option B : erreurs comptées par compte
    def test_erreurs_comptees_par_compte_et_non_remises_a_zero_sans_requete(self):
        cab = FakeListPortail(PortailProError("panne"))
        p = make_portals({"Keobiz": cab}, lists={"Keobiz": {"111111111"}, "collab-c.durand": set(), "collab-a.ancien": set()},
                         complete=self.FULL)
        self.cm._inventory_one(p, self.C, self.item("111111111"))
        self.cm._inventory_one(p, self.C, self.item("999999999"))    # classé d'après les listes, sans requête
        self.cm._inventory_one(p, self.C, self.item("111111111"))
        self.assertEqual(p.errors["Keobiz"], 2)
        cab.result = NonHabiliteError("non habilité")               # le cabinet répond de nouveau
        self.cm._inventory_one(p, self.C, self.item("111111111"))
        self.assertNotIn("Keobiz", p.errors)

    def test_compte_du_cdm_en_erreur_a_repetition_ecarte(self):
        from campaign import ERRORS_STOP
        camille = FakeListPortail(PortailProError("Target page, context or browser has been closed"))
        p = make_portals({"collab-c.durand": camille}, lists={"Keobiz": set(), "collab-c.durand": {"222222222"}, "collab-a.ancien": set()},
                         complete=self.FULL)
        for _ in range(ERRORS_STOP):
            self.assertEqual(self.cm._inventory_one(p, self.C, self.item("222222222"))[0], "ERROR")
        self.assertIn("collab-c.durand", p.unusable)
        status, *_ = self.cm._inventory_one(p, self.C, self.item("222222222"))
        self.assertEqual(status, "COMPTE_INDISPONIBLE")
        self.assertEqual(len(camille.calls), ERRORS_STOP)             # plus aucune requête une fois écarté

    def test_connexion_abandonnee(self):
        p = make_portals({"Keobiz": FakeListPortail(NonHabiliteError("non")),
                          "collab-c.durand": FakeListPortail(PortailLoginAbandoned("2 échecs"))})
        status, *_ = self.cm._inventory_one(p, self.C, self.item())
        self.assertEqual(status, "COMPTE_INDISPONIBLE")
        self.assertIn("collab-c.durand", p.unusable)
        p = make_portals({"Keobiz": FakeListPortail(PortailLoginAbandoned("2 échecs"))})
        with self.assertRaises(PortailLoginAbandoned):
            self.cm._inventory_one(p, self.C, self.item())


class ListeHabilitations(unittest.TestCase):
    def portail(self, pages: list[int]):
        p = object.__new__(PortailPro)
        p.habilitations_path, p.calls = "/pcr-habilitations/v1/habilitations/abc", []

        def get(path, params, accept):
            n = int(params["page"])
            p.calls.append(n)
            count = pages[n - 1] if n <= len(pages) else 0
            return resp(json={"sirens": [{"siren": f"{n:03d}{i:06d}", "rs": ""} for i in range(count)], "erreurs": []})
        p._get = get
        return p

    def test_pagination_jusqu_a_la_derniere_page(self):
        p = self.portail([500, 500, 123])
        sirens, complete = p.list_habilitations(page_size=500, pause_s=0, max_pages=10)
        self.assertTrue(complete)
        self.assertEqual(len(sirens), 1123)
        self.assertEqual(p.calls, [1, 2, 3])

    def test_plafond_atteint_liste_incomplete(self):
        p = self.portail([500] * 5)
        sirens, complete = p.list_habilitations(page_size=500, pause_s=0, max_pages=3)
        self.assertFalse(complete)
        self.assertEqual(p.calls, [1, 2, 3])

    # -- revue option B : une réponse anormale n'est jamais prise pour « fin de liste »
    def scripted(self, bodies: list):
        p = object.__new__(PortailPro)
        p.habilitations_path, p.calls = "/pcr-habilitations/v1/habilitations/abc", []

        def get(path, params, accept):
            p.calls.append(int(params["page"]))
            return resp(json=bodies[len(p.calls) - 1])
        p._get = get
        return p

    def test_reponse_sans_liste_des_la_premiere_page_leve_une_erreur(self):
        for body in ({"erreurs": []}, {"sirens": None}, {"sirens": {"siren": "123456789"}}, []):
            with self.assertRaises(PortailProError, msg=body):
                self.scripted([body]).list_habilitations(pause_s=0)

    def test_reponse_anormale_ensuite_garde_la_liste_partielle(self):
        page1 = {"sirens": [{"siren": f"1{i:08d}"} for i in range(10)]}
        sirens, complete = self.scripted([page1, {"erreurs": []}]).list_habilitations(page_size=10, pause_s=0)
        self.assertEqual((len(sirens), complete), (10, False))
        p = self.scripted([page1])
        first = p._get

        def get(path, params, accept):   # page 2 : reconnexion en échec dans le navigateur (pas une erreur portail)
            if params["page"] == "2":
                raise TimeoutError("Timeout 30000ms exceeded (page.reload)")
            return first(path, params, accept)
        p._get = get
        self.assertEqual(len(p.list_habilitations(page_size=10, pause_s=0)[0]), 10)
        p = self.scripted([page1])
        p._get = lambda *a: (_ for _ in ()).throw(PortailLoginAbandoned("suspendu"))   # jamais avalée
        with self.assertRaises(PortailLoginAbandoned):
            p.list_habilitations(page_size=10, pause_s=0)

    def test_erreur_inconnue_du_portail_liste_incomplete(self):
        ok = {"code": "50003", "message": "Insee"}
        sirens, complete = self.scripted([{"sirens": [{"siren": "123456789"}], "erreurs": [ok]}]).list_habilitations(pause_s=0)
        self.assertEqual((sirens, complete), ({"123456789"}, True))
        for errs in ([ok, {"code": "40001"}], {"code": "40001"}, ["erreur"]):
            sirens, complete = self.scripted([{"sirens": [{"siren": "123456789"}], "erreurs": errs}]).list_habilitations(pause_s=0)
            self.assertEqual((sirens, complete), ({"123456789"}, False), errs)   # SIREN gardés, absence non probante

    def test_premiere_page_vide_relue_une_fois(self):
        empty = {"sirens": [], "erreurs": []}
        p = self.scripted([empty, empty])
        self.assertEqual(p.list_habilitations(pause_s=0), (set(), True))   # vide deux fois : vraiment vide
        self.assertEqual(p.calls, [1, 1])
        p = self.scripted([empty, {"sirens": [{"siren": "123456789"}]}])
        self.assertEqual(p.list_habilitations(pause_s=0), ({"123456789"}, True))

    def test_entree_invalide_ne_tronque_pas_la_liste(self):
        page1 = [{"siren": f"1{i:08d}"} for i in range(9)] + [{"siren": None}]    # page pleine dont une entrée illisible
        p = self.scripted([{"sirens": page1}, {"sirens": [{"siren": "222222222"}]}])
        sirens, complete = p.list_habilitations(page_size=10, pause_s=0)
        self.assertEqual(p.calls, [1, 2])
        self.assertTrue(complete)
        self.assertIn("222222222", sirens)

    def test_pagination_ignoree_par_le_portail(self):
        same = {"sirens": [{"siren": f"1{i:08d}"} for i in range(10)]}
        p = self.scripted([same, same, same])
        sirens, complete = p.list_habilitations(page_size=10, pause_s=0, max_pages=50)
        self.assertEqual((len(sirens), complete), (10, False))
        self.assertEqual(p.calls, [1, 2])                     # pas 50 requêtes

    # -- écran « Rattachement de compte » : la liste n'est pas appelée par le site, l'identifiant du compte l'est
    def observe(self, p, *paths):
        class Req:
            def __init__(self, path):
                self.url, self.headers = "https://services.portailpro.gouv.fr" + path + "?x=1", {}
        p.rattachements = {}
        for path in paths:
            p._on_request(Req(path))

    def test_adresse_deduite_de_l_identifiant_du_compte(self):
        p = self.scripted([{"sirens": [{"siren": "123456789"}]}])
        p.habilitations_path = None
        self.observe(p, "/pcr-habilitations/compte-technique/v2/comptes/utilisateur/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        seen = []
        get = p._get
        p._get = lambda path, params, accept: (seen.append(path), get(path, params, accept))[1]
        self.assertEqual(p.list_habilitations(pause_s=0), ({"123456789"}, True))
        self.assertEqual(seen, ["/pcr-habilitations/v1/habilitations/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"])

    def test_adresse_vue_prime_sur_l_adresse_deduite(self):
        p = self.scripted([{"sirens": [{"siren": "123456789"}]}])
        p.habilitations_path = None
        self.observe(p, "/pcr-habilitations/compte-technique/v2/comptes/utilisateur/11111111-1111-1111-1111-111111111111",
                     "/pcr-habilitations/v1/habilitations/22222222-2222-2222-2222-222222222222")
        seen = []
        get = p._get
        p._get = lambda path, params, accept: (seen.append(path), get(path, params, accept))[1]
        p.list_habilitations(pause_s=0)
        self.assertEqual(seen, ["/pcr-habilitations/v1/habilitations/22222222-2222-2222-2222-222222222222"])

    def test_autres_appels_ignores(self):
        p = object.__new__(PortailPro)
        p.habilitations_path, p.habilitations_fallback = None, None
        self.observe(p, "/pcr-habilitations/compte-technique/v2/comptes/autre/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                     "/api-rattachement/v1/rattachements/")
        self.assertEqual((p.habilitations_path, p.habilitations_fallback), (None, None))

    def test_annulation_entre_deux_pages(self):
        p = self.portail([500] * 5)
        sirens, complete = p.list_habilitations(page_size=500, pause_s=0, should_stop=lambda: len(p.calls) >= 2)
        self.assertFalse(complete)
        self.assertEqual(p.calls, [1, 2])


# ---------------------------------------------------------------------------------------------- inventaire complet
class FakeInventoryPortail:
    """Remplace PortailPro dans campaign.py : chaque compte (reconnu par son identifiant) a sa liste d'entreprises."""
    LISTS: dict = {}
    calls: list = []
    on_list = None

    def __init__(self, profile_dir, headless=True, channel=None, login=None, password=None):
        self.login = login

    def block_habilitation_calls(self):
        pass

    def ensure_session(self):
        pass

    def list_habilitations(self, page_size, pause_s, max_pages, should_stop=None):
        FakeInventoryPortail.calls.append(("liste", self.login))
        if FakeInventoryPortail.on_list:
            FakeInventoryPortail.on_list(self.login)
        return set(self.LISTS[self.login]), True

    def list_avis(self, siren):
        FakeInventoryPortail.calls.append(("avis", self.login, siren))
        if siren in self.LISTS[self.login]:
            return avis_cfe()
        raise NonHabiliteError("non habilité")

    def close(self):
        pass


class FakeSF:
    def __init__(self, sirens_cdm: dict):
        self.sirens_cdm = sirens_cdm

    def get_accounts_by_sirens(self, sirens):
        return [{"id": f"001INV{s}", "name": f"Client {s}", "siren": s, "entite": "Keobiz", "email": "c@x.fr",
                 "civilite": "", "nom": "", "prenom": "", "cdm_nom": "Camille DURAND", "cdm_email": cdm, "cdm_telephone": ""}
                for s, cdm in self.sirens_cdm.items() if s in sirens]


class InventaireComplet(unittest.TestCase):
    SIRENS = {"111111111": CAMILLE.email, "222222222": CAMILLE.email, "999999999": CAMILLE.email}

    def setUp(self):
        import campaign
        self.campaign = campaign
        self.saved = (campaign.PortailPro, config.load_collab_accounts, config.CAMPAIGN_INVENTORY_INTERVAL_S,
                      config.CAMPAIGN_LIST_MIN_CLIENTS, config.CAMPAIGN_LIST_PAGE_INTERVAL_S)
        campaign.PortailPro = FakeInventoryPortail
        config.load_collab_accounts = lambda: {CAMILLE.email: CAMILLE}
        config.CAMPAIGN_INVENTORY_INTERVAL_S, config.CAMPAIGN_LIST_MIN_CLIENTS, config.CAMPAIGN_LIST_PAGE_INTERVAL_S = 0, 0, 0
        FakeInventoryPortail.LISTS = {"x": {"111111111"}, "v": {"222222222"}}   # identifiants : cabinet « x », Camille « v »
        FakeInventoryPortail.calls, FakeInventoryPortail.on_list = [], None
        cab = config.Cabinet(code="Keobiz", libelle="Keobiz", portal_login="x", portal_password="y",
                             sender_address="contact@exemple.fr", reply_to="contact@exemple.fr", signature_html="")
        self.cm = CampaignManager(FakeSlack(), FakeSF(self.SIRENS), {"Keobiz": cab}, queue.Queue())
        self.cid = db.create_campaign({"created_by": "U1", "created_by_name": "testeur", "entite": "Keobiz",
                                       "document_type": "CFE", "annee": "2025", "source": "sirens",
                                       "sirens": "\n".join(self.SIRENS), "send_limit": None, "slack_channel": "CAMP"})

    def tearDown(self):
        (self.campaign.PortailPro, config.load_collab_accounts, config.CAMPAIGN_INVENTORY_INTERVAL_S,
         config.CAMPAIGN_LIST_MIN_CLIENTS, config.CAMPAIGN_LIST_PAGE_INTERVAL_S) = self.saved

    def statuses(self):
        return {it["siren"]: (it["status"], it["portail_compte"]) for it in db.list_campaign_items(self.cid)}

    def test_chaque_client_interroge_sur_le_bon_compte(self):
        self.cm._run_inventory(self.cid)
        self.assertEqual(db.get_campaign(self.cid)["status"], "INVENTORY_DONE")
        self.assertEqual(self.statuses(), {"111111111": ("FOUND", "Keobiz"), "222222222": ("FOUND", "collab-c.durand"),
                                           "999999999": ("NOT_HABILITE", None)})
        avis = [c for c in FakeInventoryPortail.calls if c[0] == "avis"]
        self.assertEqual(avis, [("avis", "x", "111111111"), ("avis", "v", "222222222")])   # 0 requête pour 999999999

    def test_liste_vide_du_cabinet_ignoree_interrogation_directe(self):
        FakeInventoryPortail.LISTS = {"x": set(), "v": {"222222222"}}
        self.cm._run_inventory(self.cid)
        avis = [c for c in FakeInventoryPortail.calls if c[0] == "avis"]
        self.assertIn(("avis", "x", "111111111"), avis)   # cabinet interrogé directement, malgré sa liste vide
        self.assertIn(("avis", "x", "999999999"), avis)
        self.assertEqual(self.statuses()["222222222"], ("FOUND", "collab-c.durand"))

    def test_annulation_pendant_la_lecture_des_listes(self):
        FakeInventoryPortail.on_list = lambda login: db.update_campaign(self.cid, status="CANCELLED")
        self.cm._run_inventory(self.cid)
        self.assertEqual(FakeInventoryPortail.calls, [("liste", "x")])   # ni liste de Camille, ni interrogation
        self.assertEqual(db.get_campaign(self.cid)["status"], "CANCELLED")


class EnvoiCampagne(unittest.TestCase):
    def test_colonnes_portail_compte(self):
        rid = db.create_request({"slack_user_id": "U1", "client_name": "X", "siren": "123456789", "entite": "Keobiz",
                                 "document_type": "CFE", "annee": "2025", "portail_compte": "collab-a.ancien"})
        self.assertEqual(db.get_request(rid)["portail_compte"], "collab-a.ancien")
        with db.connect() as con:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(campaign_items)")}
        self.assertIn("portail_compte", cols)

    def test_le_compte_retenu_par_l_inventaire_est_essaye_en_premier(self):
        cabinet = config.Cabinet(code="Keobiz", libelle="Keobiz", portal_login="x", portal_password="y",
                                 sender_address="contact@exemple.fr", reply_to="contact@exemple.fr", signature_html="")
        w = worker_mod.Worker(queue.Queue(), FakeSlack(), {"Keobiz": cabinet}, sf=None, mailer=FakeMailer(),
                              collabs={CAMILLE.email: CAMILLE, AUTRE.email: AUTRE})
        req = {"cdm_email": CAMILLE.email, "cdm_nom": "Camille DURAND", "portail_compte": "collab-a.ancien"}
        self.assertEqual([a[2] for a in w._accounts_for(req, cabinet)], ["collab-a.ancien", "Keobiz", "collab-c.durand"])
        req["portail_compte"] = "collab-c.durand"   # compte du CDM lui-même : en premier, sans doublon
        self.assertEqual([a[2] for a in w._accounts_for(req, cabinet)], ["collab-c.durand", "Keobiz"])
        req["portail_compte"] = "Keobiz"           # trouvé sur le cabinet : ordre habituel
        self.assertEqual([a[2] for a in w._accounts_for(req, cabinet)], ["Keobiz", "collab-c.durand"])


# ---------------------------------------------------------------------------------------------- moteur Chrome partagé
@unittest.skipIf(os.environ.get("SKIP_BROWSER"), "SKIP_BROWSER=1")
class MoteurPartage(unittest.TestCase):
    def test_deux_sessions_dans_le_meme_thread(self):
        out = {}

        def run():
            try:
                a = PortailPro(Path(_TMP) / "profA", headless=True, channel=config.BROWSER_CHANNEL)
                b = PortailPro(Path(_TMP) / "profB", headless=True, channel=config.BROWSER_CHANNEL)
                a.page.goto("about:blank"); b.page.goto("about:blank")
                b.close(); a.page.goto("about:blank"); a.close(); a.close()
                out["ok"] = True
            except Exception as e:
                out["err"] = f"{type(e).__name__}: {e}"
        t = threading.Thread(target=run)
        t.start(); t.join(120)
        self.assertTrue(out.get("ok"), out.get("err"))


if __name__ == "__main__":
    unittest.main()
