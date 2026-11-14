"""Envoi Gmail au nom du chargé de mission, depuis l'adresse générique du cabinet, Reply-To = Email-to-Case.

Deux modes d'authentification :
- GOOGLE_SA_KEY_PATH renseigné → compte de service + délégation domain-wide, qui impersonne cabinet.sender_address
- sinon → OAuth utilisateur (client_secret.json / token.json) : l'email part du compte Gmail autorisé (tests)

Pour la CFE, le corps et l'objet dépendent du cas de paiement lu dans l'avis (gabarits templates/cfe_<cas>.html).
L'appelant (worker) passe l'analyse de l'avis via `analyse` ; sans elle, on revient au gabarit générique.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
import re
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from config import Cabinet

log = logging.getLogger("mailer")

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
CIVILITES = {"M": "Monsieur", "MR": "Monsieur", "MME": "Madame", "MLLE": "Madame"}

# Gabarit (templates/cfe_<fichier>.html) et objet par cas de paiement CFE.
CFE_CAS = {
    "RIEN_A_PAYER":        ("cfe_rien_a_payer.html",        "✅ Votre avis de CFE {annee} est disponible"),
    "A_PAYER":             ("cfe_a_payer.html",             "📢 Votre avis de CFE {annee} est disponible - action requise"),
    "PRELEVEMENT_ECHEANCE":("cfe_prelevement_echeance.html","📢 Votre avis de CFE {annee} est disponible"),
    "MENSUALISE":          ("cfe_mensualise.html",          "📢 Votre avis de CFE {annee} est disponible"),
}


def _fmt_montant(v) -> str:
    """1789.0 → '1 789,00' (format français). Chaîne vide si None."""
    try:
        s = f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return ""
    return s.replace(",", " ").replace(".", ",")   # 1,789.00 → 1 789,00


class Mailer:
    def __init__(self, sa_key_path: str | None, client_secret_file: str, token_file: str,
                 template_file: Path, test_recipient: str | None = None):
        self.sa_key_path, self.client_secret_file, self.token_file = sa_key_path, client_secret_file, token_file
        self.template = template_file.read_text(encoding="utf-8")
        self.templates_dir = template_file.parent               # dossier des gabarits CFE
        self.test_recipient = test_recipient or None
        self._services: dict[str, object] = {}
        self._cfe_cache: dict[str, str] = {}

    # ------------------------------------------------------------ auth
    def _service(self, sender_address: str):
        from googleapiclient.discovery import build

        key = sender_address if self.sa_key_path else "oauth"
        if key not in self._services:
            if self.sa_key_path:
                from google.oauth2 import service_account
                creds = service_account.Credentials.from_service_account_file(
                    self.sa_key_path, scopes=[GMAIL_SEND_SCOPE]).with_subject(sender_address)
            else:
                creds = _oauth_credentials(self.client_secret_file, self.token_file)
            self._services[key] = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._services[key]

    # ------------------------------------------------------------ contenu
    def _cfe_template(self, fichier: str) -> str | None:
        if fichier not in self._cfe_cache:
            p = self.templates_dir / fichier
            self._cfe_cache[fichier] = p.read_text(encoding="utf-8") if p.exists() else None
            if self._cfe_cache[fichier] is None:
                log.warning("Gabarit CFE %s introuvable, gabarit générique utilisé", p)
        return self._cfe_cache[fichier]

    def render(self, req: dict, cabinet: Cabinet, analyse=None) -> tuple[str, str]:
        """(objet, corps HTML). Pour la CFE avec un cas connu, gabarit et objet selon le cas de paiement."""
        civ = CIVILITES.get((req.get("client_civilite") or "").upper().strip("."), "")
        logo_path = self._logo_path(cabinet)
        logo_html = ('<img src="cid:logo" alt="{0}" width="200" style="display:block;width:200px;height:auto;border:0;">'
                     .format(cabinet.libelle) if logo_path else f"<strong>{cabinet.libelle}</strong>")
        signature = cabinet.signature_html.format_map(_Safe(
            cdm_nom=req.get("cdm_nom") or cabinet.libelle, cdm_email=req.get("cdm_email") or cabinet.sender_address,
            cdm_telephone=req.get("cdm_telephone") or "", cabinet=cabinet.libelle,
            telephone=req.get("cdm_telephone") or cabinet.telephone, adresse=cabinet.adresse, site_web=cabinet.site_web,
            reply_to=cabinet.reply_to, sender=cabinet.sender_address, logo=logo_html))

        # gabarit et objet : spécifiques au cas de paiement CFE si disponible, sinon génériques
        cas = getattr(analyse, "cas", None) if analyse is not None else None
        template, subject = self.template, None
        if req.get("document_type") == "CFE" and cas in CFE_CAS:
            fichier, objet_modele = CFE_CAS[cas]
            t = self._cfe_template(fichier)
            if t is not None:
                template = t
                subject = objet_modele.format(annee=req.get("annee") or "") + f" - {req['client_name']}"

        nom = (req.get("client_nom") or "").strip()
        salutation = (f"{civ} {nom} ".strip() + " ") if (civ or nom) else ""   # "Monsieur Dupont " / "" ; l'espace avant 👋
        champs = dict(
            salutation=salutation,
            civilite=civ, client_nom=req.get("client_nom") or "", client_prenom=req.get("client_prenom") or "",
            client_name=req.get("client_name") or "", siren=req.get("siren") or "", annee=req.get("annee") or "",
            signature=signature, cabinet=cabinet.libelle,
            montant=_fmt_montant(getattr(analyse, "montant_a_payer", None)) if analyse is not None else "",
            solde=_fmt_montant(getattr(analyse, "solde_restant", None)) if analyse is not None else "",
            date_limite=(getattr(analyse, "date_limite", None) or "") if analyse is not None else "",
            date_prelevement=(getattr(analyse, "date_prelevement", None) or "") if analyse is not None else "")
        html = template.format_map(_Safe(**champs))
        if not civ and not req.get("client_nom"):
            html = html.replace("Bonjour ,", "Bonjour,").replace("Bonjour  👋", "Bonjour 👋")

        if subject is None:
            subject = f"{DOC_SUBJECTS.get(req['document_type'], req['document_type'])} {req['annee']} – {req['client_name']}"
        return subject, html

    def _template_for(self, cabinet: Cabinet) -> str:
        """Gabarit du cabinet (cabinets.json → "template") ou gabarit par défaut. Conservé pour compatibilité."""
        if not cabinet.template:
            return self.template
        p = Path(cabinet.template)
        if not p.is_absolute():
            p = Path(__file__).parent / p
        if not p.exists():
            log.warning("Gabarit %s introuvable pour %s, gabarit par défaut utilisé", p, cabinet.code)
            return self.template
        return p.read_text(encoding="utf-8")

    @staticmethod
    def _logo_path(cabinet: Cabinet) -> Path | None:
        if not cabinet.logo:
            return None
        p = Path(cabinet.logo)
        if not p.is_absolute():
            p = Path(__file__).parent / p
        return p if p.exists() else None

    def preview(self, req: dict, cabinet: Cabinet, analyse=None) -> str:
        """Texte lisible de ce qui serait envoyé (mode test)."""
        subject, html = self.render(req, cabinet, analyse)
        return (f"De : {req.get('cdm_nom') or cabinet.libelle} <{cabinet.sender_address}>\n"
                f"À : {req['client_email']}\nRépondre à : {cabinet.reply_to}\nObjet : {subject}\n\n{_to_text(html)}")

    # ------------------------------------------------------------ envoi
    def send(self, req: dict, cabinet: Cabinet, files: list[Path], analyse=None) -> "SentEmail":
        subject, html = self.render(req, cabinet, analyse)
        msg = EmailMessage()
        # From = nom du CDM <adresse du cabinet>. En mode OAuth (tests), Gmail remplace l'adresse par celle du
        # compte connecté mais conserve le nom affiché.
        msg["From"] = formataddr((req.get("cdm_nom") or cabinet.libelle, cabinet.sender_address))
        msg["To"] = self.test_recipient or req["client_email"]
        msg["Reply-To"] = cabinet.reply_to
        msg["Subject"] = f"[TEST → {req['client_email']}] {subject}" if self.test_recipient else subject
        msg.set_content(_to_text(html))
        msg.add_alternative(html, subtype="html")
        logo_path = self._logo_path(cabinet)
        if logo_path and "cid:logo" in html:   # logo intégré (affiché même si les images distantes sont bloquées)
            msg.get_payload()[1].add_related(logo_path.read_bytes(), maintype="image", subtype="png", cid="<logo>")
        for p in files:
            msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc = self._service(cabinet.sender_address)
        msg_id = svc.users().messages().send(userId="me", body={"raw": raw}).execute()["id"]
        return SentEmail(gmail_id=msg_id, subject=msg["Subject"], html=html, text=_to_text(html),
                         from_address=cabinet.sender_address, from_name=req.get("cdm_nom") or cabinet.libelle, to_address=msg["To"],
                         html_for_salesforce=self._html_for_salesforce(html, cabinet))

    @staticmethod
    def _html_for_salesforce(html: str, cabinet: Cabinet) -> str:
        """Le logo intégré (cid:) n'existe pas hors de l'email : on pointe vers son URL publique, sinon on retire l'image."""
        if cabinet.logo_url:
            return html.replace("cid:logo", cabinet.logo_url)
        return re.sub(r'<img[^>]*src="cid:logo"[^>]*>', f"<strong>{cabinet.libelle}</strong>", html)


@dataclass
class SentEmail:
    gmail_id: str
    subject: str
    html: str
    text: str
    from_address: str
    from_name: str
    to_address: str
    html_for_salesforce: str


DOC_SUBJECTS = {"CFE": "Votre avis d'imposition CFE"}


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _to_text(html: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ")).strip()


def _oauth_credentials(client_secret_file: str, token_file: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, [GMAIL_SEND_SCOPE])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(client_secret_file, [GMAIL_SEND_SCOPE]).run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())
    return creds
