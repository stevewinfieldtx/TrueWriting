@echo off
echo ============================================================
echo   TrueWriting Analyzer - Setup
echo ============================================================
echo.
echo Installing dependencies...
py -m pip install textstat nltk numpy pywin32 --quiet
echo.
echo Downloading NLTK data...
py -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True); nltk.download('stopwords', quiet=True); nltk.download('averaged_perceptron_tagger', quiet=True); nltk.download('averaged_perceptron_tagger_eng', quiet=True)"
echo.
echo ============================================================
echo   Setup complete!
echo.
echo   Usage:
echo     py run.py --pst "C:\path\to\email.pst"
echo     py run.py --mbox "C:\path\to\sent.mbox"
echo     py run.py --eml-dir "C:\path\to\eml_files"
echo.
echo   Your Trustifi PST:
echo     py run.py --pst "C:\Users\SteveWinfield\OneDrive - Winfield Technology\Trustifi Email.pst"
echo ============================================================
pause
