"""
Essai « grosse page » : UNE SEULE requête pour obtenir la liste des entreprises du compte Keobiz
avec une taille de page plus grande que les 20 du site (défaut 500).

Pour rester discret, les appels « liste des entreprises » que le site lance lui-même au chargement
de la page sont BLOQUÉS dans le navigateur (ils n'atteignent jamais le serveur) ; seuls les appels
standard de la page passent, puis notre unique requête. Aucun nouvel essai en cas d'échec.
Le croisement avec Salesforce ne touche pas au portail.

Usage : .venv/bin/python test_grosse_page.py [taille]     (défaut 500)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid as uuidlib
from collections import Counter
from urllib.parse import urlparse

import config
from portailpro import PortailPro
from salesforce_client import SalesforceClient
from test_comptes_collab import portail_en_maintenance



def main() -> int:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    if subprocess.run(["pgrep", "-f", "app.py"], capture_output=True).returncode == 0:
        print("❌ Le service (app.py) tourne : arrête-le d'abord."); return 1
    if portail_en_maintenance():
        print("🛠 Portail en maintenance : essai annulé."); return 2

    cab = config.load_cabinets()["Keobiz"]
    stats = {"emis": 0, "bloques": 0}
    hab = {"path": None}

    def on_request(req):
        if urlparse(req.url).netloc.startswith("services.portailpro"):
            stats["emis"] += 1

    def block_hab(route):
        hab["path"] = hab["path"] or urlparse(route.request.url).path
        stats["bloques"] += 1
        route.abort()   # n'atteint jamais le serveur

    p = PortailPro(config.PROFILES_DIR / "Keobiz", headless=config.PORTAIL_HEADLESS, channel=config.BROWSER_CHANNEL,
                   login=cab.portal_login, password=cab.portal_password)
    result = {}
    try:
        p._ctx.on("request", on_request)
        p._ctx.route(re.compile(r".*/pcr-habilitations/.*"), block_hab)
        print("Connexion Keobiz (session mémorisée)…")
        p.login()
        p.page.wait_for_timeout(5000)          # laisse le site tenter (et se faire bloquer) ses appels de liste
        path = hab["path"] or p.habilitations_path or p.habilitations_fallback
        if not path:
            sys.exit("Adresse de la liste des entreprises introuvable (appel du site non observé)")
        params = {"size": str(size), "page": "1", "filter": "siren", **p.rattachements}
        headers = {"Authorization": p.token or "", "Accept": "application/json, text/plain, */*",
                   "X-Correlation-ID": str(uuidlib.uuid4())}
        print(f"Requête unique : page 1, taille {size}…")
        t0 = time.time()
        r = p._http.get(path, params=params, headers=headers)   # UNE requête, aucun nouvel essai
        result = {"status": r.status_code, "duree": round(time.time() - t0, 1)}
        try:
            j = r.json()
        except Exception:
            j = None
            result["corps"] = (r.text or "")[:300]
        if isinstance(j, dict):
            result["sirens"] = [str(x.get("siren")) for x in (j.get("sirens") or []) if x.get("siren")]
            result["erreurs"] = [e.get("code") for e in (j.get("erreurs") or [])]
            result["autres_cles"] = sorted(k for k in j if k not in ("sirens", "erreurs"))
        raw = config.DATA_DIR / f"habilitations-Keobiz-size{size}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        raw.write_text(json.dumps(j if j is not None else result, ensure_ascii=False, indent=1), encoding="utf-8")
        result["fichier"] = raw.name
    finally:
        try:
            p.close()
        except Exception:
            pass

    envoyes = stats["emis"] - stats["bloques"]
    print(f"\n=== Résultat ===")
    print(f"HTTP {result.get('status')} en {result.get('duree')} s")
    if "corps" in result:
        print("Réponse non JSON :", result["corps"])
    n = len(result.get("sirens") or [])
    if "sirens" in result:
        if n == 0:
            verdict = "liste vide (taille refusée ou ignorée ?)"
        elif n == size:
            verdict = f"page PLEINE ({n}) → la taille {size} est acceptée, mais il y a d'autres pages"
        elif n == 20 and size != 20:
            verdict = "20 éléments seulement → le portail IGNORE la taille demandée (plafond 20)"
        else:
            verdict = f"{n} < {size} → liste COMPLÈTE en une seule requête"
        print(f"SIREN reçus : {n} — {verdict}")
        print(f"Codes 'erreurs' : {result.get('erreurs')} · autres clés : {result.get('autres_cles')}")
    print(f"\nTrafic : {envoyes + 1} appel(s) API réellement envoyé(s) au portail "
          f"(1 vérif. maintenance + appels standard de la page + notre requête) ; {stats['bloques']} appel(s) du site bloqué(s) avant envoi")

    # ---- Salesforce (aucune requête portail)
    sirens = result.get("sirens") or []
    if sirens:
        accs = {a["siren"]: a for a in SalesforceClient(config.sf_auth()).get_accounts_by_sirens(sirens)}
        keo = [a for a in accs.values() if a["entite"] == "Keobiz"]
        val = [a for a in keo if a["statut_mission"] == "MissionValidee"]
        print(f"\nSalesforce : {len(accs)}/{len(sirens)} SIREN connus · {len(keo)} rattachés à Keobiz · {len(val)} en mission validée")
        top = Counter(a["cdm_nom"] or "(sans CDM)" for a in val).most_common(8)
        print("Chargés de mission les plus représentés :", ", ".join(f"{c} ({k})" for c, k in top))
    print(f"\nRéponse brute : data/{result.get('fichier')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
