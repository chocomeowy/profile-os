"""
Shared LLM (Gemini) context helpers for the OS scripts.
"""

import time
import google.generativeai as genai

# Standard list of fallbacks for general tasks
DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
]

def generate_with_fallback(
    api_key: str, 
    prompt: str, 
    system_instruction: str = None, 
    models: list[str] = None,
    generation_config: dict = None
) -> str:
    """
    Attempt generation with a list of fallback models and retry on rate limits.
    """
    genai.configure(api_key=api_key)
    models_to_try = models if models is not None else DEFAULT_MODELS
    
    last_error = None
    for model_name in models_to_try:
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                print(f"      Attempting generation with {model_name} (attempt {attempt + 1}/{max_retries + 1})...")
                
                kwargs = {"model_name": model_name}
                if system_instruction:
                    kwargs["system_instruction"] = system_instruction
                    
                model = genai.GenerativeModel(**kwargs)
                response = model.generate_content(prompt, generation_config=generation_config)
                return response.text
            except Exception as e:
                last_error = e
                err_msg = str(e).lower()
                
                # Check for rate limit or quota exceeded errors (status code 429 or matching keywords)
                is_rate_limit = (
                    "429" in err_msg or 
                    "quota" in err_msg or 
                    "rate limit" in err_msg or 
                    "exhausted" in err_msg
                )
                
                # Check if it is a permanent daily quota exhaustion (e.g. limit: 0)
                is_permanent_daily = "limit: 0" in err_msg or "daily" in err_msg
                
                if is_rate_limit and not is_permanent_daily and attempt < max_retries:
                    # Exponential backoff sleep: 5s on first retry, 15s on second retry
                    sleep_time = 5 * (3 ** attempt)
                    print(f"      [Rate Limit] {model_name} rate limited. Retrying in {sleep_time}s... Error: {e}")
                    time.sleep(sleep_time)
                else:
                    print(f"      {model_name} failed: {e}")
                    break  # Break retry loop to try the next model
            
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


