"""Registre des types de documents + récupération sur le portail.

Ajouter un type = une entrée dans DOCUMENT_TYPES + une fonction fetch_<code>(portail, siren, annee, periode).
"""
from __future__ import annotations

from dataclasses import dataclass

from portailpro import PortailPro

DOCUMENT_TYPES = {
    "CFE": {"label": "Avis d'imposition CFE", "unit": "ETABLISSEMENT", "needs_periode": False},
    # "TVA": {"label": "Déclaration TVA", "unit": "ENTREPRISE", "needs_periode": True},   # parcours à cartographier
}


@dataclass
class DocResult:
    label: str            # ex. "SIRET 12345678900011 – SOCIETE EXEMPLE"
    filename: str         # nom de la pièce jointe
    content: bytes | None
    status: str           # DOWNLOADED | SKIPPED
    message: str = ""


def fetch(portail: PortailPro, document_type: str, siren: str, annee: str, periode: str | None = None) -> list[DocResult]:
    if document_type == "CFE":
        return fetch_cfe(portail, siren, annee)
    raise NotImplementedError(f"Type de document non pris en charge : {document_type}")


def fetch_cfe(portail: PortailPro, siren: str, annee: str) -> list[DocResult]:
    """CFE = un avis par établissement (SIRET). Le portail liste directement les avis du SIREN."""
    avis = [a for a in portail.list_avis(siren) if a.is_cfe and a.annee == annee]
    if not avis:
        return [DocResult(label=f"SIREN {siren}", filename="", content=None, status="SKIPPED",
                          message=f"Aucun avis CFE {annee} disponible sur le portail")]
    results = []
    for a in sorted(avis, key=lambda a: (a.siret, a.reference)):
        label = f"SIRET {a.siret} – {a.commune}".strip(" –")
        results.append(DocResult(label=label, filename=f"CFE_{annee}_{a.siret}.pdf",
                                 content=portail.download_document(siren, a.id_principal), status="DOWNLOADED"))
        if a.id_suite:
            results.append(DocResult(label=label + " (suite)", filename=f"CFE_{annee}_{a.siret}_suite.pdf",
                                     content=portail.download_document(siren, a.id_suite), status="DOWNLOADED"))
    return results
