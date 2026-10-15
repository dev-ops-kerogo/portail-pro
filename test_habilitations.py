"""
Test A — liste des entreprises accessibles par chaque compte portailpro (LECTURE SEULE, DISCRET).

Principe : on n'interroge AUCUN client. On ouvre simplement deux pages du portail, comme un humain dans
son navigateur, et on récupère au passage les réponses que le site charge de lui-même :
    pcr-habilitations/v1/habilitations/{id}   → liste des SIREN accessibles par le compte
    api-rattachement/v1/rattachements/        → rattachements du compte (DGFiP, URSSAF, Douane…)
Puis on croise les SIREN trouvés avec Salesforce (aucune requête portail pour ça).

Trafic portail : 1 vérification de maintenance + 1 connexion (collaborateur) + 2 pages par compte.
Le script compte et affiche toutes les requêtes envoyées au portail.

Usage : .venv/bin/python test_habilitations.py [--collab 2]
Sorties : data/test-habilitations-AAAAMMJJ-HHMMSS.md (+ JSON bruts des réponses, à côté)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from urllib.parse import urlparse, parse_qs

import config
from portailpro import START_URL, PortailPro, PortailProError
from salesforce_client import SalesforceClient
from test_comptes_collab import connect, load_accounts, portail_en_maintenance

HOME_URL = "https://portailpro.gouv.fr/"
CAPTURE = re.compile(r"pcr-habilitations|api-rattachement")


def nine_digits(obj) -> list[str]:
    """Tous les nombres à 9 chiffres (SIREN) contenus dans une structure JSON."""
    found = []
    if isinstance(obj, dict):
        for v in obj.values():
            found += nine_digits(v)
    elif isinstance(obj, list):
        for v in obj:
            found += nine_digits(v)
    elif isinstance(obj, (str, int)):
        found += re.findall(r"(?<!\d)\d{9}(?!\d)", str(obj))
    return found


def shape(obj, depth: int = 0) -> str:
    """Description compacte de la structure (clés, longueurs) — sans les valeurs."""
    if depth > 2:
        return "…"
    if isinstance(obj, dict):
        return "{" + ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(obj.items())[:12]) + "}"
    if isinstance(obj, list):
        return f"[{len(obj)} × {shape(obj[0], depth + 1) if obj else '∅'}]"
    return type(obj).__name__


def settle(p: PortailPro) -> None:
    try:
        p.page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    p.page.wait_for_timeout(3000)


def read_caught(caught: list, page_label: str) -> list[dict]:
    """Lit le corps des réponses capturées AVANT de quitter la page (sinon il n'est plus disponible)."""
    out = []
    for r in caught:
        u = urlparse(r.url)
        item = {"page": page_label, "path": u.path, "query": parse_qs(u.query), "status": r.status}
        try:
            item["json"] = r.json()
        except Exception as e:
            item["error"] = type(e).__name__
        out.append(item)
    caught.clear()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Liste des entreprises accessibles par compte portailpro (discret)")
    ap.add_argument("--collab", default="2", help="collaborateur(s) à inclure en plus de Keobiz (défaut : 2)")
    args = ap.parse_args()
    only = {int(x) for x in re.findall(r"\d", args.collab)} or None

    if subprocess.run(["pgrep", "-f", "app.py"], capture_output=True).returncode == 0:
        print("❌ Le service (app.py) tourne : arrête-le d'abord.")
        return 1
    if portail_en_maintenance():
        print("🛠 Portail en maintenance : test annulé (aucune connexion).")
        return 2

    accounts, notes = load_accounts(only)
    for n in notes:
        print(f"⚠️  {n}")
    ts = time.strftime("%Y%m%d-%H%M%S")
    results: dict[str, dict] = {}
    total_requests = 1   # la vérification de maintenance

    for a in accounts:
        print(f"\n▶ {a['label']}")
        counter = {"n": 0, "api": 0}
        caught: list = []

        def on_request(req, c=counter):
            host = urlparse(req.url).netloc
            if "portailpro.gouv.fr" in host:
                c["n"] += 1
                if host.startswith("services."):
                    c["api"] += 1

        def on_response(resp, lst=caught):
            if CAPTURE.search(resp.url):
                lst.append(resp)

        p = None
        res = {"login": None, "responses": [], "requests": 0, "api": 0}
        try:
            p = PortailPro(a["profile"], headless=a["headless"], channel=config.BROWSER_CHANNEL,
                           login=a["login"], password=a["password"])
            p._ctx.on("request", on_request)
            p._ctx.on("response", on_response)
            res["login"] = "✅ " + connect(p, a["label"], interactive=not a["headless"])
            print(f"  connecté — {res['login']}")
            settle(p)
            res["responses"] += read_caught(caught, "avis d'imposition")
            p.page.goto(HOME_URL, wait_until="domcontentloaded")
            settle(p)
            res["responses"] += read_caught(caught, "accueil")
        except PortailProError as e:
            res["login"] = f"❌ {e}"
            print(f"  ❌ {e}")
        except Exception as e:
            res["login"] = f"❌ {type(e).__name__}: {str(e)[:150]}"
            print(f"  ❌ {type(e).__name__}: {e}")
        finally:
            res["requests"], res["api"] = counter["n"], counter["api"]
            total_requests += counter["api"]
            if p:
                try:
                    p.close()
                except Exception:
                    pass
        sirens = sorted({s for it in res["responses"] if "pcr-habilitations" in it["path"]
                         for s in nine_digits(it.get("json", {}).get("sirens") if isinstance(it.get("json"), dict) else it.get("json"))})
        res["sirens"] = sirens
        results[a["key"]] = res
        raw = config.DATA_DIR / f"habilitations-{a['key']}-{ts}.json"
        raw.write_text(json.dumps(res["responses"], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {len(res['responses'])} réponse(s) capturée(s), {len(sirens)} SIREN, {counter['n']} requête(s) portail "
              f"— brut : {raw.name}")

    # ---------------------------------------------------------- Salesforce (aucune requête portail)
    all_sirens = sorted({s for r in results.values() for s in r["sirens"]})
    sf_by_siren = {}
    if all_sirens:
        try:
            for acc in SalesforceClient(config.sf_auth()).get_accounts_by_sirens(all_sirens):
                sf_by_siren.setdefault(acc["siren"], acc)
        except Exception as e:
            print(f"⚠️  Salesforce : {type(e).__name__}: {e}")

    # ---------------------------------------------------------- rapport
    L = [f"# Entreprises accessibles par compte portailpro — {time.strftime('%d/%m/%Y %H:%M')}", "",
         f"Appels API au portail : **{total_requests}** (dont 1 vérification de maintenance), "
         "aucun client interrogé individuellement.", ""]
    for a in accounts:
        r = results[a["key"]]
        L += [f"## {a['label']}", "", f"- Connexion : {r['login']}", f"- Appels API portail : {r['api']} (chargement de pages compris : {r['requests']} requêtes)",
              f"- **SIREN accessibles trouvés : {len(r['sirens'])}**", "", "Réponses capturées :", ""]
        for it in r["responses"]:
            q = {k: v[0] if len(v) == 1 else v for k, v in it["query"].items()}
            j = it.get("json")
            n9 = len(set(nine_digits(j))) if j is not None else 0
            L.append(f"- [{it['page']}] `{it['path']}` query={q} → HTTP {it['status']} · "
                     f"structure={shape(j) if j is not None else it.get('error')} · {n9} numéro(s) à 9 chiffres")
        hints = sorted({k for it in r["responses"] if isinstance(it.get("json"), dict) for k in it["json"]
                        if re.search(r"total|nombre|count|page|taille|size|suivant|next", k, re.I)})
        if hints:
            L.append(f"\n> Indices de pagination / total dans les réponses : {', '.join(hints)}")
        if r["sirens"]:
            L += ["", "| SIREN | Client (Salesforce) | Cabinet | Chargé de mission | Statut mission |", "|---|---|---|---|---|"]
            for s in r["sirens"]:
                acc = sf_by_siren.get(s)
                if acc:
                    L.append(f"| {s} | {acc['name'][:40]} | {acc['entite']} | {acc['cdm_nom']} | {acc['statut_mission']} |")
                else:
                    L.append(f"| {s} | *(absent de Salesforce ou mission annulée)* | | | |")
            in_sf = [s for s in r["sirens"] if s in sf_by_siren]
            mine = [s for s in in_sf if a["email_sf"] and sf_by_siren[s]["cdm_email"].lower() == a["email_sf"].lower()]
            L += ["", f"- Dans Salesforce : **{len(in_sf)}/{len(r['sirens'])}**"
                  + (f" · dont **{len(mine)}** où {a['label']} est chargé de mission" if a["email_sf"] else "")]
        L.append("")

    out = config.DATA_DIR / f"test-habilitations-{ts}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n✅ Rapport : {out}\n   Appels API au portail : {total_requests} au total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
