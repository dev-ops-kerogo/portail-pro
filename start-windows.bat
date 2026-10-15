@echo off
REM Documents fiscaux — démarrage sous Windows. À lancer depuis le dossier du projet (double-clic).
cd /d "%~dp0"
set PYTHONUTF8=1

if not exist .venv\Scripts\python.exe (
  echo [1/3] Creation de l'environnement Python...
  py -3.12 -m venv .venv || python -m venv .venv || goto :erreur
  echo [2/3] Installation des dependances...
  .venv\Scripts\python -m pip install --upgrade pip -q
  .venv\Scripts\pip install -r requirements.txt -q || goto :erreur
)
if not exist .env  echo ATTENTION : fichier .env absent  (copier .env.example et le remplir)   & goto :erreur
if not exist cabinets.json echo ATTENTION : fichier cabinets.json absent & goto :erreur

echo [3/3] Lancement du service (Ctrl+C pour arreter)...
.venv\Scripts\python app.py
goto :fin

:erreur
echo.
echo Echec du demarrage : lire le message ci-dessus et app.log.
pause
:fin
