"""Suivi des avis CFE dans un Google Sheet, au fil de l'eau (une ligne par client traité).

Réutilise le compte de service Google du service (celui du Sheet des accès). L'onglet est choisi
automatiquement : celui dont le nom contient l'année de la campagne (ex. « ... CFE 2026 »), pour ne
pas avoir à changer le code chaque année. Les données sont écrites à partir de DATA_START_ROW, sous
les en-têtes de la maquette.

Colonnes (A→H) : Société | SIREN | CDM (Salesforce) | Montant (€) | Situation | Action CDM | Compte portail utilisé | Statut

Réglages .env :
    SUIVI_SHEET_ENABLED=true
    SUIVI_SHEET_ID=1o0gSmrp0FTyEMTp1Q6tfzrB4YllO6yUSBVbFpamIokQ
    SUIVI_SHEET_SA_KEY_PATH=secrets/gmail-sa.json      # défaut : la clé du compte technique du Sheet des accès
    SUIVI_SHEET_DATA_START_ROW=7                        # 1re ligne de données (sous les en-têtes)

Écriture idempotente : un même SIREN met à jour sa ligne (pas de doublon) ; un nouveau SIREN est ajouté.
Jamais bloquant : toute erreur est renvoyée à l'appelant, qui journalise sans faire échouer l'envoi au client.
"""
from __future__ import annotations

import os
import re
import threading

SHEETS_RW = "https://www.googleapis.com/auth/spreadsheets"
DATA_START_ROW_DEFAUT = 7
LIBELLE_CAS = {
    "RIEN_A_PAYER": "Rien à payer",
    "A_PAYER": "À payer, pas de prélèvement",
    "PRELEVEMENT_ECHEANCE": "Prélèvement à l'échéance",
    "MENSUALISE": "Mensualisé",
    "INDETERMINE": "À vérifier",
}


class SuiviSheetError(RuntimeError):
    """Écriture dans le Sheet impossible (accès, onglet introuvable, API en erreur…)."""


def suivi_actif() -> bool:
    return (os.getenv("SUIVI_SHEET_ENABLED", "").strip().lower() in ("1", "true", "oui", "yes")
            and bool(os.getenv("SUIVI_SHEET_ID", "").strip()))


class SuiviSheet:
    """Client d'écriture du suivi. Un verrou sérialise les écritures (le worker est mono-thread, mais prudence)."""

    def __init__(self, sheet_id: str | None = None, key_path: str | None = None):
        self.sheet_id = (sheet_id or os.getenv("SUIVI_SHEET_ID", "")).strip()
        self.key_path = (key_path or os.getenv("SUIVI_SHEET_SA_KEY_PATH")
                         or os.getenv("CREDENTIALS_SA_KEY_PATH") or os.getenv("GOOGLE_SA_KEY_PATH") or "").strip()
        self.data_start = int(os.getenv("SUIVI_SHEET_DATA_START_ROW", str(DATA_START_ROW_DEFAUT)))
        if not self.sheet_id:
            raise SuiviSheetError("SUIVI_SHEET_ID absent")
        if not self.key_path or not os.path.exists(self.key_path):
            raise SuiviSheetError(f"clé du compte de service introuvable ({self.key_path})")
        self._lock = threading.Lock()
        self._svc = None
        self._tab_cache: dict[str, str] = {}   # année -> nom d'onglet

    def _sheets(self):
        if self._svc is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            creds = service_account.Credentials.from_service_account_file(self.key_path, scopes=[SHEETS_RW])
            self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        return self._svc

    def _onglet_annee(self, annee: str) -> str:
        """Nom de l'onglet dont le titre contient l'année (ex. 'Suivi traitement auto CFE 2026')."""
        annee = str(annee)
        if annee in self._tab_cache:
            return self._tab_cache[annee]
        try:
            meta = self._sheets().get(spreadsheetId=self.sheet_id,
                                      fields="sheets(properties(title))").execute()
        except Exception as e:
            raise SuiviSheetError(f"lecture des onglets impossible : {e}")
        titres = [s["properties"]["title"] for s in meta.get("sheets", [])]
        match = [t for t in titres if annee in t]
        if not match:
            raise SuiviSheetError(f"aucun onglet ne contient l'année {annee} (onglets : {', '.join(titres)})")
        self._tab_cache[annee] = match[0]
        return match[0]

    @staticmethod
    def _norm_siren(v) -> str:
        """SIREN comparable : chiffres seuls, sans zéros de tête (le Sheet peut stocker un SIREN comme nombre)."""
        d = re.sub(r"\D", "", str(v or ""))
        return d.lstrip("0") or d

    def _colonne_sirens(self, tab: str) -> list[str]:
        """SIREN déjà présents (colonne B), NORMALISÉS, pour retrouver la ligne d'un client et éviter les doublons."""
        r = self._sheets().values().get(spreadsheetId=self.sheet_id,
                                        range=f"'{tab}'!B{self.data_start}:B").execute()
        return [self._norm_siren(row[0] if row else "") for row in r.get("values", [])]

    @staticmethod
    def _ligne(av: dict) -> list:
        """Une ligne du tableau à partir d'un dict d'avis."""
        cas = av.get("cas")
        return [
            av.get("societe", ""),
            av.get("siren", ""),
            av.get("cdm", ""),
            av.get("montant", ""),
            LIBELLE_CAS.get(cas, av.get("situation", "") or (cas or "")),
            "Oui" if av.get("action_cdm") else "Non",
            av.get("compte_portail", ""),
            av.get("statut", ""),
        ]

    def enregistrer(self, avis: dict) -> None:
        """Ajoute ou met à jour la ligne du client (clé = SIREN). Idempotent."""
        siren_norm = self._norm_siren(avis.get("siren"))
        annee = str(avis.get("annee") or "")
        with self._lock:
            tab = self._onglet_annee(annee)
            sirens = self._colonne_sirens(tab)
            try:
                idx = sirens.index(siren_norm)                  # SIREN déjà là -> on met à jour sa ligne
                ligne_no = self.data_start + idx
            except ValueError:
                ligne_no = self.data_start + len(sirens)        # nouveau -> première ligne libre
            rng = f"'{tab}'!A{ligne_no}:H{ligne_no}"
            try:
                self._sheets().values().update(
                    spreadsheetId=self.sheet_id, range=rng,
                    valueInputOption="USER_ENTERED", body={"values": [self._ligne(avis)]}).execute()
            except Exception as e:
                raise SuiviSheetError(f"écriture ligne {ligne_no} impossible : {e}")
