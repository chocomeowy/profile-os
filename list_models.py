"""
Model Query Utility - Profile OS
=================================
Queries Google AI Studio using your GEMINI_API_KEY to list all available models
and their supported generation methods.
"""

import os
import sys
import google.generativeai as genai

def main():
    # Attempt to retrieve key from environment
    api_key = os.environ.get("GEMINI_API_KEY")
    
    if not api_key:
        print("[Error] GEMINI_API_KEY environment variable is not set.")
        print("Usage:")
        print("  export GEMINI_API_KEY=\"your_key_here\"")
        print("  python3 list_models.py")
        sys.exit(1)
        
    print(f"Connecting to Google AI Studio with API Key (ending in ...{api_key[-4:] if len(api_key) > 4 else ''})")
    
    try:
        genai.configure(api_key=api_key)
        
        print("\n=== Available Gemini Models ===\n")
        count = 0
        for model in genai.list_models():
            # Filter to show mostly text/content generation models for clarity
            if "generateContent" in model.supported_generation_methods:
                count += 1
                print(f"Model Name:  {model.name}")
                print(f"DisplayName: {model.display_name}")
                print(f"Description: {model.description}")
                print(f"Limits:      Input: {model.input_token_limit} tokens | Output: {model.output_token_limit} tokens")
                print("-" * 60)
                
        print(f"\nFound {count} models capable of Content Generation.")
        
    except Exception as e:
        print(f"\n[Error] Failed to communicate with ModelService: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
