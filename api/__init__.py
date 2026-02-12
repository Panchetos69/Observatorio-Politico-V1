from google import genai

class LegislativeAgent:
    def __init__(self, store, gemini_api_key):
        self.store = store
        self.client = genai.Client(api_key=gemini_api_key)