"""Salesforce : recherche client (typeahead Slack), détails du compte, archivage des PDF sur la fiche."""
from __future__ import annotations

import base64
import re
import threading

from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceExpiredSession

ACCOUNT_FIELDS = ("Id, Name, SIREN__c, Entite_Juridique_Calc__c, StatutMission__c, "
                  "ContactSalutation__c, ContactFirstName__c, ContactLastName__c, ContactEmail__c, "
                  "ChargeDeMission__r.Name, ChargeDeMission__r.Email, ChargeDeMission__r.Phone")

_SOSL_RESERVED = re.compile(r"([?&|!{}\[\]()^~*:\\\"'+\-])")


class SalesforceClient:
    def __init__(self, auth: dict):
        """auth : arguments simple_salesforce.Salesforce() — voir config.sf_auth() (JWT ou mot de passe)."""
        self._auth = auth
        self._sf: Salesforce | None = None
        self._lock = threading.Lock()

    def _client(self) -> Salesforce:
        with self._lock:
            if self._sf is None:
                self._sf = Salesforce(**self._auth)
            return self._sf

    def _call(self, fn):
        try:
            return fn(self._client())
        except SalesforceExpiredSession:
            with self._lock:
                self._sf = None
            return fn(self._client())

    # ------------------------------------------------------------ recherche
    def search_accounts(self, term: str, limit: int = 20) -> list[dict]:
        """Recherche par nom (SOSL) ou par début de SIREN (SOQL) — pour le menu déroulant Slack."""
        term = term.strip()
        if not term:
            return []
        if re.fullmatch(r"[\d ]{2,}", term):
            siren = term.replace(" ", "")
            soql = (f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE SIREN__c LIKE '{siren}%' "
                    f"AND StatutMission__c != 'MissionAnnulee' ORDER BY Name LIMIT {limit}")
            records = self._call(lambda sf: sf.query(soql))["records"]
        else:
            safe = _SOSL_RESERVED.sub(r"\\\1", term)
            sosl = (f"FIND {{{safe}*}} IN NAME FIELDS RETURNING Account({ACCOUNT_FIELDS} "
                    f"WHERE SIREN__c != null AND StatutMission__c != 'MissionAnnulee' ORDER BY Name LIMIT {limit})")
            records = self._call(lambda sf: sf.search(sosl))["searchRecords"]
        return [_normalize(r) for r in records]

    def get_account(self, account_id: str) -> dict | None:
        soql = f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE Id = '{account_id}' LIMIT 1"
        records = self._call(lambda sf: sf.query(soql))["records"]
        return _normalize(records[0]) if records else None

    def list_accounts_for_campaign(self, entite: str, extra_filter: str | None) -> list[dict]:
        """Tous les clients d'un cabinet retenus pour une campagne (SIREN renseigné, mission non annulée, filtre .env)."""
        where = (f"Entite_Juridique_Calc__c = '{entite}' AND SIREN__c != null AND StatutMission__c != 'MissionAnnulee'"
                 + (f" AND ({extra_filter})" if extra_filter else ""))
        records = self._call(lambda sf: sf.query_all(f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE {where} ORDER BY Name"))["records"]
        return [_normalize(r) for r in records]

    def get_accounts_by_sirens(self, sirens: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(sirens), 200):
            chunk = ", ".join(f"'{re.sub(r'[^0-9]', '', x)}'" for x in sirens[i:i + 200])
            records = self._call(lambda sf: sf.query_all(f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE SIREN__c IN ({chunk}) "
                                                         f"AND StatutMission__c != 'MissionAnnulee' ORDER BY Name"))["records"]
            out.extend(_normalize(r) for r in records)
        return out

    # ------------------------------------------------------------ archivage
    def archive_pdf(self, account_id: str, filename: str, content: bytes) -> str:
        """Dépose le PDF dans les fichiers de la fiche Account (ContentVersion + lien automatique)."""
        body = {"Title": filename.rsplit(".", 1)[0], "PathOnClient": filename,
                "VersionData": base64.b64encode(content).decode(), "FirstPublishLocationId": account_id}
        return self._call(lambda sf: sf.ContentVersion.create(body))["id"]


    def log_email(self, account_id: str, subject: str, html_body: str, text_body: str, from_address: str,
                  from_name: str, to_address: str, sent_at_iso: str, content_version_ids: list[str]) -> str:
        """Enregistre l'email envoyé dans la chronologie d'activités de la fiche (objet EmailMessage, statut Envoyé)
        et y rattache les PDF déjà archivés. Renvoie l'Id de l'EmailMessage."""
        body = {"RelatedToId": account_id, "Subject": subject[:3000], "HtmlBody": html_body[:32000], "TextBody": text_body[:32000],
                "FromAddress": from_address, "FromName": from_name[:255], "ToAddress": to_address,
                "Status": "5", "Incoming": False, "MessageDate": sent_at_iso}   # 5 = brouillon : seul état qui accepte les pièces jointes
        email_id = self._call(lambda sf: sf.EmailMessage.create(body))["id"]
        if content_version_ids:
            ids = ",".join(f"'{i}'" for i in content_version_ids)
            docs = self._call(lambda sf: sf.query(f"SELECT ContentDocumentId FROM ContentVersion WHERE Id IN ({ids})"))["records"]
            for d in docs:
                try:
                    self._call(lambda sf: sf.ContentDocumentLink.create(
                        {"ContentDocumentId": d["ContentDocumentId"], "LinkedEntityId": email_id, "ShareType": "V", "Visibility": "AllUsers"}))
                except Exception as e:   # le lien PDF ↔ email est un confort : l'email reste enregistré
                    import logging
                    logging.getLogger("salesforce").warning("Rattachement du PDF à l'email %s : %s", email_id, e)
        self._call(lambda sf: sf.EmailMessage.update(email_id, {"Status": "3"}))   # 3 = envoyé
        return email_id


def _normalize(r: dict) -> dict:
    cdm = r.get("ChargeDeMission__r") or {}
    return {
        "id": r["Id"],
        "name": r.get("Name") or "",
        "siren": re.sub(r"\D", "", r.get("SIREN__c") or ""),
        "entite": r.get("Entite_Juridique_Calc__c") or "",
        "statut_mission": r.get("StatutMission__c") or "",
        "civilite": r.get("ContactSalutation__c") or "",
        "prenom": r.get("ContactFirstName__c") or "",
        "nom": r.get("ContactLastName__c") or "",
        "email": (r.get("ContactEmail__c") or "").strip(),
        "cdm_nom": cdm.get("Name") or "",
        "cdm_email": cdm.get("Email") or "",
        "cdm_telephone": cdm.get("Phone") or "",
    }
