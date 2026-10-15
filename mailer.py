"""Envoi Gmail au nom du chargé de mission, depuis l'adresse générique du cabinet, Reply-To = Email-to-Case.

Deux modes d'authentification :
  - GOOGLE_SA_KEY_PATH renseigné → compte de service + délégation domain-wide, qui impersonne cabinet.sender_address
  - sinon → OAuth utilisateur (client_secret.json / token.json) : l'email part du compte Gmail autorisé (tests)
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


class Mailer:
    def __init__(self, sa_key_path: str | None, client_secret_file: str, token_file: str,
                 template_file: Path, test_recipient: str | None = None):
        self.sa_key_path, self.client_secret_file, self.token_file = sa_key_path, client_secret_file, token_file
        self.template = template_file.read_text(encoding="utf-8")
        self.test_recipient = test_recipient or None
        self._services: dict[str, object] = {}

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
    def render(self, req: dict, cabinet: Cabinet) -> tuple[str, str]:
        """(objet, corps HTML) de l'email pour cette demande."""
        civ = CIVILITES.get((req.get("client_civilite") or "").upper().strip("."), "")
        logo_path = self._logo_path(cabinet)
        logo_html = ('<img src="cid:logo" alt="{0}" width="200" style="display:block;width:200px;height:auto;border:0;">'
                     .format(cabinet.libelle) if logo_path else f"<strong>{cabinet.libelle}</strong>")
        signature = cabinet.signature_html.format_map(_Safe(
            cdm_nom=req.get("cdm_nom") or cabinet.libelle, cdm_email=req.get("cdm_email") or cabinet.sender_address,
            cdm_telephone=req.get("cdm_telephone") or "", cabinet=cabinet.libelle,
            telephone=req.get("cdm_telephone") or cabinet.telephone, adresse=cabinet.adresse, site_web=cabinet.site_web,
            reply_to=cabinet.reply_to, sender=cabinet.sender_address, logo=logo_html))
        html = self._template_for(cabinet).format_map(_Safe(
            civilite=civ, client_nom=req.get("client_nom") or "", client_prenom=req.get("client_prenom") or "",
            client_name=req.get("client_name") or "", siren=req.get("siren") or "", annee=req.get("annee") or "",
            signature=signature, cabinet=cabinet.libelle))
        if not civ and not req.get("client_nom"):
            html = html.replace("Bonjour  ,", "Bonjour,")
        subject = f"{DOC_SUBJECTS.get(req['document_type'], req['document_type'])} {req['annee']} – {req['client_name']}"
        return subject, html

    def _template_for(self, cabinet: Cabinet) -> str:
        """Gabarit du cabinet (cabinets.json → "template") ou gabarit par défaut."""
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

    def preview(self, req: dict, cabinet: Cabinet) -> str:
        """Texte lisible de ce qui serait envoyé (mode test)."""
        subject, html = self.render(req, cabinet)
        return (f"De : {req.get('cdm_nom') or cabinet.libelle} <{cabinet.sender_address}>\n"
                f"À : {req['client_email']}\nRépondre à : {cabinet.reply_to}\nObjet : {subject}\n\n{_to_text(html)}")

    # ------------------------------------------------------------ envoi
    def send(self, req: dict, cabinet: Cabinet, files: list[Path]) -> str:
        subject, html = self.render(req, cabinet)
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
