"""
Test multi-comptes portailpro (LECTURE SEULE) — ne modifie pas le service.

But : savoir quels clients sont visibles sur quel compte (cabinet vs collaborateurs) et ce que répond
le portail quand un client n'est PAS rattaché au compte.

Ce que fait le script, compte par compte (l'un après l'autre) :
  1. connexion au portail (fenêtre Chrome visible pour les collaborateurs : saisir le code si un
     second facteur est demandé) ;
  2. relève les appels API faits par le site (pour repérer un éventuel « liste de mes entreprises ») ;
  3. interroge la LISTE des avis (listerDocumentsEnsu) pour :
       - quelques clients de chaque collaborateur (chargé de mission dans Salesforce),
       - les demandes « sans document » (NO_DOCUMENT) de la base.
Garanties : aucun document n'est ouvert/téléchargé (un avis consulté passerait en « lu » sur le portail),
aucun email, aucun message Slack, aucune écriture en base. Aucun identifiant n'est affiché.

Usage : .venv/bin/python test_comptes_collab.py [--par-collab 5]
Rapport : data/test-comptes-AAAAMMJJ-HHMMSS.md
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import config
import portailpro
from portailpro import API_BASE, LIST_PATH, DOC_PATH, START_URL, PortailPro, PortailProError
from salesforce_client import ACCOUNT_FIELDS, SalesforceClient, _normalize

ACCEPT_JSON = "application/json, text/plain, */*"
PAUSE_S = max(config.CAMPAIGN_INVENTORY_INTERVAL_S, 1.0)   # politesse envers le portail


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", s.lower()).strip("-") or "x"


def soql_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ------------------------------------------------------------------ comptes
def load_accounts(only: set[int] | None = None) -> tuple[list[dict], list[str]]:
    notes = []
    cabinets = config.load_cabinets()
    keo = cabinets.get("Keobiz")
    accounts = []
    if keo and keo.portal_login and keo.portal_password:
        accounts.append({"key": "Keobiz", "label": "Keobiz (cabinet)", "login": keo.portal_login,
                         "password": keo.portal_password, "email_sf": None,
                         "profile": config.PROFILES_DIR / "Keobiz", "headless": config.PORTAIL_HEADLESS})
    else:
        notes.append("Compte Keobiz absent ou incomplet dans .env/cabinets.json")
    for i in (1, 2, 3):
        if only and i not in only:
            continue
        nom = (config.env(f"PORTAIL_COLLAB{i}_NOM") or "").strip()
        email = (config.env(f"PORTAIL_COLLAB{i}_EMAIL_SF") or "").strip() or None
        login = (config.env(f"PORTAIL_COLLAB{i}_LOGIN") or "").strip()
        pwd = config.env(f"PORTAIL_COLLAB{i}_PASSWORD")
        if not (login and pwd):
            if login or pwd or nom or email:
                notes.append(f"Collaborateur {i} : identifiant ou mot de passe manquant dans .env — ignoré")
            continue
        if not email and "@" in login:
            email = login   # l'identifiant portailpro est en général l'email du collaborateur
        nom = nom or f"Collaborateur {i}"
        profile = config.PROFILES_DIR / ("collab-" + slug(email.split("@")[0] if email else f"collab{i}"))
        accounts.append({"key": f"C{i}", "label": nom, "login": login, "password": pwd, "email_sf": email,
                         "profile": profile, "headless": False})   # fenêtre visible : 2nd facteur éventuel
    return accounts, notes


# ------------------------------------------------------------------ SIREN à tester
def collab_clients(accounts: list[dict], per_collab: int) -> list[dict]:
    """Quelques clients de chaque collaborateur (chargé de mission dans Salesforce)."""
    sf = SalesforceClient(config.sf_auth())
    out = []
    for a in accounts:
        if not a["email_sf"]:
            if a["key"] != "Keobiz":
                print(f"   ⚠️  {a['label']} : identifiant qui n'est pas un email → impossible de le retrouver dans Salesforce")
            continue
        em = soql_str(a["email_sf"])
        # Qui est-ce dans Salesforce ? (complète le nom si le .env ne le donne pas)
        try:
            users = sf._call(lambda c: c.query(f"SELECT Name, Email, IsActive FROM User WHERE Email = '{em}' LIMIT 1"))["records"]
        except Exception:
            users = []
        if users and a["label"].startswith("Collaborateur "):
            a["label"] = users[0]["Name"]
        where = f"ChargeDeMission__r.Email = '{em}' AND SIREN__c != null AND {config.CAMPAIGN_SF_FILTER}"
        try:
            total = sf._call(lambda c: c.query(f"SELECT COUNT() FROM Account WHERE {where}"))["totalSize"]
            records = sf._call(lambda c: c.query(f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE {where} "
                                                 f"ORDER BY Name LIMIT {int(per_collab)}"))["records"]
        except Exception as e:
            print(f"   ⚠️  Salesforce ({a['label']}) : {type(e).__name__}: {e}")
            total, records = 0, []
        statut = ("utilisateur Salesforce trouvé" if users else "⚠️ aucun utilisateur Salesforce avec cet email")
        print(f"   {a['label']} <{a['email_sf']}> : {statut} — chargé de mission sur {total} client(s) (mission validée)")
        for r in records:
            n = _normalize(r)
            if len(n["siren"]) == 9:
                out.append({"siren": n["siren"], "name": n["name"], "entite": n["entite"],
                            "cdm": n["cdm_nom"], "owner": a["key"], "annee": None, "source": "collab"})
    return out


def temoin_requests(limit: int = 4) -> list[dict]:
    """Témoins positifs : clients pour lesquels le compte Keobiz AVAIT trouvé la CFE (envoyée ou testée)."""
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    rows = con.execute("SELECT siren, annee, client_name, max(created_at) FROM document_requests "
                       "WHERE status IN ('SENT', 'DRY_RUN') GROUP BY siren, annee ORDER BY 4 DESC LIMIT ?",
                       (limit,)).fetchall()
    con.close()
    return [{"siren": s, "name": n or "", "entite": "", "cdm": "", "owner": "Keobiz", "annee": str(an), "source": "temoin"}
            for s, an, n, _ in rows if s]


def no_document_requests() -> list[dict]:
    """Demandes passées en « sans document » (lecture seule de la base)."""
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    rows = con.execute("SELECT siren, annee, client_name, max(created_at) FROM document_requests "
                       "WHERE status = 'NO_DOCUMENT' GROUP BY siren, annee ORDER BY 4").fetchall()
    con.close()
    return [{"siren": s, "name": n or "", "entite": "", "cdm": "", "owner": None, "annee": str(an), "source": "no_document"}
            for s, an, n, _ in rows if s]


# ------------------------------------------------------------------ portail
LOGIN_REFUSED = re.compile(r"mot de passe est incorrect|courriel et/ou votre mot de passe|identifiants? incorrect", re.I)


def single_attempt_login(p: PortailPro) -> str:
    """UNE seule soumission du formulaire (pas de nouvel essai) : plusieurs mots de passe erronés
    peuvent bloquer un compte. S'arrête net si le portail répond « mot de passe incorrect »."""
    page = p.page
    if portailpro.is_refused(p.login_id, p.password) or portailpro.login_blocked_until(p.login_id, p.password):
        raise PortailProError("Identifiants déjà refusés (ou connexion suspendue) par le service — aucune tentative envoyée")
    p._dismiss_cookie_banner()
    p._wait_form_ready()
    ident, pwd = p._find_ident(), page.locator("input[type='password']").first
    for _ in (1, 2):   # remplissage (pas soumission) : le bandeau cookies peut vider les champs
        ident.fill(p.login_id); pwd.fill(p.password); page.wait_for_timeout(300)
        if ident.input_value() == p.login_id and pwd.input_value():
            break
        p._dismiss_cookie_banner()
    else:
        raise PortailProError("Le formulaire refuse la saisie (bandeau cookies ?) — aucune tentative envoyée")
    btn = page.get_by_role("button", name=re.compile("connexion|se connecter|valider", re.I)).first
    if btn.count():
        try:
            btn.click(timeout=10000)
        except Exception:
            p._dismiss_cookie_banner(); btn.click(force=True)
    else:
        pwd.press("Enter")
    deadline = time.time() + 30
    while time.time() < deadline:
        if p.token is not None:
            return "connexion par identifiants"
        try:
            body = page.inner_text("body", timeout=2000)
        except Exception:
            body = ""
        if LOGIN_REFUSED.search(body):
            portailpro.mark_refused(p.login_id, p.password)   # partagé avec le service : jamais resoumis
            raise PortailProError("Identifiant ou mot de passe REFUSÉ par le portail "
                                  "(1 seule tentative envoyée, aucun nouvel essai)")
        if not p._login_form_visible(timeout_ms=1000) and not re.search(r"/ms/login", page.url):
            return "connexion par identifiants (étape supplémentaire éventuelle)"
        page.wait_for_timeout(1000)
    raise PortailProError("Pas de réponse claire du portail après la tentative (aucun nouvel essai)")


def connect(p: PortailPro, label: str, interactive: bool) -> str:
    p.page.goto(START_URL, wait_until="domcontentloaded")
    if p._login_form_visible(timeout_ms=15000):
        # Collaborateurs : une seule tentative ; Keobiz : connexion habituelle du service.
        if not interactive and portailpro.is_refused(p.login_id, p.password):
            raise PortailProError("Identifiants déjà refusés par le service — aucune tentative envoyée")
        how = single_attempt_login(p) if interactive else (p._fill_login_form() or "connexion par identifiants")
    else:
        how = "session déjà ouverte (profil Chrome mémorisé)"
    if p.token is None:
        if interactive:
            print(f"   ⏳ {label} : si un code de vérification est demandé, saisis-le dans la fenêtre Chrome (5 min max)…",
                  flush=True)
        p._wait_token(300 if interactive else 90)
        if interactive and how.startswith("connexion"):
            how += " + étape manuelle éventuelle"
    return how


def probe(p: PortailPro, siren: str) -> dict:
    """UNIQUEMENT la liste des avis (jamais consulterDocumentEnsu : ouvrir un avis le marque « lu »)."""
    try:
        r = p._get(LIST_PATH, {"siren": siren}, ACCEPT_JSON)
    except Exception as e:
        return {"status": "ERR", "error": f"{type(e).__name__}: {str(e)[:120]}"}
    out: dict = {"status": r.status_code}
    try:
        data = r.json()
    except Exception:
        # Pendant une maintenance, le portail répond 200 avec une page HTML : surtout ne pas conclure « vide ».
        txt = r.text or ""
        out["status"] = "MAINTENANCE" if re.search(r"maintenance|indisponible", txt, re.I) else "NON-JSON"
        out["body"] = txt[:200]
        return out
    if isinstance(data, dict):
        res = data.get("resultat") or []
        out["n"] = len(res)
        out["cfe_years"] = sorted({str(x.get("annee")) for x in res
                                   if "cotisation fonci" in (x.get("libelle") or "").lower()}, reverse=True)
        out["types"] = sorted({(x.get("libelle") or "?") for x in res})
        out["infos"] = data.get("infos")
        out["other_keys"] = sorted(k for k in data if k not in ("resultat", "infos"))
    else:
        out["body"] = str(data)[:200]
    return out


def discover(p: PortailPro) -> list[str]:
    """Visite l'accueil après connexion et relève les API appelées (hors liste/avis) : structure seulement."""
    found, responses = set(), []

    def on_resp(resp):
        u = urlparse(resp.url)
        if "portailpro.gouv.fr" in u.netloc and (u.netloc.startswith("services.") or "/api" in u.path):
            if not (u.path.startswith(LIST_PATH) or u.path.startswith(DOC_PATH)):
                responses.append(resp)

    p._ctx.on("response", on_resp)
    for url in (START_URL, "https://portailpro.gouv.fr/"):
        try:
            p.page.goto(url, wait_until="domcontentloaded")
            p.page.wait_for_timeout(6000)
        except Exception:
            pass
    p._ctx.remove_listener("response", on_resp)
    for resp in responses:
        u = urlparse(resp.url)
        desc = f"{resp.request.method} {u.netloc}{u.path} → {resp.status}"
        try:
            body = resp.body()
            n_siren = len(set(re.findall(rb"(?<!\d)\d{9}(?!\d)", body)))
            keys = ""
            if body[:1] in (b"{", b"["):
                import json
                j = json.loads(body)
                if isinstance(j, dict):
                    keys = " clés=" + ",".join(sorted(j.keys())[:8])
                elif isinstance(j, list):
                    keys = f" liste de {len(j)} élément(s)"
            desc += f"{keys} — {n_siren} numéro(s) à 9 chiffres"
        except Exception:
            pass
        found.add(desc)
    return sorted(found)


# ------------------------------------------------------------------ rapport
def cell(res: dict | None, annee: str | None) -> str:
    if res is None:
        return "—"
    if res.get("status") == "ERR":
        return "erreur"
    if res.get("status") == "MAINTENANCE":
        return "🛠 maintenance"
    if res.get("status") == "NON-JSON":
        return "⚠️ réponse inattendue"
    if res.get("status") != 200:
        return f"HTTP {res['status']}"
    n = res.get("n", 0)
    if n == 0:
        return "∅ vide"
    years = res.get("cfe_years") or []
    if annee:
        return (f"✅ CFE {annee}" if annee in years else f"👁 vu, pas de CFE {annee}") + f" ({n} avis)"
    return (f"✅ CFE {','.join(years[:3])}" if years else "👁 vu, aucune CFE") + f" ({n} avis)"


def portail_en_maintenance() -> bool:
    """L'API répond normalement 401 sans jeton ; en maintenance elle renvoie une page HTML « maintenance »."""
    import httpx
    try:
        r = httpx.get(API_BASE + LIST_PATH, timeout=20, follow_redirects=True)
    except Exception:
        return False   # injoignable : on laisse la suite le constater
    return r.status_code == 200 and bool(re.search(r"maintenance|indisponible", r.text or "", re.I))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--par-collab", type=int, default=5, help="clients Salesforce testés par collaborateur (défaut 5)")
    ap.add_argument("--salesforce-seulement", action="store_true",
                    help="vérifie seulement que les collaborateurs sont reconnus dans Salesforce (aucun accès au portail)")
    ap.add_argument("--collab", default="1,2,3", help="numéros des collaborateurs à tester, ex. 2 ou 1,3 (défaut : tous)")
    args = ap.parse_args()
    only = {int(x) for x in re.findall(r"\d", args.collab)} or None

    if subprocess.run(["pgrep", "-f", "app.py"], capture_output=True).returncode == 0:
        print("❌ Le service (app.py) tourne : arrête-le d'abord (les profils Chrome ne peuvent pas être partagés).")
        return 1

    print("1) Comptes déclarés")
    accounts, notes = load_accounts(only)
    for a in accounts:
        print(f"   • {a['label']}" + (f"  (Salesforce : {a['email_sf']})" if a["email_sf"] else ""))
    for n in notes:
        print(f"   ⚠️  {n}")
    if len(accounts) < 2:
        print("❌ Il faut au moins le compte Keobiz + un compte collaborateur.")
        return 1

    print("2) Clients à tester")
    targets = collab_clients(accounts, args.par_collab)
    if args.salesforce_seulement:
        print(f"\n✅ Vérification Salesforce terminée ({len(targets)} client(s) d'exemple). Aucun accès au portail effectué.")
        return 0
    targets = temoin_requests() + targets + no_document_requests()
    seen, uniq = set(), []
    for t in targets:
        k = (t["siren"], t["annee"])
        if k not in seen:
            seen.add(k); uniq.append(t)
    targets = uniq
    sirens = sorted({t["siren"] for t in targets})
    print(f"   {len(targets)} cas, {len(sirens)} SIREN distincts")

    if portail_en_maintenance():
        print("\n🛠 Le portail est EN MAINTENANCE : test annulé (aucune connexion faite). Réessaie quand il est revenu.")
        return 2

    print("3) Interrogation du portail, compte par compte (liste des avis uniquement)")
    login_info, apis, matrix = {}, {}, {}   # matrix[(siren, key)] = résultat
    for a in accounts:
        print(f"\n   ▶ {a['label']}")
        t0 = time.time()
        p = None
        try:
            p = PortailPro(a["profile"], headless=a["headless"], channel=config.BROWSER_CHANNEL,
                           login=a["login"], password=a["password"])
            how = connect(p, a["label"], interactive=not a["headless"])
            login_info[a["key"]] = ("✅ connecté", how, round(time.time() - t0))
            print(f"     connecté ({how}, {round(time.time() - t0)} s)")
            # Vérifications d'abord, avec le jeton de la page des avis (la découverte change de page → autre jeton).
            for i, s in enumerate(sirens, 1):
                t1 = time.time()
                matrix[(s, a["key"])] = probe(p, s)
                tag = next((t["source"] for t in targets if t["siren"] == s), "")
                tag = {"temoin": " [témoin]", "no_document": " [sans document]"}.get(tag, "")
                print(f"     {i:>2}/{len(sirens)} {s}{tag} : {cell(matrix[(s, a['key'])], None)} ({time.time() - t1:.1f} s)",
                      flush=True)
                time.sleep(PAUSE_S)
            apis[a["key"]] = discover(p)
        except PortailProError as e:
            login_info[a["key"]] = ("❌ échec", str(e)[:160], round(time.time() - t0))
            print(f"     ❌ {e}")
        except Exception as e:
            login_info[a["key"]] = ("❌ erreur", f"{type(e).__name__}: {str(e)[:140]}", round(time.time() - t0))
            print(f"     ❌ {type(e).__name__}: {e}")
        finally:
            if p:
                try:
                    p.close()
                except Exception:
                    pass

    # ---------------------------------------------------------- rapport
    keys = [a["key"] for a in accounts]
    labels = {a["key"]: a["label"] for a in accounts}
    L = [f"# Test multi-comptes portailpro — {time.strftime('%d/%m/%Y %H:%M')}", "",
         "Lecture seule : listes d'avis uniquement (aucun avis ouvert, aucun email, aucune écriture).", ""]
    L += ["## 1. Connexions", "", "| Compte | Résultat | Détail | Durée |", "|---|---|---|---|"]
    for k in keys:
        r = login_info.get(k, ("—", "non testé", 0))
        L.append(f"| {labels[k]} | {r[0]} | {r[1]} | {r[2]} s |")
    for n in notes:
        L.append(f"\n> ⚠️ {n}")

    hdr = "| Client | SIREN | " + " | ".join(labels[k] for k in keys) + " |"
    sep = "|---|---|" + "---|" * len(keys)
    tem = [t for t in targets if t["source"] == "temoin"]
    temoins_ok = 0
    L += ["", "## Contrôle : clients témoins (Keobiz avait trouvé leur CFE en septembre)", "",
          "S'ils sont bien vus ici par Keobiz, le test est fiable.", "",
          hdr.replace("| SIREN |", "| SIREN | Année |"), sep + "---|"]
    for t in tem:
        res = {k: matrix.get((t["siren"], k)) for k in keys}
        if res.get("Keobiz") and t["annee"] in (res["Keobiz"].get("cfe_years") or []):
            temoins_ok += 1
        cells = " | ".join(cell(res[k], t["annee"]) for k in keys)
        L.append(f"| {t['name'][:40]} | {t['siren']} | {t['annee']} | {cells} |")

    L += ["", "## 2. Clients des collaborateurs (chargé de mission dans Salesforce)", "",
          "Question : ces clients sont-ils visibles sur le compte du cabinet, ou seulement sur celui du collaborateur ?", "",
          hdr.replace("| SIREN |", "| SIREN | Chargé de mission |"), sep + "---|"]
    for t in [t for t in targets if t["source"] == "collab"]:
        cells = " | ".join(cell(matrix.get((t["siren"], k)), None) for k in keys)
        L.append(f"| {t['name'][:40]} | {t['siren']} | {labels.get(t['owner'], '')} | {cells} |")

    L += ["", "## 3. Les demandes « sans document » : étaient-elles vraiment sans CFE ?", "",
          hdr.replace("| SIREN |", "| SIREN | Année |") + " Verdict |", sep + "---|---|"]
    recovered = 0
    for t in [t for t in targets if t["source"] == "no_document"]:
        res = {k: matrix.get((t["siren"], k)) for k in keys}
        found_on = [labels[k] for k in keys if res[k] and t["annee"] in (res[k].get("cfe_years") or [])]
        verdict = ("🔎 CFE trouvée sur : " + ", ".join(found_on)) if found_on else "aucune CFE sur ces comptes"
        if found_on and "Keobiz (cabinet)" not in found_on:
            recovered += 1
        cells = " | ".join(cell(res[k], t["annee"]) for k in keys)
        L.append(f"| {t['name'][:40]} | {t['siren']} | {t['annee']} | {cells} | {verdict} |")

    L += ["", "## 4. Réponses vides : que dit exactement le portail ?", ""]
    empties = {}
    for (s, k), r in matrix.items():
        if r.get("status") != 200 or r.get("n", 0) == 0:
            sig = f"HTTP {r.get('status')} · infos={r.get('infos')!r} · autres clés={r.get('other_keys')} · corps={r.get('body', '')[:80]!r}"
            empties.setdefault(sig, []).append(f"{s}@{labels[k]}")
    if not empties:
        L.append("Aucune réponse vide.")
    for sig, where in empties.items():
        L.append(f"- `{sig}` — {len(where)} cas (ex. {', '.join(where[:3])})")

    L += ["", "## 5. Appels API observés après connexion (piste « liste des entreprises du compte »)", ""]
    for k in keys:
        L.append(f"**{labels[k]}**")
        L += [f"- {x}" for x in apis.get(k, [])] or ["- (aucun)"]
        L.append("")

    vis = {k: sum(1 for s in sirens if (matrix.get((s, k)) or {}).get("n", 0) > 0) for k in keys}
    fiable = bool(tem) and temoins_ok == len(tem)
    L += ["## Synthèse", "",
          f"- Témoins retrouvés par Keobiz : **{temoins_ok}/{len(tem)}** → "
          + ("test fiable ✅" if fiable else "⚠️ résultats À NE PAS CROIRE tels quels (le test ne voit pas ce qu'il devrait voir)"),
          *[f"- {labels[k]} : voit **{vis[k]}** SIREN sur {len(sirens)} testés" for k in keys],
          f"- Demandes « sans document » dont la CFE existe en fait **sur un compte collaborateur** : **{recovered}**", ""]

    out = config.DATA_DIR / f"test-comptes-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n✅ Rapport : {out}")
    print("\n".join(L[-(len(keys) + 5):]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
