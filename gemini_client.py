import os

# Minimal local Gemini client stub.
# - If USE_GEMINI_MOCK=1, returns a canned response suitable for testing.
# - If GEMINI_API_KEY is not set, returns None so the app falls back to keyword replies.
# - Does not perform any external network calls by default to keep runs local and safe.

def generate_response(prompt: str, temperature: float = 0.2, max_tokens: int = 512) -> str:
    use_mock = os.environ.get('USE_GEMINI_MOCK', '1')
    if use_mock == '1':
        return (
            "Possible causes: viral pharyngitis, common cold. Red flags: high fever, difficulty breathing, or "
            "inability to swallow — seek immediate care. Next steps: rest, fluids, paracetamol/ibuprofen for "
            "fever/pain, and monitor symptoms. Disclaimer: This is not a diagnosis; consult a healthcare "
            "professional if concerned."
        )

    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        # No API key configured; let the application fall back to non-LLM behavior.
        return None

    # Production integration should be implemented here. For safety, we return a placeholder.
    return None
