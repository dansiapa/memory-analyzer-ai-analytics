"""
ai_analyzer.py
AI-powered deep-dive analysis menggunakan Google GenAI SDK terbaru (google-genai).
Menggunakan model gemini-2.5-flash untuk menghindari issue 404 pada endpoint v1beta.
Sudah dilengkapi dengan HTTPX Monkeypatch untuk menembus corporate proxy/self-signed cert.
"""

from __future__ import annotations

import os
import ssl
from google import genai
from google.genai import types
from parser import Snapshot
from diagnosis import Diagnosis

# =====================================================================
# 🛠️ GLOBAL SSL BYPASS UNTUK HTTPX (Corporate Proxy & Self-Signed Cert)
# =====================================================================
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

try:
    import httpx
    from functools import wraps
    
    # Patch HTTPX Client standar
    _orig_init = httpx.Client.__init__
    @wraps(_orig_init)
    def _patched_init(self, *args, **kwargs):
        kwargs['verify'] = False
        _orig_init(self, *args, **kwargs)
    httpx.Client.__init__ = _patched_init
    
    # Patch HTTPX AsyncClient
    _orig_async_init = httpx.AsyncClient.__init__
    @wraps(_orig_async_init)
    def _patched_async_init(self, *args, **kwargs):
        kwargs['verify'] = False
        _orig_async_init(self, *args, **kwargs)
    httpx.AsyncClient.__init__ = _patched_async_init
except Exception:
    pass
# =====================================================================

LANGUAGE_NAMES = {
    "en": "English",
    "id": "Indonesian"
}

SYSTEM_PROMPT_TEMPLATE = """
You are an expert Java Performance Engineer and System Architect.
Analyze the following thread dump summary and rule-based diagnosis.
Provide a clear deep-dive analysis in {language}.
Include:
1. Probable Root Cause
2. User or Business Impact
3. Concrete Next Steps / Recommendations
"""

def build_summary(snapshot: Snapshot, diagnosis: Diagnosis) -> str:
    """Membuat ringkasan ringkas dari objek thread dump snapshot."""
    summary_lines = []
    summary_lines.append(f"Verdict: {diagnosis.verdict}")
    summary_lines.append(f"Total Threads: {len(snapshot.threads)}")
    
    states = {}
    for t in snapshot.threads:
        states[t.state] = states.get(t.state, 0) + 1
    summary_lines.append(f"Thread States Summary: {states}")
    
    if hasattr(diagnosis, 'findings') and diagnosis.findings:
        summary_lines.append("\nKey Findings Detected:")
        for f in diagnosis.findings:
            summary_lines.append(f"- [{f.severity}] {f.title}: {f.detail}")
            
    return "\n".join(summary_lines)

def get_ai_analysis(snapshot: Snapshot, diagnosis: Diagnosis, api_key: str | None = None, language: str = "en") -> str:
    """Mengambil analisis deep-dive menggunakan Gemini API Client terbaru."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "Gemini API Key tidak ditemukan.\n\n"
            "Silakan tambahkan melalui menu Settings > Gemini API Key... pada aplikasi."
        )

    try:
        # Inisialisasi client resmi google-genai
        client = genai.Client(api_key=key)
    except Exception as e:
        raise RuntimeError(f"Gagal menginisialisasi Gemini Client: {e}")

    lang_name = LANGUAGE_NAMES.get(language, LANGUAGE_NAMES["en"])
    summary = build_summary(snapshot, diagnosis)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(language=lang_name)

    try:
        # Menggunakan 'gemini-2.5-flash' yang didukung penuh oleh endpoint v1beta SDK baru
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Here is the thread dump summary:\n\n{summary}",
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
            )
        )
        
        if response.text:
            return response.text
        return "Model tidak mengembalikan respons teks."
            
    except Exception as e:
        raise RuntimeError(f"Gemini API call gagal: {e}")