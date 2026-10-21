"""
Client portailpro.gouv.fr

Principe :
  1. Playwright ouvre Chrome sur /ms/avis_imposition et se connecte avec les identifiants du cabinet
     (ou attend qu'un humain le fasse si aucun identifiant n'est fourni / si un second facteur apparaît).
  2. On écoute le trafic réseau : dès qu'un appel vers services.portailpro.gouv.fr part avec un
     en-tête Authorization (Bearer ...), on le mémorise, ainsi que les paramètres rattachement*.
  3. Ensuite on appelle l'API directement en HTTP, sans plus toucher à l'interface :
       GET /api-declaration-pcr/v1/declarations/listerDocumentsEnsu?siren=...
           → {"resultat": [{libelle, annee, typeAvis, numeroFiscal, referenceAvis, montant, commune,
                             idEnsuaAvisPrincipal, idEnsuaAvisSuite, topLectureAvisPrincipal, ...}], "infos": []}
       GET /api-declaration-pcr/v1/declarations/consulterDocumentEnsu?siren=...&idEnsua=...
           → le PDF en binaire (content-type annoncé "application/json", mais le corps commence par %PDF)
  4. Si l'API répond 401 (jeton expiré), on recharge la page pour obtenir un jeton frais.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import httpx

import config
import logging
from playwright.sync_api import Request, sync_playwright

log = logging.getLogger("portailpro")

API_BASE = "https://services.portailpro.gouv.fr"
LIST_PATH = "/api-declaration-pcr/v1/declarations/listerDocumentsEnsu"
DOC_PATH = "/api-declaration-pcr/v1/declarations/consulterDocumentEnsu"
START_URL = "https://portailpro.gouv.fr/ms/avis_imposition"
# Liste des entreprises accessibles par le compte, paginée (le site l'appelle lui-même par pages de 20 ; 500 accepté) :
#   GET /pcr-habilitations/v1/habilitations/{identifiant propre au compte}?size=500&page=N&filter=siren&rattachement…
#   → {"sirens": [{"siren": "...", "rs": "..."}], "erreurs": [...]}
HABILITATIONS_PREFIX = "/pcr-habilitations/v1/habilitations/"
# Le site n'appelle pas cette liste sur l'écran « Rattachement de compte » qu'il impose, à chaque connexion depuis un
# navigateur neuf, aux comptes récemment rattachés (constaté le 15/10/2026 sur le compte d'un chargé de mission). Il appelle en
# revanche, sur toutes les pages, celui-ci — avec le MÊME identifiant que la liste (vérifié le 15/10/2026) :
#   GET /pcr-habilitations/compte-technique/v2/comptes/utilisateur/{identifiant}
COMPTE_UTILISATEUR = re.compile(r"^/pcr-habilitations/compte-technique/v\d+/comptes/utilisateur/"
                                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
RATTACHEMENT_KEYS = ("rattachementDgfip", "rattachementAcoss", "rattachementDouane")


class PortailProError(RuntimeError):
    pass


class NonHabiliteError(PortailProError):
    """Le compte n'est pas habilité pour ce SIREN (le client ne lui a pas donné accès) : ce n'est PAS « aucun document »."""


class PortailMaintenanceError(PortailProError):
    """Le portail répond par sa page de maintenance (HTTP 200 + HTML, même sur l'API)."""


class PortailLoginRefused(PortailProError):
    """Identifiant ou mot de passe refusé : on ne réessaie jamais (plusieurs échecs peuvent bloquer le compte)."""


class PortailLoginAbandoned(PortailProError):
    """Connexions en échec à répétition avec ces identifiants (code de vérification, portail…) : pause jusqu'à `until`."""

    def __init__(self, message: str, until: float | None = None):
        super().__init__(message)
        self.until = until


# Réponse de listerDocumentsEnsu pour un SIREN hors habilitations du compte (constaté le 14/10/2026) :
#   HTTP 200, "resultat": [], "infos": [{"infoCode": "131105", "infoMessage": "Vous n'êtes pas habilité à voir cette fonctionnalité"}]
NON_HABILITE_CODES = {"131105"}
# Codes « erreurs » sans effet sur la liste des entreprises (50003 = message générique Insee sur dénomination/adresse).
BENIGN_HABILITATION_ERRORS = {"50003"}
# Échecs de connexion consécutifs tolérés pour un même identifiant ; au-delà, une seule tentative par LOGIN_COOLDOWN_S.
MAX_FAILED_LOGINS, LOGIN_COOLDOWN_S = 2, 3600
LOGIN_REFUSED = re.compile(r"mot de passe est incorrect|courriel et/ou votre mot de passe", re.I)
MAINTENANCE = re.compile(r"maintenance|indisponible", re.I)


# Identifiants refusés, tous threads confondus (worker, campagnes) : jamais resoumis — y compris lors d'un renouvellement
# de jeton, ni après un redémarrage (conservés dans data/, sous forme d'empreinte HMAC : jamais le mot de passe). Un mot de
# passe CORRIGÉ (Google Sheet ou .env) a une autre empreinte : il est essayé. Pour réessayer un mot de passe inchangé,
# supprimer data/portail-identifiants-refuses.json.
_REFUSED: set[tuple[str, str]] = set()
_REFUSED_LOADED = False
_KEY_LOCK = threading.Lock()
_HMAC_KEY: bytes | None = None
# Échecs de connexion consécutifs par identifiant (code de vérification demandé, portail en panne…), toutes sessions
# confondues : une session neuve ne remet pas le compteur à zéro. → (nombre, heure du dernier échec)
_FAILED: dict[tuple[str, str], tuple[int, float]] = {}


def _private_write(path: Path, data: bytes) -> None:
    """Écriture atomique et durable (fichier temporaire unique, fsync, remplacement), lisible par le seul utilisateur."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _hmac_key() -> bytes:
    """Clé locale (data/, jamais dans le dépôt) des empreintes d'identifiants."""
    global _HMAC_KEY
    with _KEY_LOCK:
        if _HMAC_KEY is None:
            path = config.DATA_DIR / ".cle-empreintes"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                _private_write(path, secrets.token_hex(32).encode())
            _HMAC_KEY = path.read_bytes().strip()
        return _HMAC_KEY


def _credentials_key(login: str | None, password: str | None) -> tuple[str, str]:
    """(identifiant normalisé, empreinte HMAC du mot de passe) : jamais le mot de passe lui-même."""
    return ((login or "").strip().lower(), hmac.new(_hmac_key(), (password or "").encode("utf-8"), hashlib.sha256).hexdigest())


def _refused_file() -> Path:
    return config.DATA_DIR / "portail-identifiants-refuses.json"


_REFUSED_STAMP: tuple | None = None   # (date de modification, taille) du fichier au dernier chargement


def _load_refused() -> None:
    """Ajoute à la mémoire les refus du fichier s'il a changé (autre processus : --test-compte, scripts de test).
    Fichier illisible : mis de côté (jamais écrasé) et signalé."""
    global _REFUSED_LOADED, _REFUSED_STAMP
    path = _refused_file()
    with _KEY_LOCK:
        try:
            st = path.stat()
        except FileNotFoundError:
            _REFUSED_LOADED = True
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if _REFUSED_LOADED and stamp == _REFUSED_STAMP:
            return
        _REFUSED_LOADED, _REFUSED_STAMP = True, stamp
        try:
            _REFUSED.update(tuple(x[:2]) for x in json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            aside = path.with_name(f"{path.name}.illisible-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                path.rename(aside)
            except OSError:
                pass
            log.error("Fichier des identifiants refusés illisible (%s) : mis de côté (%s). Les refus déjà en mémoire "
                      "restent appliqués ; vérifier les comptes concernés.", type(e).__name__, aside.name)


def is_refused(login: str | None, password: str | None) -> bool:
    _load_refused()
    return _credentials_key(login, password) in _REFUSED


def mark_refused(login: str | None, password: str | None) -> None:
    global _REFUSED_STAMP
    key = _credentials_key(login, password)
    _load_refused()   # union avec les refus notés entre-temps par un autre processus
    with _KEY_LOCK:
        _REFUSED.add(key)
        try:
            _private_write(_refused_file(), json.dumps(sorted(_REFUSED)).encode("utf-8"))
            st = _refused_file().stat()
            _REFUSED_STAMP = (st.st_mtime_ns, st.st_size)
        except Exception as e:
            log.warning("Impossible de mémoriser l'identifiant refusé sur le disque (%s)", type(e).__name__)


# Une seule connexion à la fois par identifiant, tous threads confondus (worker + campagnes) : un mot de passe refusé
# pendant qu'un autre thread attendait n'est jamais soumis une seconde fois (revérification sous le verrou).
_LOGIN_LOCKS: dict[str, threading.Lock] = {}


def _login_lock(login: str | None) -> threading.Lock:
    with _KEY_LOCK:
        return _LOGIN_LOCKS.setdefault((login or "").strip().lower(), threading.Lock())


def login_blocked_until(login: str | None, password: str | None) -> float | None:
    """Heure (timestamp) avant laquelle aucune connexion ne sera tentée avec ces identifiants, ou None."""
    n, last = _FAILED.get(_credentials_key(login, password), (0, 0.0))
    if n >= MAX_FAILED_LOGINS and time.time() < last + LOGIN_COOLDOWN_S:
        return last + LOGIN_COOLDOWN_S
    return None


def non_habilite_message(data: Any) -> str | None:
    """Message du portail si le compte n'est pas habilité pour le SIREN interrogé, sinon None."""
    if not isinstance(data, dict) or data.get("resultat"):
        return None
    for info in data.get("infos") or []:
        if isinstance(info, dict) and str(info.get("infoCode")) in NON_HABILITE_CODES:
            return info.get("infoMessage") or "non habilité"
    return None


def json_or_maintenance(r: httpx.Response) -> Any:
    """Corps JSON de la réponse ; page HTML de maintenance → PortailMaintenanceError (et non une erreur obscure)."""
    try:
        return r.json()
    except ValueError:
        if MAINTENANCE.search(r.text or ""):
            raise PortailMaintenanceError("le portail impots est en maintenance, réessayer plus tard") from None
        raise PortailProError(f"réponse inattendue du portail (HTTP {r.status_code}, non JSON)") from None


# L'API synchrone de Playwright n'accepte qu'UN moteur par thread : les sessions d'un même thread (compte du cabinet
# + comptes des chargés de mission) le partagent, chacune avec son propre profil Chrome (cookies, connexion).
_tls = threading.local()


def _acquire_playwright():
    if getattr(_tls, "pw", None) is None:
        _tls.pw, _tls.users = sync_playwright().start(), 0
    _tls.users += 1
    return _tls.pw


def _release_playwright() -> None:
    _tls.users = max(0, getattr(_tls, "users", 1) - 1)
    if _tls.users == 0 and getattr(_tls, "pw", None) is not None:
        try:
            _tls.pw.stop()
        finally:
            _tls.pw = None


@dataclass
class Avis:
    libelle: str          # "Cotisation foncière des entreprises", "Taxe foncière", ...
    annee: str            # "2025"
    type_avis: str        # "Avis détaillé", ...
    siret: str            # numeroFiscal
    reference: str        # referenceAvis
    commune: str
    montant: float | None
    id_principal: str     # idEnsuaAvisPrincipal → PDF de l'avis
    id_suite: str | None  # idEnsuaAvisSuite → PDF complémentaire éventuel
    deja_lu: bool         # topLectureAvisPrincipal == 1

    @property
    def is_cfe(self) -> bool:
        return "cotisation fonci" in self.libelle.lower()


class PortailPro:
    def __init__(self, profile_dir: str | Path = "chrome-profile", headless: bool = True,
                 channel: str | None = "chrome", login: str | None = None, password: str | None = None):
        self.login_id, self.password = login, password
        self._closed = False
        self._ctx = None
        self._pw = _acquire_playwright()
        kwargs: dict[str, Any] = dict(headless=headless, viewport={"width": 1280, "height": 900})
        try:
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(str(profile_dir), channel=channel, **kwargs)
            except Exception:
                # Chrome/Edge absent : on retombe sur le Chromium livré avec Playwright
                self._ctx = self._pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
            self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self.token: str | None = None
            self.habilitations_path: str | None = None   # relevé dans le trafic du site (propre au compte)
            self.habilitations_fallback: str | None = None   # déduit de l'identifiant du compte (secours)
            self.rattachements = {"rattachementDgfip": "true", "rattachementAcoss": "false", "rattachementDouane": "false"}
            self._ctx.on("request", self._on_request)
            self._http = httpx.Client(base_url=API_BASE, timeout=60)
        except Exception:
            # ouverture interrompue : rien ne doit rester ouvert (profil verrouillé, moteur partagé compté à tort)
            self._closed = True
            if self._ctx is not None:
                try:
                    self._ctx.close()
                except Exception:
                    pass
            _release_playwright()
            raise

    # ------------------------------------------------------------------ session
    def _on_request(self, req: Request) -> None:
        if not req.url.startswith(API_BASE):
            return
        path = urlparse(req.url).path
        if path.startswith(HABILITATIONS_PREFIX):
            self.habilitations_path = path
        elif m := COMPTE_UTILISATEUR.match(path):
            self.habilitations_fallback = HABILITATIONS_PREFIX + m.group(1)
        auth = req.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            self.token = auth  # toujours le plus récent (le SPA renouvelle le jeton lui-même)
        qs = parse_qs(urlparse(req.url).query)
        for k in RATTACHEMENT_KEYS:
            if k in qs:
                self.rattachements[k] = qs[k][0]

    def login(self, timeout_s: int = 300) -> None:
        """Ouvre le portail ; saisit les identifiants si le formulaire apparaît ; attend le premier jeton.
        Une seule connexion à la fois par identifiant (tous threads) : les vérifications se font sous le verrou."""
        with _login_lock(self.login_id):
            self._login(timeout_s)

    def _login(self, timeout_s: int) -> None:
        if self.login_id and self.password and is_refused(self.login_id, self.password):
            raise PortailLoginRefused("identifiants déjà refusés par le portail — aucune nouvelle tentative tant que le "
                                      "mot de passe n'est pas corrigé (Google Sheet des accès ou .env)")
        until = login_blocked_until(self.login_id, self.password)
        if until:
            raise PortailLoginAbandoned(f"connexion suspendue jusqu'à {time.strftime('%H:%M', time.localtime(until))} après "
                                        f"{MAX_FAILED_LOGINS} échecs consécutifs (code de vérification ? portail ?) — "
                                        "protection contre le blocage du compte", until)
        key = _credentials_key(self.login_id, self.password)
        try:
            self.token = None
            self.page.goto(START_URL, wait_until="domcontentloaded")
            if self._login_form_visible(timeout_ms=15000):
                if not (self.login_id and self.password):
                    print("→ Formulaire de connexion affiché : connectez-vous dans la fenêtre Chrome...")
                else:
                    self._fill_login_form()
            self._wait_token(timeout_s if not (self.login_id and self.password) else 90)
        except Exception as e:
            _FAILED[key] = (_FAILED.get(key, (0, 0.0))[0] + 1, time.time())
            if isinstance(e, PortailProError):
                raise
            # Erreur du navigateur : son « Call log » recopie les saisies (fill("<mot de passe>")) → message nettoyé,
            # exception d'origine NON chaînée (log.exception l'afficherait).
            raise PortailProError(f"connexion impossible ({type(e).__name__}: {self._scrub(str(e))})") from None
        _FAILED.pop(key, None)

    def _scrub(self, text: str) -> str:
        """Message d'erreur sans journal d'appels Playwright ni mot de passe."""
        text = text.split("Call log:")[0]
        if self.password:
            text = text.replace(self.password, "***")
        return re.sub(r"\s+", " ", text).strip()[:300]

    def _login_form_visible(self, timeout_ms: int) -> bool:
        try:
            self.page.wait_for_selector("input[type='password']", state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _dismiss_cookie_banner(self) -> None:
        """Neutralise le bandeau de consentement (tarteaucitron). Tant qu'il est ouvert, le site marque le reste de la
        page `inert` : les champs ignorent toute saisie. On retire cet attribut et on masque le bandeau, SANS cliquer sur
        « Tout refuser » (ce bouton recharge la page de façon différée et effacerait la saisie)."""
        page = self.page
        try:
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        try:
            page.evaluate("""() => {
                document.querySelectorAll('[inert]').forEach(e => e.removeAttribute('inert'));
                for (const id of ['tarteaucitronRoot', 'tarteaucitronAlertBig', 'tarteaucitronBack', 'tarteaucitronPercentage']) {
                    const el = document.getElementById(id); if (el) el.style.display = 'none';
                }
                document.body.classList.remove('tarteaucitron-modal-open');
                document.body.style.overflow = '';
            }""")
        except Exception:
            pass

    def _accept_cookie_banner_and_reload(self) -> None:
        """Secours : cliquer « Tout refuser » (recharge la page) puis attendre que le formulaire soit de nouveau prêt."""
        page = self.page
        for sel in ("#tarteaucitronAllDenied2", "#tarteaucitronAllDenied", "button:has-text('Tout refuser')"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=3000)
                    break
            except Exception:
                continue
        page.wait_for_timeout(2000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

    def _wait_form_ready(self) -> None:
        """Formulaire de connexion affiché, sans voile « Chargement en cours… » par-dessus."""
        self.page.wait_for_selector("input[type='password']", state="visible", timeout=30000)
        try:
            self.page.wait_for_selector("text=Chargement en cours", state="hidden", timeout=15000)
        except Exception:
            pass
        self.page.wait_for_timeout(300)

    def _find_ident(self):
        for sel in ("input[type='email']", "input[autocomplete='username']", "input[name*='user' i]",
                    "input[id*='dentifiant' i]", "input[type='text']"):
            loc = self.page.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        raise PortailProError("Champ identifiant introuvable sur la page de connexion")

    def _fill_login_form(self) -> None:
        page = self.page
        self._dismiss_cookie_banner()
        for attempt in (1, 2, 3):
            self._wait_form_ready()
            ident = self._find_ident()
            pwd = page.locator("input[type='password']").first
            ident.fill(self.login_id)
            pwd.fill(self.password)
            page.wait_for_timeout(300)
            if ident.input_value() != self.login_id or not pwd.input_value():
                log.warning("portailpro : saisie refusée par le formulaire (bandeau cookies ?), nouvelle tentative %d", attempt)
                self._dismiss_cookie_banner()
                if attempt >= 2:
                    self._accept_cookie_banner_and_reload()
                continue
            btn = page.get_by_role("button", name=re.compile("connexion|se connecter|valider", re.I)).first
            if btn.count():
                try:
                    btn.click(timeout=10000)
                except Exception:
                    self._dismiss_cookie_banner()
                    btn.click(force=True)
            else:
                pwd.press("Enter")
            # Connexion réussie dès qu'un jeton API est capturé ou que le formulaire a disparu (hors page de login).
            deadline = time.time() + 60   # un seul envoi par connexion : on lui laisse le temps d'aboutir
            while time.time() < deadline:
                if self.token is not None:
                    return
                if self._login_refused():
                    mark_refused(self.login_id, self.password)
                    raise PortailLoginRefused("identifiant ou mot de passe refusé par le portail — aucune nouvelle "
                                              "tentative (plusieurs échecs peuvent bloquer le compte)")
                if not self._login_form_visible(timeout_ms=1000) and not re.search(r"/ms/login", page.url):
                    return
                page.wait_for_timeout(1000)
            # Formulaire toujours là après l'envoi : on ne renvoie JAMAIS le mot de passe dans la même connexion (un refus
            # dont le message n'aurait pas été reconnu compterait double et rapprocherait le compte du blocage).
            log.warning("portailpro : formulaire toujours affiché après l'envoi (tentative %d) : pas de nouvel envoi", attempt)
            break
        shot = config.DATA_DIR / f"portail-echec-connexion-{time.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            shot = None
        raise PortailProError("Connexion refusée (identifiants ?) ou étape supplémentaire (MFA) : intervention manuelle nécessaire"
                              + (f" — capture : {shot}" if shot else ""))

    def _login_refused(self) -> bool:
        try:
            return bool(LOGIN_REFUSED.search(self.page.inner_text("body", timeout=2000)))
        except Exception:
            return False

    def ensure_session(self) -> None:
        """À appeler avant chaque lot d'appels : (re)connecte si nécessaire."""
        if self.token is None:
            self.login()

    def refresh_token(self) -> None:
        """Jeton expiré : recharge la page (session cookie encore valide → nouveau jeton) sinon reconnexion."""
        self.token = None
        self.page.reload(wait_until="domcontentloaded")
        if self._login_form_visible(timeout_ms=8000):
            self.login()
        else:
            self._wait_token(60)

    def _wait_token(self, timeout_s: int, msg: str | None = None) -> None:
        if msg:
            print(msg, flush=True)
        deadline = time.time() + timeout_s
        while self.token is None:
            if time.time() > deadline:
                raise PortailProError("Aucun jeton capturé : la connexion n'a pas abouti.")
            self.page.wait_for_timeout(500)  # laisse tourner la boucle d'événements Playwright

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._http.close()
            self._ctx.close()
        finally:
            _release_playwright()

    # ---------------------------------------------------------------------- API
    def _get(self, path: str, params: dict, accept: str) -> httpx.Response:
        r = None
        refreshed = False
        for attempt in range(1, 5):
            headers = {
                "Authorization": self.token or "",
                "Accept": accept,
                "X-Correlation-ID": str(uuid.uuid4()),
            }
            try:
                r = self._http.get(path, params={**params, **self.rattachements}, headers=headers)
            except httpx.TransportError as e:   # coupure réseau, délai dépassé : on réessaie
                if attempt == 4:
                    raise
                log.warning("portailpro %s : %s, nouvel essai %d/3 dans %ds", path, e, attempt, 3 * attempt)
                time.sleep(3 * attempt)
                continue
            if r.status_code == 401 and not refreshed:
                refreshed = True
                self.refresh_token()
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 4:
                # indisponibilité passagère du portail : attente 3 s, 6 s, 9 s ; jeton rafraîchi au 2e échec
                log.warning("portailpro %s : HTTP %d, nouvel essai %d/3 dans %ds", path, r.status_code, attempt, 3 * attempt)
                time.sleep(3 * attempt)
                if attempt == 2 and not refreshed:
                    refreshed = True
                    self.refresh_token()
                continue
            return r
        return r  # type: ignore[return-value]

    def list_avis(self, siren: str) -> list[Avis]:
        """Tous les avis d'imposition (CFE, TF, ...) visibles pour un SIREN."""
        r = self._get(LIST_PATH, {"siren": siren}, "application/json, text/plain, */*")
        if r.status_code == 403:   # « non habilité » seulement si le portail le dit (131105) : sinon limitation / blocage
            try:
                msg = non_habilite_message(r.json())
            except ValueError:
                msg = None
            if msg:
                raise NonHabiliteError(f"SIREN {siren} : compte non habilité ({msg})")
            raise PortailProError(f"SIREN {siren} : accès refusé par le portail (HTTP 403) — limitation ou blocage possible")
        r.raise_for_status()
        data = json_or_maintenance(r)
        msg = non_habilite_message(data)
        if msg:
            raise NonHabiliteError(f"SIREN {siren} : compte non habilité ({msg})")
        avis = []
        for x in data.get("resultat") or []:
            if not x.get("idEnsuaAvisPrincipal"):
                continue
            avis.append(Avis(
                libelle=x.get("libelle") or "", annee=str(x.get("annee") or ""), type_avis=x.get("typeAvis") or "",
                siret=x.get("numeroFiscal") or "", reference=x.get("referenceAvis") or "", commune=x.get("commune") or "",
                montant=x.get("montant"), id_principal=x["idEnsuaAvisPrincipal"], id_suite=x.get("idEnsuaAvisSuite") or None,
                deja_lu=x.get("topLectureAvisPrincipal") == 1,
            ))
        return avis

    def block_habilitation_calls(self) -> None:
        """Bloque, AVANT envoi, les appels « liste des entreprises » que le site lance lui-même (pages de 20) : on n'en
        garde que l'adresse propre au compte (relevée par _on_request), puis list_habilitations() lit la liste par 500."""
        self._ctx.route(re.compile(r".*/pcr-habilitations/.*"), lambda route: route.abort())

    def list_habilitations(self, page_size: int = 500, pause_s: float = 5.0, max_pages: int = 200,
                           should_stop=None) -> tuple[set[str], bool]:
        """SIREN accessibles par le compte → (sirens, complète). « Complète » seulement si toutes les pages ont été lues
        sans anomalie : plafond de pages, arrêt demandé (should_stop), erreur signalée par le portail ou réponse anormale
        après la 1re page → SIREN déjà lus (utilisables pour trouver des clients) mais complète=False (une absence n'y
        prouve rien). Réponse anormale dès la 1re page → PortailProError."""
        fallback = lambda: getattr(self, "habilitations_fallback", None)   # noqa: E731
        if not (self.habilitations_path or fallback()):   # rien vu : on (re)charge la page, le site appelle de lui-même
            self.page.goto(START_URL, wait_until="domcontentloaded")
            deadline = time.time() + 30
            while not (self.habilitations_path or fallback()) and time.time() < deadline:
                self.page.wait_for_timeout(500)
        path = self.habilitations_path or fallback()   # l'adresse vue telle quelle prime sur celle déduite
        if not path:
            raise PortailProError("adresse de la liste des entreprises du compte introuvable (appel du site non observé)")
        if path != self.habilitations_path:
            log.info("Liste des entreprises : adresse déduite de l'identifiant du compte (écran « Rattachement » affiché)")
        sirens: set[str] = set()
        flagged = False        # erreur inconnue signalée par le portail : SIREN utilisables, absence non probante
        reread_empty = True    # 1re page vide : relue une fois avant d'y croire
        page = 1
        while page <= max_pages:
            if should_stop and should_stop():
                return sirens, False
            try:
                r = self._get(path, {"size": str(page_size), "page": str(page), "filter": "siren"},
                              "application/json, text/plain, */*")
                r.raise_for_status()
                data = json_or_maintenance(r)
                raw = data.get("sirens") if isinstance(data, dict) else None
                if not isinstance(raw, list):   # pas de liste exploitable : surtout ne pas conclure « fin de liste »
                    raise PortailProError(f"liste des entreprises : réponse inattendue page {page} (pas de liste « sirens »)")
            except (PortailLoginRefused, PortailLoginAbandoned):
                raise
            except Exception as e:   # portail, réseau ou navigateur (reconnexion en échec) : la partie lue reste utile
                if page == 1:
                    raise
                log.warning("Liste des entreprises interrompue page %d (%s) : liste partielle", page, e)
                return sirens, False
            errs = data.get("erreurs") or []
            unknown = ([e for e in errs if not (isinstance(e, dict) and str(e.get("code")) in BENIGN_HABILITATION_ERRORS)]
                       if isinstance(errs, list) else [errs])
            if unknown and not flagged:
                flagged = True
                log.warning("Liste des entreprises : erreur signalée par le portail page %d (%s) : liste jugée incomplète",
                            page, str(unknown)[:300])
            if page == 1 and not raw and reread_empty:
                reread_empty = False
                time.sleep(pause_s)
                continue
            before = len(sirens)
            for x in raw:
                s = re.sub(r"\D", "", str(x.get("siren"))) if isinstance(x, dict) and x.get("siren") else ""
                if len(s) == 9:
                    sirens.add(s)
            if len(raw) < page_size:   # longueur BRUTE de la page : une entrée invalide ne tronque pas la liste
                return sirens, not flagged
            if len(sirens) == before:   # page pleine sans aucun nouveau SIREN : le portail ignore la pagination
                log.warning("Liste des entreprises : page %d identique aux précédentes : liste partielle", page)
                return sirens, False
            time.sleep(pause_s)
            page += 1
        return sirens, False

    def list_documents_raw(self, siren: str) -> Any:
        """Réponse JSON brute (mode --probe)."""
        r = self._get(LIST_PATH, {"siren": siren}, "application/json, text/plain, */*")
        r.raise_for_status()
        return r.json()

    def download_document(self, siren: str, id_ensua: str) -> bytes:
        r = self._get(DOC_PATH, {"siren": siren, "idEnsua": id_ensua}, "application/pdf, application/json, */*")
        r.raise_for_status()
        return extract_pdf(r)


# ------------------------------------------------------------------ utilitaires
def iter_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)


def extract_pdf(r: httpx.Response) -> bytes:
    """consulterDocumentEnsu renvoie le PDF brut (content-type erroné) ; on garde les cas JSON+base64 par prudence."""
    ct = r.headers.get("content-type", "").lower()
    body = r.content
    if body.startswith(b"%PDF") or "pdf" in ct or "octet-stream" in ct:
        return body
    if "html" in ct and MAINTENANCE.search(r.text or ""):
        raise PortailMaintenanceError("le portail impots est en maintenance, réessayer plus tard")
    if "json" in ct or body[:1] in (b"{", b"["):
        data = r.json()
        for s in iter_strings(data):
            if len(s) > 500:
                try:
                    b = base64.b64decode(s, validate=False)
                except Exception:
                    continue
                if b.startswith(b"%PDF"):
                    return b
        raise PortailProError("Réponse JSON sans PDF base64 : " + json.dumps(data, ensure_ascii=False)[:300])
    try:
        b = base64.b64decode(body, validate=False)
        if b.startswith(b"%PDF"):
            return b
    except Exception:
        pass
    raise PortailProError(f"Format de document inattendu (content-type={ct!r}, début={body[:40]!r})")
