"""Lecture d'un avis CFE : montant, mode de paiement, action du CDM.

À placer à la racine du dépôt portail-pro, à côté de documents.py.
N'accède ni au portail ni au réseau : prend les octets d'un PDF déjà téléchargé et les analyse.

lire_avis_cfe(pdf_bytes) renvoie un AnalyseCFE :
  siren           : 9 chiffres (depuis le n° SIRET), ou None
  annee           : année de l'avis (2026…), ou None
  montant_a_payer : float en euros (0.0 si rien à payer)
  cas             : RIEN_A_PAYER | A_PAYER | PRELEVEMENT_ECHEANCE | MENSUALISE | INDETERMINE
  action_cdm      : True seulement si le CDM doit aller payer (cas A_PAYER, montant > 0)
  libelle         : phrase lisible pour le tableau récap

Prudence voulue : tout avis que la fonction ne sait pas classer avec certitude ressort en
INDETERMINE (action_cdm=False) plutôt que d'être deviné. Mieux vaut « à vérifier » qu'un
mauvais classement qui enverrait un faux message au client. Idem si le PDF est illisible.

Règle de décision, sur le seul cadre « Votre situation » (pas la notice générique) :
  1. "VOUS N'AVEZ RIEN À PAYER" ou montant 0                     -> RIEN_A_PAYER
  2. mensualisation (versements / somme restant à prélever / contrat) -> MENSUALISE
  3. "vous avez choisi le PRÉLÈVEMENT À L'ÉCHÉANCE"              -> PRELEVEMENT_ECHEANCE
  4. "vous n'avez pas adhéré à un prélèvement"                  -> A_PAYER (action CDM)
  5. montant > 0 sans mode identifié                            -> INDETERMINE
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
    date_limite: str | None = None      # "à payer" : date limite de paiement (JJ/MM/AAAA)
    date_prelevement: str | None = None # "prélèvement à l'échéance" : date du prélèvement
    solde_restant: float | None = None  # "mensualisé" : somme restant à prélever


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).lower()


def _texte_pdf(pdf_bytes: bytes) -> str | None:
    """Texte de toutes les pages, ou None si le PDF est illisible (chiffré, scanné, corrompu)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            return None
        txt = "\n".join(p.extract_text() or "" for p in reader.pages)
        return txt if txt.strip() else None  # vide => sans doute un scan image
    except Exception as e:
        log.warning("PDF CFE illisible : %s", e)
        return None


def _cadre_situation(texte: str) -> str:
    """Isole le haut de l'avis (cadre « Votre situation ») avant la notice explicative.

    Les mots « mensualisé », « prélèvement à l'échéance »… apparaissent AUSSI dans la notice
    générique (« Comment payer votre impôt ? »), présente sur tout avis. Sans cette coupe, tout
    avis serait classé « mensualisé ». On coupe au premier marqueur de notice rencontré.
    """
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


def _date_limite(texte: str) -> str | None:
    """« Au plus tard le JJ/MM/AAAA » (cas à payer)."""
    m = re.search(r"au\s+plus\s+tard\s+le\s+(\d{2}/\d{2}/\d{4})", texte, re.I)
    return m.group(1) if m else None


def _date_prelevement(texte: str) -> str | None:
    """« sera prélevé le JJ/MM/AAAA » (cas prélèvement à l'échéance)."""
    m = re.search(r"pr[ée]lev[ée]\s+le\s+(\d{2}/\d{2}/\d{4})", texte, re.I)
    return m.group(1) if m else None


def _solde_restant(texte: str) -> float | None:
    """« Somme restant à prélever : X,XX € » (cas mensualisé)."""
    m = re.search(r"somme\s+restant\s+a?\s*pr[ée]lever\s*:?\s*([\d\s\u202f\xa0]+,\d{2})", _norm(texte), re.I) \
        or re.search(r"somme\s+restant[^\d]{0,20}([\d\s\u202f\xa0]+,\d{2})", texte, re.I)
    if not m:
        return None
    brut = m.group(1).replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    try:
        return float(brut.replace(",", "."))
    except ValueError:
        return None


def lire_avis_cfe(pdf_bytes: bytes) -> AnalyseCFE:
    texte = _texte_pdf(pdf_bytes)
    if texte is None:
        return AnalyseCFE(None, None, 0.0, "INDETERMINE", False,
                          "PDF illisible (chiffré ou scanné) — à vérifier à la main")

    siren, annee = _siren(texte), _annee(texte)       # lisibles sur tout le document
    situation = _cadre_situation(texte)               # le CAS se décide sur le seul cadre du haut
    n = _norm(situation)
    montant = _montant(situation)
    date_limite = _date_limite(situation)
    date_prelevement = _date_prelevement(situation)
    solde_restant = _solde_restant(situation)

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

    return AnalyseCFE(siren, annee, montant or 0.0, cas, action, lib,
                      date_limite=date_limite, date_prelevement=date_prelevement, solde_restant=solde_restant)


# --- Test hors ligne : python lecture_cfe.py avis1.pdf avis2.pdf ... ---
if __name__ == "__main__":
    import sys
    for chemin in sys.argv[1:]:
        with open(chemin, "rb") as f:
            r = lire_avis_cfe(f.read())
        print(f"{chemin}\n  siren={r.siren} annee={r.annee} montant={r.montant_a_payer:.2f} "
              f"cas={r.cas} action_cdm={r.action_cdm}\n  {r.libelle}\n")
