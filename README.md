# Documents fiscaux — application Slack

Un collaborateur demande depuis Slack l'envoi d'un document fiscal (V1 : avis d'imposition CFE) à un
client. Le service récupère le PDF sur portailpro.gouv.fr **par API** (compte du cabinet), l'envoie
par Gmail **au nom du chargé de mission** depuis l'adresse générique du cabinet (Reply-To → Email-to-Case),
l'archive sur la fiche Salesforce, et notifie le demandeur du résultat.

Reprend les décisions de la spec de juin (multi-cabinets via `Entite_Juridique_Calc__c`, envoi
automatique, `SKIPPED` + notification si document absent, archivage Salesforce, blocage si email
client vide, dédoublonnage), sans UiPath Orchestrator, MySQL ni application Node : **un seul
service Python**.

```
app.py               Slack (Bolt, Socket Mode) : App Home, /document, modale, validations, file d'attente
worker.py            thread de traitement : portailpro → PDF → Gmail → archivage → notification
portailpro.py        session portailpro par cabinet (login automatique) + API listerDocumentsEnsu / consulterDocumentEnsu
documents.py         registre des types de documents (CFE ; TVA/IS à ajouter) et récupération
salesforce_client.py recherche client (typeahead), fiche Account, archivage ContentVersion + EmailMessage
mailer.py            Gmail : compte de service (prod) ou OAuth utilisateur (tests), gabarit + signature cabinet
db.py                suivi des demandes (SQLite : document_requests, request_documents, request_events, campaigns, campaign_items)
campaign.py          campagnes : inventaire portail → rapport Excel → envoi en masse après validation (voir §7)
mysql_journal.py     journal d'activité MySQL (OVH) : une ligne par demande, pour le monitoring (facultatif)
config.py            .env + cabinets.json
cabinets.example.json  configuration par cabinet (compte portail, adresse d'envoi, Reply-To, signature)
slack_manifest.json  manifeste pour créer l'app Slack en un clic
templates/email.html corps de l'email par défaut ({civilite} {client_nom} {client_name} {siren} {annee} {signature}) ; un gabarit par cabinet via "template" dans cabinets.json
```

## 1. Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # inutile si Chrome est installé (BROWSER_CHANNEL=chrome)
cp .env.example .env
cp cabinets.example.json cabinets.json
```

### Installation sous Windows (VM de production)

1. Installer **Python 3.12** depuis python.org (cocher *Add python.exe to PATH*) et **Google Chrome**
   (sinon lancer `.venv\Scripts\playwright install chromium` et vider `BROWSER_CHANNEL` dans `.env`).
2. Copier le dossier du projet (sans `.venv/` ni `data/`) sur la VM, avec `.env`, `cabinets.json` et `secrets/`.
3. Double-cliquer sur `start-windows.bat` : il crée `.venv`, installe les dépendances et lance le service.
   Pour les tests : `.venv\Scripts\python app.py --test-sf transport` et `--test-portail Keobiz <SIREN>`.
4. Pour tourner en continu : **Planificateur de tâches** → Créer une tâche → déclencheur *Au démarrage*,
   action *Démarrer un programme* = `start-windows.bat` (répertoire de départ = dossier du projet), onglet Paramètres :
   *Si la tâche échoue, redémarrer toutes les 1 minute*. Désactiver la mise en veille de la VM.
5. La première connexion portailpro ouvre Chrome en arrière-plan ; le profil est conservé dans `data\chrome-profiles\`.
   Si un second facteur est demandé, mettre `PORTAIL_HEADLESS=false`, se connecter à la main une fois, puis remettre `true`.

## 2. Créer l'app Slack (5 minutes)

1. https://api.slack.com/apps → **Create New App** → **From a manifest** → choisir l'espace de travail → coller le contenu de `slack_manifest.json` → Create.
2. **Basic Information → App-Level Tokens** → Generate Token, scope `connections:write` → copier le `xapp-…` dans `SLACK_APP_TOKEN`.
3. **Install App** → Install to Workspace → copier le **Bot User OAuth Token** `xoxb-…` dans `SLACK_BOT_TOKEN`.
4. Optionnel : créer un canal `#docs-fiscaux-alertes`, y inviter l'app (`/invite @Documents fiscaux`) et mettre son ID dans `SLACK_ALERT_CHANNEL`.

## 3. Configurer

- **Salesforce** : par certificat (JWT, recommandé) `SALESFORCE_INSTANCE_URL`, `SALESFORCE_CLIENT_ID`, `SALESFORCE_USERNAME`,
  `SALESFORCE_PRIVATE_KEY` (chemin du fichier de clé, ex. `secrets/salesforce.key`) ; ou par mot de passe `SF_PASSWORD`,
  `SF_SECURITY_TOKEN`. Compte avec accès Account + création de fichiers.
- **Cabinets** : dans `cabinets.json`, une entrée par valeur de `Entite_Juridique_Calc__c` : identifiants
  portailpro (noms des variables d'env), adresse d'envoi, Reply-To (Email-to-Case), signature HTML
  (placeholders `{cdm_nom} {cdm_email} {cdm_telephone} {cabinet} {telephone} {adresse} {site_web} {reply_to} {logo}` ; `logo` = chemin PNG intégré à l'email). Les mots de passe restent dans `.env`.
- **Gmail** :
  - *Tests* : mêmes `client_secret.json` / `token.json` que le pipeline batch (OAuth utilisateur, scope gmail.send).
    Mettre `TEST_RECIPIENT=` votre adresse : tous les emails vous sont redirigés, avec le vrai destinataire dans l'objet.
  - *Production* : compte de service Google Workspace avec **délégation domain-wide** (scope
    `https://www.googleapis.com/auth/gmail.send`), fichier de clé dans `GOOGLE_SA_KEY_PATH`. Le service
    impersonne `sender_address` de chaque cabinet.
- **Comptes portail des chargés de mission** (clients habilités sur le compte personnel du collaborateur) : essayés après
  celui du cabinet. Source principale : un **Google Sheet des accès** (`CREDENTIALS_SHEET_ID`), colonnes « Mail de
  connexion », « Mot de passe », « Chargé de mission », partagé **en lecture** avec le compte technique Google
  (`CREDENTIALS_SA_KEY_PATH`) et relu toutes les `CREDENTIALS_REFRESH_S` secondes : nouveau compte ou mot de passe corrigé
  pris en compte sans redémarrage. Rattachement au client par l'email du chargé de mission dans Salesforce, sinon par son
  nom. Anomalies du Sheet (doublon, mot de passe manquant) et mots de passe refusés signalés dans le canal d'alertes ;
  les mots de passe ne sont jamais écrits ni journalisés. Sans Google Sheet : `PORTAIL_COLLAB<n>_*` dans `.env` (voir
  `.env.example`) ; avec un Sheet, ces identifiants ne sont plus utilisés (seul `ACTIF=false` compte), et tant que le
  Sheet n'a jamais pu être lu, aucun client n'est déclaré « non habilité » (inventaires reportés).
  Un mot de passe refusé par le portail n'est **plus jamais resoumis**, même après redémarrage (empreinte dans
  `data/portail-identifiants-refuses.json`, jamais le mot de passe) : le corriger suffit ; pour réessayer un mot de passe
  inchangé, supprimer ce fichier puis redémarrer le service.

## 4. Vérifier brique par brique

```bash
python app.py --test-sf "transport"              # recherche client Salesforce (nom ou début de SIREN)
python app.py --test-portail Keobiz 123456789 # connexion au compte portail du cabinet + liste des avis
python app.py --test-sheet                       # Google Sheet des accès : comptages seulement (aucun identifiant)
python app.py --test-compte c.durand@keobiz.fr 123456789   # compte portail d'un chargé de mission + liste des avis
python app.py --test-mysql                       # journal MySQL : crée la table, rejoue les demandes récentes
python app.py                                     # lance le service (Slack + worker)
```

Puis dans Slack : ouvrir l'app **Documents fiscaux** → *Nouvelle demande* (ou `/document`), choisir le
client, le document, l'année → *Envoyer au client*. La confirmation arrive en message direct.

## 5. Modes de test (du plus sûr au plus réel)

| Mode | Où | Effet |
|---|---|---|
| `DRY_RUN=true` | `.env` (global) | aucun email, aucun archivage ; les PDF et l'aperçu de l'email sont déposés dans Slack (`DRY_RUN_CHANNEL`, par défaut le canal d'alertes ; sinon message direct au demandeur). Bandeau dans l'App Home. |
| Case **« Mode test »** | dans la modale, par demande | idem, pour cette demande seulement. Ne compte pas dans le dédoublonnage. Utile pour vérifier un dossier réel sans risque. |
| `TEST_RECIPIENT=vous@...` | `.env` (global) | envoi Gmail **réel** mais à vous au lieu du client (vrai destinataire rappelé dans l'objet) ; archivage actif. |
| `ARCHIVE_TEST_ACCOUNT_ID=001...` | `.env` (global) | les PDF sont archivés sur ce compte Salesforce de test au lieu de la fiche du client. |

Mise en route conseillée : `DRY_RUN=true` pour les premières demandes, puis `TEST_RECIPIENT` pour valider
l'email tel qu'il arrive, puis rien (production). La case « Mode test » reste disponible en production.

## 6. Comportement

| Situation | Résultat |
|---|---|
| Email client vide / invalide dans Salesforce | refusé à la soumission, message dans la modale |
| Cabinet du client absent de `cabinets.json` | refusé à la soumission |
| Demande identique (client + document + année) dans les `DEDUP_DAYS` derniers jours | refusée, sauf case « Renvoyer » cochée |
| Plusieurs établissements (SIRET) | un PDF par établissement, tous joints au même email |
| Aucun avis pour l'année | statut `NO_DOCUMENT`, pas d'email, notification au demandeur |
| Mode test (global ou case cochée) | statut `DRY_RUN`, PDF + aperçu dans Slack, rien chez le client |
| Jeton portail expiré | rechargement automatique ; si le formulaire de connexion revient, reconnexion avec les identifiants du cabinet |
| Erreur technique (portail, Gmail) | statut `FAILED`, notification au demandeur + alerte dans `SLACK_ALERT_CHANNEL` |
| Redémarrage du service | les demandes `PENDING`/`PROCESSING` sont reprises |
| Archivage Salesforce (`ARCHIVE_TO_SALESFORCE=true`) | les PDF sont déposés dans les fichiers de la fiche Account, puis l'email envoyé est enregistré dans sa chronologie d'activités (objet EmailMessage, statut Envoyé, PDF rattachés) |
| Fichiers | `data/requests/<id>/`, purgés après `DOC_RETENTION_DAYS` (les PDF sont sur la fiche Salesforce si archivage activé) |

## 7. Campagnes (traitement en masse)

`/document campagne` (réservé aux `CAMPAIGN_ADMINS`) ouvre une modale : cabinet, document, année, clients concernés
(liste de SIREN pour les tests, ou tous les clients du cabinet selon `CAMPAIGN_SF_FILTER`), limite d'envois facultative.

1. **Inventaire** : le service interroge le portail pour chaque client (délai `CAMPAIGN_INVENTORY_INTERVAL_S`) et dépose
   dans `CAMPAIGN_CHANNEL` un Excel : avis trouvé / aucun avis / email manquant / déjà envoyé / non habilité / erreur. **Rien n'est envoyé.**
2. **Envoi** : uniquement après le clic « Lancer l'envoi » d'un administrateur (avec confirmation). Une demande par client
   retenu, traitée par le worker habituel (mêmes garde-fous), au rythme `CAMPAIGN_SEND_INTERVAL_S`, dans la limite de
   `CAMPAIGN_DAILY_LIMIT` emails réels par jour (quota Gmail) avec reprise automatique à `CAMPAIGN_RESUME_HOUR`.
   Message de progression, puis rapport final Excel. « Annuler la campagne » arrête l'envoi (les demandes déjà en file se terminent).

Une seule campagne à la fois. Les demandes de campagne portent `campaign_id` (SQLite et journal MySQL) et ne génèrent pas
de message direct par client. Un redémarrage du service interrompt un inventaire (à relancer) mais reprend un envoi en cours.
Fichiers : `data/campaigns/<id>/`.

## 8. Ajouter un type de document

Ajouter une entrée dans `DOCUMENT_TYPES` (`documents.py`) et une fonction `fetch_<code>` qui renvoie des
`DocResult`. Le champ « Période » apparaît automatiquement dans la modale si `needs_periode` est vrai.
Slack, Salesforce, l'email et le suivi ne changent pas.

## 9. Exploitation

- Le service doit tourner en continu (poste dédié ou serveur avec Chrome/Chromium). Un profil Chrome
  par cabinet est conservé dans `data/chrome-profiles/` pour éviter de se reconnecter à chaque fois.
- Si portailpro impose un second facteur, la connexion automatique échoue : lancer avec
  `PORTAIL_HEADLESS=false` sur un poste avec écran, se connecter à la main une fois, le profil garde la session.
- Vérifier les CGU de portailpro pour l'accès scripté, et les mandats de représentation par cabinet.
- Contrôle d'accès : `SLACK_ALLOWED_USERS` (IDs Slack) ; vide = tout l'espace de travail.
