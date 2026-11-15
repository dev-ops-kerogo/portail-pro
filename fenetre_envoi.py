"""Fenêtre d'envoi des mails de campagne : lundi-vendredi, hors jours fériés français, 8h30-17h30 (heure de Paris).

Autonome : les jours fériés (y compris les mobiles liés à Pâques) sont calculés, sans dépendance externe.
Le worker appelle peut_envoyer_maintenant() avant chaque envoi ; si c'est non, il attend secondes_avant_ouverture().

Réglables dans .env (sinon valeurs par défaut) :
    CAMPAIGN_SEND_WINDOW=true          # false = pas de restriction (envoi à toute heure, tous les jours)
    CAMPAIGN_SEND_START=08:30
    CAMPAIGN_SEND_END=17:30
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def _paques(an: int) -> date:
    """Dimanche de Pâques (algorithme de Butcher/Gauss)."""
    a = an % 19
    b, c = divmod(an, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mois, jour = divmod(h + l - 7 * m + 114, 31)
    return date(an, mois, jour + 1)


def jours_feries(an: int) -> set[date]:
    """Les 11 jours fériés nationaux français de l'année (fixes + mobiles liés à Pâques)."""
    p = _paques(an)
    return {
        date(an, 1, 1),      # Jour de l'an
        p + timedelta(days=1),    # Lundi de Pâques
        date(an, 5, 1),      # Fête du travail
        date(an, 5, 8),      # Victoire 1945
        p + timedelta(days=39),   # Ascension
        p + timedelta(days=50),   # Lundi de Pentecôte
        date(an, 7, 14),     # Fête nationale
        date(an, 8, 15),     # Assomption
        date(an, 11, 1),     # Toussaint
        date(an, 11, 11),    # Armistice 1918
        date(an, 12, 25),    # Noël
    }


def est_ouvre(d: date) -> bool:
    """Jour ouvré = lundi-vendredi et non férié."""
    return d.weekday() < 5 and d not in jours_feries(d.year)


def _hhmm(val: str, defaut: str) -> time:
    h, m = (os.getenv(val) or defaut).split(":")
    return time(int(h), int(m))


def fenetre_active() -> bool:
    return (os.getenv("CAMPAIGN_SEND_WINDOW", "true").strip().lower() not in ("0", "false", "non", "no"))


def peut_envoyer_maintenant(maintenant: datetime | None = None) -> bool:
    """True si on peut envoyer là, maintenant (heure de Paris), selon la fenêtre. Fenêtre désactivée → toujours True."""
    if not fenetre_active():
        return True
    now = (maintenant or datetime.now(PARIS)).astimezone(PARIS)
    if not est_ouvre(now.date()):
        return False
    return _hhmm("CAMPAIGN_SEND_START", "08:30") <= now.time() < _hhmm("CAMPAIGN_SEND_END", "17:30")


def prochaine_ouverture(maintenant: datetime | None = None) -> datetime:
    """Le prochain instant où la fenêtre s'ouvre (heure de Paris)."""
    now = (maintenant or datetime.now(PARIS)).astimezone(PARIS)
    debut = _hhmm("CAMPAIGN_SEND_START", "08:30")
    # aujourd'hui, avant l'ouverture, et jour ouvré → ouverture aujourd'hui
    if est_ouvre(now.date()) and now.time() < debut:
        return datetime.combine(now.date(), debut, tzinfo=PARIS)
    # sinon, le prochain jour ouvré à l'heure d'ouverture
    j = now.date() + timedelta(days=1)
    while not est_ouvre(j):
        j += timedelta(days=1)
    return datetime.combine(j, debut, tzinfo=PARIS)


def secondes_avant_ouverture(maintenant: datetime | None = None) -> float:
    """Secondes à attendre avant la prochaine ouverture (0 si on est déjà dans la fenêtre)."""
    if peut_envoyer_maintenant(maintenant):
        return 0.0
    now = (maintenant or datetime.now(PARIS)).astimezone(PARIS)
    return max(0.0, (prochaine_ouverture(now) - now).total_seconds())
