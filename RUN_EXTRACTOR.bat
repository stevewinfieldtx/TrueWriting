@echo off
echo ============================================
echo TrueWriting PST Extractor - Setup and Run
echo ============================================
echo.
echo Step 1: Installing required library...
py -m pip install libpff-python
echo.
echo Step 2: Running extraction...
echo This may take a few minutes for a 1.2GB PST file.
echo.
py "C:\Users\steve\Documents\TrueWriting\extract_sent_email.py"
echo.
echo ============================================
echo Done! If successful, upload this file to Claude:
echo C:\Users\steve\Documents\TrueWriting\corpus_sent.json
echo ============================================
pause
