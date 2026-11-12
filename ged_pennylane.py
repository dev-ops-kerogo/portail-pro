"""Dépôt d'un avis CFE dans la GED Pennylane d'un dossier client (API Firm).

À placer à la racine du service, à côté de worker.py.

Pour chaque avis : trouve le dossier Pennylane du client par son SIREN, descend l'arborescence
GED jusqu'au sous-dossier voulu (par défaut « Exercice <annee> / Fiscal / Autres déclarations »),
et y dépose le PDF. Rien n'est jamais supprimé ; en cas d'échec, l'erreur est renvoyée à l'appelant
(le worker la journalise sans faire échouer l'envoi au client).

Activation et réglages dans .env :
    GED_PENNYLANE_ENABLED=true
    PENNYLANE_FIRM_TOKEN=...                         (jeton Firm : Entreprises lecture + Fichiers GED écriture)
    GED_PENNYLANE_CHEMIN=Exercice {annee}|Fiscal|Autres déclarations   (dossiers séparés par |)

Le chemin accepte {annee} ; on le compare sans tenir compte des accents ni de la casse, pour résister
aux petites variations de libellés entre dossiers.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata

import httpx

log = logging.getLogger("ged_pennylane")

BASE = "https://app.pennylane.com/api/external/firm/v1"
CHEMIN_DEFAUT = "Exercice {annee}|Fiscal|Autres déclarations"


class GEDError(RuntimeError):
    """Le dépôt n'a pas abouti (client introuvable, dossier manquant, API en erreur…)."""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


class GEDPennylane:
    """Client minimal de l'API Firm pour déposer dans la GED. Réutilisable entre dépôts (une session HTTP)."""

    def __init__(self, token: str | None = None, chemin: str | None = None):
        self.token = (token or os.getenv("PENNYLANE_FIRM_TOKEN", "")).strip()
        self.chemin = (chemin or os.getenv("GED_PENNYLANE_CHEMIN") or CHEMIN_DEFAUT)
        if not self.token:
            raise GEDError("PENNYLANE_FIRM_TOKEN absent : dépôt GED impossible")
        self._http = httpx.Client(base_url=BASE, timeout=60,
                                  headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        self._company_by_siren: dict[str, int] = {}   # cache SIREN → id société Pennylane (durée de vie du client)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ API
    def _get(self, path: str, **params) -> dict:
        r = self._http.get(path, params=params)
        if r.status_code == 403:
            raise GEDError(f"accès refusé par Pennylane (403) sur {path} — vérifier les droits du jeton")
        r.raise_for_status()
        return r.json()

    def _company_id(self, siren: str) -> int:
        """id de la société Pennylane pour ce SIREN (lu dans la liste des entreprises, mis en cache)."""
        siren = re.sub(r"\D", "", siren)
        if siren in self._company_by_siren:
            return self._company_by_siren[siren]
        page = 1
        while True:
            data = self._get("/companies", page=page, per_page=1000)
            items = data.get("items") or []
            for c in items:
                s = re.sub(r"\D", "", str(c.get("siren") or c.get("registration_number") or ""))
                if s:
                    self._company_by_siren[s] = c["id"]
            if siren in self._company_by_siren:
                return self._company_by_siren[siren]
            if len(items) < 1000:   # dernière page atteinte, SIREN non trouvé
                raise GEDError(f"SIREN {siren} introuvable dans le portefeuille Pennylane du cabinet")
            page += 1

    def _folders(self, company_id: int) -> list[dict]:
        folders, cursor = [], None
        while True:
            data = self._get(f"/companies/{company_id}/dms/folders", limit=100, **({"cursor": cursor} if cursor else {}))
            folders += data.get("items") or []
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return folders

    def _folder_id(self, company_id: int, chemin: list[str]) -> int:
        """Descend l'arborescence : à chaque niveau, un dossier dont le nom correspond et dont le parent est le
        dossier du niveau précédent (racine au départ). Comparaison insensible aux accents et à la casse."""
        folders = self._folders(company_id)
        parent: int | None = None
        for voulu in chemin:
            cible = _norm(voulu)
            match = next((f for f in folders
                          if _norm(f.get("name")) == cible and (f.get("parent_folder") or {}).get("id") == parent), None)
            if not match:
                raise GEDError(f"dossier GED « {voulu} » introuvable (chemin : {' / '.join(chemin)})")
            parent = match["id"]
        assert parent is not None
        return parent

    def deposer(self, siren: str, annee: str, nom_fichier: str, pdf: bytes) -> dict:
        """Dépose le PDF dans le dossier voulu de la GED du client. Renvoie la réponse Pennylane."""
        company_id = self._company_id(siren)
        chemin = [part.format(annee=annee).strip() for part in self.chemin.split("|") if part.strip()]
        folder_id = self._folder_id(company_id, chemin)
        r = self._http.post(f"/companies/{company_id}/dms/files",
                            files={"file": (nom_fichier, pdf, "application/pdf")},
                            data={"parent_folder_id": str(folder_id)})
        if not r.is_success:
            raise GEDError(f"dépôt refusé par Pennylane (HTTP {r.status_code}) : {r.text[:300]}")
        return r.json()


def ged_active() -> bool:
    return (os.getenv("GED_PENNYLANE_ENABLED", "").strip().lower() in ("1", "true", "oui", "yes")
            and bool(os.getenv("PENNYLANE_FIRM_TOKEN", "").strip()))
