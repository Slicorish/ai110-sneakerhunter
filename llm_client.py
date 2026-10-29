import os


class GeminiClient:
    """
    Minimal Gemini API wrapper.

    Requirements:
    - google-genai installed
    - GEMINI_API_KEY set in environment (or loaded via python-dotenv)

    Most prompts the agent sends ask for JSON, so responses default to JSON mode
    to cut down on unparseable output. Pass json_mode=False for plain text.
    """

    def __init__(self, model_name: str = "gemini-flash-lite-latest", temperature: float = 0.2):
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "Missing GEMINI_API_KEY. Create a .env file and set GEMINI_API_KEY=..."
            )

        # Import here so the app can start without the dependency in feeds-only mode.
        from google import genai
        from google.genai import types

        self.client = genai.Client(api_key=api_key)
        self._types = types
        self.model_name = model_name
        self.temperature = float(temperature)

    def complete(self, system_prompt: str, user_prompt: str, json_mode: bool = True) -> str:
        """
        Sends a single request to Gemini and returns the response text.

        API errors (rate limits, network, blocked content) are raised, not hidden,
        so the agent can log them and tell them apart from an empty response.
        """
        config = self._types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=self.temperature,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=user_prompt,
            config=config,
        )

        # response.text is None when the model returns no text (e.g. a safety block).
        return response.text or ""
