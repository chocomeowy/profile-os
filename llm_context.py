"""
Shared LLM (Gemini) context helpers for the OS scripts.
"""

import google.generativeai as genai

# Standard list of fallbacks for general tasks
DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-pro"
]

def generate_with_fallback(
    api_key: str, 
    prompt: str, 
    system_instruction: str = None, 
    models: list[str] = None,
    generation_config: dict = None
) -> str:
    """
    Attempt generation with a list of fallback models.
    """
    genai.configure(api_key=api_key)
    models_to_try = models if models is not None else DEFAULT_MODELS
    
    last_error = None
    for model_name in models_to_try:
        try:
            print(f"      Attempting generation with {model_name}...")
            
            kwargs = {"model_name": model_name}
            if system_instruction:
                kwargs["system_instruction"] = system_instruction
                
            model = genai.GenerativeModel(**kwargs)
            response = model.generate_content(prompt, generation_config=generation_config)
            return response.text
        except Exception as e:
            print(f"      {model_name} failed: {e}")
            last_error = e
            continue
            
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")

