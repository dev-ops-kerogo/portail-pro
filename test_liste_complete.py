"""
Liste COMPLÈTE des entreprises accessibles par le compte Keobiz, par pages de 500 (taille acceptée par le
portail le 14/10), puis mesure de la couverture des clients Keobiz (Salesforce, sans requête portail).

Prudence (anti-blocage) :
  - les appels « liste » lancés automatiquement par le site au chargement sont BLOQUÉS avant envoi ;
  - une requête par page, PAUSE entre deux pages (défaut 10 s), arrêt dès la dernière page (incomplète) ;
  - plafond de sécurité (défaut 30 pages = 15 000 entreprises) ; arrêt immédiat sur toute réponse anormale.

Usage : .venv/bin/python test_liste_complete.py [--taille 500] [--pause 10] [--max-pages 30]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import uuid as uuidlib
from urllib.parse import urlparse

import config
from portailpro import PortailPro
from salesforce_client import SalesforceClient
from test_comptes_collab import portail_en_maintenance



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--taille", type=int, default=500)
    ap.add_argument("--pause", type=float, default=10.0)
    ap.add_argument("--max-pages", type=int, default=30)
    args = ap.parse_args()

    if subprocess.run(["pgrep", "-f", "app.py"], capture_output=True).returncode == 0:
        print("❌ Le service (app.py) tourne : arrête-le d'abord."); return 1
    if portail_en_maintenance():
        print("🛠 Portail en maintenance : annulé."); return 2

    cab = config.load_cabinets()["Keobiz"]
    stats = {"emis": 0, "bloques": 0}
    hab = {"path": None}

    def on_request(req):
        if urlparse(req.url).netloc.startswith("services.portailpro"):
            stats["emis"] += 1

    def block_hab(route):
        hab["path"] = hab["path"] or urlparse(route.request.url).path
        stats["bloques"] += 1
        route.abort()

    p = PortailPro(config.PROFILES_DIR / "Keobiz", headless=config.PORTAIL_HEADLESS, channel=config.BROWSER_CHANNEL,
                   login=cab.portal_login, password=cab.portal_password)
    sirens, pages, stop_reason, sent = [], [], "plafond de pages atteint", 0
    try:
        p._ctx.on("request", on_request)
        p._ctx.route(re.compile(r".*/pcr-habilitations/.*"), block_hab)
        print("Connexion Keobiz (session mémorisée)…", flush=True)
        p.login()
        p.page.wait_for_timeout(5000)
        path = hab["path"] or p.habilitations_path or p.habilitations_fallback
        if not path:
            sys.exit("Adresse de la liste des entreprises introuvable (appel du site non observé)")
        for page in range(1, args.max_pages + 1):
            params = {"size": str(args.taille), "page": str(page), "filter": "siren", **p.rattachements}
            headers = {"Authorization": p.token or "", "Accept": "application/json, text/plain, */*",
                       "X-Correlation-ID": str(uuidlib.uuid4())}
            sent += 1
            r = p._http.get(path, params=params, headers=headers)   # une requête, aucun nouvel essai
            try:
                j = r.json()
            except Exception:
                j = None
            if r.status_code != 200 or not isinstance(j, dict):
                stop_reason = f"réponse anormale page {page} (HTTP {r.status_code}) → arrêt par prudence"
                print(f"  ⚠️ {stop_reason}", flush=True)
                break
            got = [str(x.get("siren")) for x in (j.get("sirens") or []) if x.get("siren")]
            sirens += got
            pages.append(len(got))
            print(f"  page {page} : {len(got)} entreprise(s) — total {len(sirens)}", flush=True)
            if len(got) < args.taille:
                stop_reason = "dernière page atteinte (liste complète)"
                break
            time.sleep(args.pause)
    finally:
        try:
            p.close()
        except Exception:
            pass

    sirens = sorted(set(sirens))
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = config.DATA_DIR / f"habilitations-Keobiz-complet-{ts}.json"
    out.write_text(json.dumps({"date": ts, "pages": pages, "arret": stop_reason, "sirens": sirens}, indent=1), encoding="utf-8")
    print(f"\n=== Liste Keobiz : {len(sirens)} entreprises ({len(pages)} requête(s) de liste) — {stop_reason}")
    print(f"Trafic : {stats['emis'] - stats['bloques'] + 1 + sent} appel(s) API envoyé(s) au portail au total "
          f"(1 vérif. maintenance + {stats['emis'] - stats['bloques']} appel(s) standard de la page + {sent} requête(s) de liste) ; "
          f"{stats['bloques']} appel(s) automatique(s) du site bloqué(s) avant envoi")

    # ---------------------------------------------------------- couverture (Salesforce uniquement)
    sf = SalesforceClient(config.sf_auth())
    where = f"Entite_Juridique_Calc__c = 'Keobiz' AND SIREN__c != null AND {config.CAMPAIGN_SF_FILTER}"
    total = sf._call(lambda c: c.query(f"SELECT COUNT() FROM Account WHERE {where}"))["totalSize"]
    accs = sf.get_accounts_by_sirens(sirens) if sirens else []
    covered = {a["siren"] for a in accs if a["entite"] == "Keobiz" and a["statut_mission"] == "MissionValidee"}
    print(f"\nClients Keobiz en mission validée (Salesforce) : {total}")
    print(f"  couverts par le compte portail Keobiz : {len(covered)} ({100 * len(covered) / max(total, 1):.1f} %)")

    collab = sorted(glob.glob(str(config.DATA_DIR / "habilitations-C2-*.json")), key=os.path.getmtime)
    if collab:
        vs = {m for it in json.load(open(collab[-1], encoding="utf-8")) if "pcr-habilitations" in it.get("path", "")
              for x in ((it.get("json") or {}).get("sirens") or []) for m in [str(x.get("siren"))]}
        vacc = {a["siren"] for a in sf.get_accounts_by_sirens(sorted(vs))
                if a["entite"] == "Keobiz" and a["statut_mission"] == "MissionValidee"}
        print(f"  couverts par le compte du collaborateur n°2 : {len(vacc)} — dont {len(vacc - covered)} que Keobiz NE couvre PAS")
        print(f"  couverture Keobiz + collaborateur n°2 : {len(covered | vacc)} ({100 * len(covered | vacc) / max(total, 1):.1f} %)")
    print(f"\nListe enregistrée : data/{out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
