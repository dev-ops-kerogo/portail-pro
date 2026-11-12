"""Lecture d'un avis CFE : montant, mode de paiement, action du CDM.

À placer à la racine du dépôt, à côté de documents.py.
N'accède ni au portail ni au réseau : prend les octets d'un PDF déjà téléchargé et les analyse.

lire_avis_cfe(pdf_bytes) renvoie un AnalyseCFE :
  siren, annee, montant_a_payer,
  cas : RIEN_A_PAYER | A_PAYER | PRELEVEMENT_ECHEANCE | MENSUALISE | INDETERMINE
  action_cdm : True seulement si le CDM doit aller payer (cas A_PAYER, montant > 0)
  libelle : phrase lisible pour le tableau récap

Prudence voulue : tout avis non classable avec certitude ressort en INDETERMINE (action_cdm=False)
plutôt que deviné. Idem si le PDF est illisible.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("lecture_cfe")


@dataclass
class AnalyseCFE:
    siren: str | None
    annee: int | None
    montant_a_payer: float
    cas: str
    action_cdm: bool
    libelle: str


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).lower()


def _texte_pdf(pdf_bytes: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            return None
        txt = "\n".join(p.extract_text() or "" for p in reader.pages)
        return txt if txt.strip() else None
    except Exception as e:
        log.warning("PDF CFE illisible : %s", e)
        return None


def _cadre_situation(texte: str) -> str:
    """Isole le cadre « Votre situation » avant la notice générique (sinon tout avis serait « mensualisé »)."""
    n = _norm(texte)
    marqueurs = ("information la valeur locative", "comment payer votre impot",
                 "obligation de paiement par voie")
    positions = [n.find(m) for m in marqueurs if n.find(m) != -1]
    return texte[:min(positions)] if positions else texte


def _montant(texte: str) -> float | None:
    m = re.search(r"montant\s*a\s*payer\s*:?\s*([\d\s\u202f\xa0]+,\d{2})", texte, re.I)
    if not m:
        return None
    brut = m.group(1).replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    try:
        return float(brut.replace(",", "."))
    except ValueError:
        return None


def _siren(texte: str) -> str | None:
    m = re.search(r"n[°o]\s*siret\s*:?\s*(\d{9})\s*\d{5}", texte, re.I)
    return m.group(1) if m else None


def _annee(texte: str) -> int | None:
    m = re.search(r"avis\s+d['’]impot\s+(\d{4})", _norm(texte))
    return int(m.group(1)) if m else None


def lire_avis_cfe(pdf_bytes: bytes) -> AnalyseCFE:
    texte = _texte_pdf(pdf_bytes)
    if texte is None:
        return AnalyseCFE(None, None, 0.0, "INDETERMINE", False,
                          "PDF illisible (chiffré ou scanné) — à vérifier à la main")

    siren, annee = _siren(texte), _annee(texte)
    situation = _cadre_situation(texte)
    n = _norm(situation)
    montant = _montant(situation)

    rien = "vous n'avez rien a payer" in n or montant == 0.0
    mensualise = any(x in n for x in (
        "total de vos versements", "somme restant a prelever", "contrat de mensualisation"))
    echeance = "prelevement a l'echeance" in n and "vous avez choisi" in n
    non_adhere = "n'avez pas adhere a un prelevement" in n

    if rien:
        cas, action, lib = "RIEN_A_PAYER", False, "Rien à payer"
    elif mensualise:
        reste = montant or 0.0
        cas, action = "MENSUALISE", False
        lib = f"Mensualisé, reste à prélever {reste:.2f} €" if reste else "Mensualisé"
    elif echeance:
        cas, action, lib = "PRELEVEMENT_ECHEANCE", False, "Prélèvement à l'échéance (automatique)"
    elif non_adhere:
        cas, action, lib = "A_PAYER", True, "À payer — le CDM doit aller payer"
    else:
        cas, action, lib = "INDETERMINE", False, "Mode de paiement non identifié — à vérifier"

    return AnalyseCFE(siren, annee, montant or 0.0, cas, action, lib)
