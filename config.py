"""Configuration : variables d'environnement (.env) + cabinets.json (un cabinet = un compte portailpro + une identité d'envoi)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent


def env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "oui")


DATA_DIR = Path(env("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "requests.sqlite"
FILES_DIR = DATA_DIR / "requests"
PROFILES_DIR = DATA_DIR / "chrome-profiles"
DOC_RETENTION_DAYS = int(env("DOC_RETENTION_DAYS", "30"))
DEDUP_DAYS = int(env("DEDUP_DAYS", "30"))
TEMPLATE_FILE = BASE_DIR / "templates" / "email.html"

SLACK_BOT_TOKEN = env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = env("SLACK_APP_TOKEN")
SLACK_ALERT_CHANNEL = env("SLACK_ALERT_CHANNEL")
SLACK_ALLOWED_USERS = {u.strip() for u in (env("SLACK_ALLOWED_USERS") or "").split(",") if u.strip()}

# Salesforce : deux modes d'authentification.
#  - JWT (recommandé) : SALESFORCE_USERNAME + SALESFORCE_CLIENT_ID + SALESFORCE_PRIVATE_KEY (chemin du fichier .key/.pem,
#    ou contenu PEM avec des \n) + SALESFORCE_INSTANCE_URL (https://login.salesforce.com | https://test.salesforce.com | My Domain)
#  - mot de passe : SF_USERNAME + SF_PASSWORD + SF_SECURITY_TOKEN + SF_DOMAIN
SF_USERNAME = env("SALESFORCE_USERNAME") or env("SF_USERNAME")
SF_PASSWORD = env("SF_PASSWORD")
SF_SECURITY_TOKEN = env("SF_SECURITY_TOKEN")
SF_CLIENT_ID = env("SALESFORCE_CLIENT_ID")
SF_PRIVATE_KEY = env("SALESFORCE_PRIVATE_KEY")
SF_INSTANCE_URL = env("SALESFORCE_INSTANCE_URL")


def _sf_domain() -> str:
    """Convertit SALESFORCE_INSTANCE_URL en 'domain' simple-salesforce (login | test | mon-domaine.my)."""
    if SF_INSTANCE_URL:
        host = SF_INSTANCE_URL.replace("https://", "").replace("http://", "").strip("/")
        return host[: -len(".salesforce.com")] if host.endswith(".salesforce.com") else host
    return env("SF_DOMAIN", "login")


SF_DOMAIN = _sf_domain()


def sf_auth() -> dict:
    """Arguments à passer à simple_salesforce.Salesforce() selon le mode configuré."""
    if SF_CLIENT_ID and SF_PRIVATE_KEY:
        auth = dict(username=SF_USERNAME, consumer_key=SF_CLIENT_ID, domain=SF_DOMAIN)
        if SF_PRIVATE_KEY.lstrip().startswith("-----"):
            auth["privatekey"] = SF_PRIVATE_KEY.replace("\\n", "\n")
        else:
            key_path = Path(SF_PRIVATE_KEY).expanduser()
            if not key_path.exists():
                raise RuntimeError(f"Clé privée Salesforce introuvable : {key_path} (SALESFORCE_PRIVATE_KEY)")
            auth["privatekey_file"] = str(key_path)
        return auth
    return dict(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_SECURITY_TOKEN, domain=SF_DOMAIN)
ARCHIVE_TO_SALESFORCE = env_bool("ARCHIVE_TO_SALESFORCE", True)
ARCHIVE_TEST_ACCOUNT_ID = env("ARCHIVE_TEST_ACCOUNT_ID")   # si renseigné, les PDF sont archivés sur ce compte Salesforce au lieu de celui du client (tests)

PORTAIL_HEADLESS = env_bool("PORTAIL_HEADLESS", True)
BROWSER_CHANNEL = env("BROWSER_CHANNEL")

GOOGLE_SA_KEY_PATH = env("GOOGLE_SA_KEY_PATH")
GOOGLE_CLIENT_SECRET_FILE = env("GOOGLE_CLIENT_SECRET_FILE", "client_secret.json")
GOOGLE_TOKEN_FILE = env("GOOGLE_TOKEN_FILE", "token.json")
TEST_RECIPIENT = env("TEST_RECIPIENT")
# Journal d'activité MySQL (OVH) — facultatif : vide = désactivé
MYSQL_HOST = env("MYSQL_HOST")
MYSQL_PORT = int(env("MYSQL_PORT", "3306"))
MYSQL_DATABASE = env("MYSQL_DATABASE")
MYSQL_USER = env("MYSQL_USER")
MYSQL_PASSWORD = env("MYSQL_PASSWORD")
MYSQL_SSL = env_bool("MYSQL_SSL", False)
MYSQL_TABLE = env("MYSQL_TABLE", "docs_fiscaux_demandes")

# Campagnes (traitement en masse) — voir campaign.py
CAMPAIGN_ADMINS = {u.strip() for u in (env("CAMPAIGN_ADMINS") or "").split(",") if u.strip()}   # IDs Slack autorisés ; vide = personne
CAMPAIGN_SF_FILTER = env("CAMPAIGN_SF_FILTER", "StatutMission__c = 'MissionValidee'")           # clients retenus (SOQL, en plus du cabinet et du SIREN)
CAMPAIGN_INVENTORY_INTERVAL_S = float(env("CAMPAIGN_INVENTORY_INTERVAL_S", "1.0"))               # délai entre deux interrogations du portail
CAMPAIGN_LIST_MIN_CLIENTS = int(env("CAMPAIGN_LIST_MIN_CLIENTS", "100"))                          # en dessous : interrogation directe compte par compte, sans récupérer les listes
CAMPAIGN_LIST_PAGE_INTERVAL_S = float(env("CAMPAIGN_LIST_PAGE_INTERVAL_S", "5"))                  # pause entre deux pages de 500 entreprises
CAMPAIGN_LIST_MAX_PAGES = int(env("CAMPAIGN_LIST_MAX_PAGES", "200"))                              # plafond de sécurité par compte (200 × 500 = 100 000 entreprises)
CAMPAIGN_SEND_INTERVAL_S = float(env("CAMPAIGN_SEND_INTERVAL_S", "5"))                           # délai entre deux envois
CAMPAIGN_DAILY_LIMIT = int(env("CAMPAIGN_DAILY_LIMIT", "1800"))                                  # envois réels max par jour (quota Gmail ≈ 2000/jour/compte)
CAMPAIGN_RESUME_HOUR = int(env("CAMPAIGN_RESUME_HOUR", "7"))                                     # heure de reprise le lendemain quand le quota est atteint
CAMPAIGN_CHANNEL = env("CAMPAIGN_CHANNEL") or SLACK_ALERT_CHANNEL                                # canal des messages de campagne et des rapports
CAMPAIGNS_DIR = DATA_DIR / "campaigns"

DRY_RUN = env_bool("DRY_RUN", False)   # global : aucun email, PDF + aperçu déposés dans Slack
DRY_RUN_CHANNEL = env("DRY_RUN_CHANNEL") or SLACK_ALERT_CHANNEL   # canal où déposer PDF + aperçu en mode test ; vide = message direct au demandeur


@dataclass
class Cabinet:
    code: str                 # = Entite_Juridique_Calc__c
    libelle: str
    portal_login: str | None
    portal_password: str | None
    sender_address: str
    reply_to: str
    signature_html: str
    active: bool = True
    telephone: str = ""          # coordonnées affichées dans la signature ({telephone} {adresse} {site_web})
    adresse: str = ""
    site_web: str = ""
    logo: str | None = None      # chemin d'une image PNG intégrée à l'email ({logo} → <img src="cid:logo">)
    template: str | None = None  # gabarit HTML propre au cabinet (défaut : templates/email.html)
    logo_url: str | None = None  # URL publique du logo, utilisée pour la copie de l'email enregistrée dans Salesforce


def load_cabinets() -> dict[str, Cabinet]:
    path = Path(env("CABINETS_FILE", str(BASE_DIR / "cabinets.json")))
    if not path.exists():
        raise RuntimeError(f"Fichier cabinets introuvable : {path} (copier cabinets.example.json)")
    raw = json.loads(path.read_text(encoding="utf-8"))
    cabinets = {}
    for code, c in raw.items():
        cabinets[code] = Cabinet(
            code=code,
            libelle=c.get("libelle", code),
            portal_login=env(c.get("portal_login_env", "")),
            portal_password=env(c.get("portal_password_env", "")),
            sender_address=c["sender_address"],
            reply_to=c.get("reply_to") or c["sender_address"],
            signature_html=c.get("signature_html", "<p>{cdm_nom}<br>{cabinet}</p>"),
            active=bool(c.get("active", True)),
            telephone=c.get("telephone", ""), adresse=c.get("adresse", ""), site_web=c.get("site_web", ""),
            logo=c.get("logo") or None, template=c.get("template") or None, logo_url=c.get("logo_url") or None,
        )
    return cabinets


@dataclass
class PortalAccount:
    """Compte portailpro personnel d'un chargé de mission : essayé quand le compte du cabinet n'est pas habilité."""
    code: str        # identifiant interne = nom du profil Chrome (ex. "collab-c.durand")
    nom: str         # libellé affiché ; vide = nom du chargé de mission dans Salesforce
    email: str       # email du chargé de mission dans Salesforce (clé de routage, en minuscules)
    login: str
    password: str


def load_collab_accounts() -> dict[str, PortalAccount]:
    """Comptes déclarés dans .env (une série par collaborateur, numérotation libre) :
         PORTAIL_COLLAB<n>_LOGIN, PORTAIL_COLLAB<n>_PASSWORD   obligatoires
         PORTAIL_COLLAB<n>_EMAIL_SF                            email Salesforce du chargé de mission (défaut : LOGIN si c'est un email)
         PORTAIL_COLLAB<n>_NOM                                 facultatif
         PORTAIL_COLLAB<n>_ACTIF                               false pour désactiver sans effacer (défaut : true)
       Renvoie {email du chargé de mission (minuscules): PortalAccount}."""
    accounts: dict[str, PortalAccount] = {}
    nums = sorted({m.group(1) for k in os.environ if (m := re.fullmatch(r"PORTAIL_COLLAB(\d+)_LOGIN", k))}, key=int)
    for num in nums:   # numéro gardé tel quel (« 01 » ≠ « 1 »)
        p = f"PORTAIL_COLLAB{num}_"
        login, password = (env(p + "LOGIN") or "").strip(), env(p + "PASSWORD") or ""
        if not (login and password) or not env_bool(p + "ACTIF", True):
            continue
        email = ((env(p + "EMAIL_SF") or "").strip() or (login if "@" in login else "")).lower()
        if not email or email in accounts:
            continue   # impossible à rattacher à un chargé de mission, ou doublon (le premier déclaré est gardé)
        accounts[email] = PortalAccount(code=collab_code(email), nom=(env(p + "NOM") or "").strip(), email=email,
                                        login=login, password=password)
    return accounts


def collab_code(email: str) -> str:
    """Nom de session (profil Chrome) d'un compte collaborateur : fonction du SEUL email — jamais de l'ordre de
    déclaration ni des comptes désactivés — pour qu'une session ne passe jamais d'un collaborateur à un autre.
    « collab-<partie locale> » pour une adresse @keobiz.fr simple (ex. collab-c.durand),
    sinon suffixé d'une empreinte de l'email complet (unicité garantie)."""
    local, _, domain = email.partition("@")
    slug = re.sub(r"[^a-z0-9.-]+", "-", local).strip("-")
    if slug and slug == local and domain == "keobiz.fr":
        return f"collab-{slug}"
    return f"collab-{slug or 'compte'}-{hashlib.sha1(email.encode('utf-8')).hexdigest()[:8]}"


def check_required() -> None:
    missing = [n for n in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not env(n)]
    if not SF_USERNAME:
        missing.append("SALESFORCE_USERNAME")
    if not (SF_CLIENT_ID and SF_PRIVATE_KEY) and not (SF_PASSWORD and SF_SECURITY_TOKEN):
        missing.append("SALESFORCE_CLIENT_ID + SALESFORCE_PRIVATE_KEY (ou SF_PASSWORD + SF_SECURITY_TOKEN)")
    if missing:
        raise RuntimeError("Variables manquantes dans .env : " + ", ".join(missing))
    for d in (DATA_DIR, FILES_DIR, PROFILES_DIR, CAMPAIGNS_DIR):
        d.mkdir(parents=True, exist_ok=True)
