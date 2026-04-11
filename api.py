"""
TrueWriting API - FastAPI wrapper for the CPP analysis engine.
Deployed on Railway. Called by TrueEngine, ClearSignals, LifeStages, etc.

Endpoints:
  POST /analyze     - Generate TW-0 voice profile from text
  GET  /health      - Health check

No CLI flags. No human intervention. Services POST text, get back CPP JSON.
"""

import os
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

from analyzer import TranscriptIngester, EmailIngester, TrueWritingAnalyzer

app = FastAPI(title="TrueWriting API", version="0.4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Optional API key protection
API_KEY = os.getenv("TRUEWRITING_API_KEY", "")


# ── Request Models ──────────────────────────────────────────

class TextSegment(BaseModel):
    text: str
    source_id: Optional[str] = ''
    title: Optional[str] = ''
    date: Optional[str] = None
    speaker: Optional[str] = None

class AnalyzeRequest(BaseModel):
    source_type: str = 'transcript'  # 'transcript', 'email', 'call', 'text'
    texts: Optional[List[str]] = None  # Simple: array of strings
    segments: Optional[List[TextSegment]] = None  # Rich: array with metadata
    min_words: Optional[int] = 50  # Minimum total words to analyze


# ── Endpoints ───────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "truewriting", "version": "0.4.0"}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    """Generate a TW-0 voice profile from text content.
    
    Accepts either:
    - texts: ["raw text 1", "raw text 2", ...] (simple)
    - segments: [{ text, source_id, title, date, speaker }, ...] (rich)
    
    Returns: Full CPP JSON profile.
    """
    # Auth check (optional)
    # Could add Bearer token check here if needed

    # Build messages from input
    if req.segments:
        data = [s.dict() for s in req.segments]
        messages = TranscriptIngester.from_json(data)
    elif req.texts:
        messages = TranscriptIngester.from_texts(req.texts)
    else:
        raise HTTPException(status_code=400, detail="Provide 'texts' or 'segments'")

    if not messages:
        raise HTTPException(status_code=400, detail="No usable text content (all segments too short)")

    total_words = sum(m.word_count for m in messages)
    if total_words < req.min_words:
        raise HTTPException(
            status_code=400,
            detail=f"Not enough content: {total_words} words (minimum {req.min_words})"
        )

    # Override source_type on all messages
    for m in messages:
        m.source_type = req.source_type

    # Run analysis
    analyzer = TrueWritingAnalyzer(messages)
    profile = analyzer.analyze()

    return profile


# ── Run ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8200"))
    uvicorn.run(app, host="0.0.0.0", port=port)
