"""
LLM Provider Connectivity & Compatibility Diagnostic Tool.
Tests connectivity, response latency, and JSON completion compatibility
across Gemini, Groq, and custom third-party OpenAI-compatible endpoints.

Usage:
    python scripts/test_llm_connection.py [OPTIONS]

Options:
    --provider [gemini|groq|custom|all]  (default: custom)
    --base-url TEXT                     Custom base URL override
    --api-key TEXT                      Custom API key override
    --model TEXT                        Model name override
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings


async def test_gemini(api_key: Optional[str] = None) -> bool:
    """Test Google Gemini connectivity and JSON output."""
    key = api_key or settings.gemini_api_key
    print(f"\n--- Testing Tier 1: Google Gemini ---")
    if not key:
        print("❌ GEMINI_API_KEY is not set.")
        return False

    print(f"API Key: {key[:6]}...{key[-4:] if len(key) > 10 else ''}")
    t0 = time.monotonic()
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)
        chat = client.aio.chats.create(
            model=settings.gemini_model,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        response = await chat.send_message(
            'Return a JSON object with keys "status" ("ok") and "message" ("Gemini online").'
        )
        elapsed = time.monotonic() - t0
        print(f"✅ {settings.gemini_model} responded in {elapsed:.2f}s:")
        print(f"   Raw text: {response.text.strip()}")
        data = json.loads(response.text)
        print(f"   Parsed JSON: {data}")
        return True
    except Exception as exc:
        print(f"❌ Gemini test failed: {exc}")
        return False


async def test_openai_compatible(
    provider_name: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
) -> bool:
    """Test any OpenAI-compatible endpoint (Groq, OpenRouter, DeepSeek, custom gateway)."""
    print(f"\n--- Testing {provider_name} (OpenAI-Compatible) ---")
    print(f"Base URL : {base_url}")
    print(f"Model    : {model}")
    if not api_key:
        print(f"❌ API Key for {provider_name} is not set.")
        return False
    print(f"API Key  : {api_key[:6]}...{api_key[-4:] if len(api_key) > 10 else ''}")

    t0 = time.monotonic()
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        completion = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a test agent. Return ONLY valid JSON with keys 'status' ('ok') and 'provider' (name).",
                },
                {"role": "user", "content": "Respond to test ping."},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=100,
        )
        elapsed = time.monotonic() - t0
        content = completion.choices[0].message.content or "{}"
        print(f"✅ {provider_name} responded in {elapsed:.2f}s:")
        print(f"   Raw output: {content.strip()}")
        data = json.loads(content)
        print(f"   Parsed JSON: {data}")
        if hasattr(completion, "usage") and completion.usage:
            print(f"   Token Usage: prompt={completion.usage.prompt_tokens}, completion={completion.usage.completion_tokens}")
        return True
    except Exception as exc:
        print(f"❌ {provider_name} test failed: {exc}")
        return False


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Test LLM endpoints connectivity.")
    parser.add_argument("--provider", choices=["gemini", "groq", "custom", "all"], default="custom")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)

    args = parser.parse_args()

    results = {}

    if args.provider in ("gemini", "all"):
        results["gemini"] = await test_gemini(api_key=args.api_key)

    if args.provider in ("groq", "all"):
        groq_url = args.base_url or settings.groq_base_url
        groq_key = args.api_key or settings.groq_api_key
        groq_model = args.model or settings.groq_model
        results["groq"] = await test_openai_compatible("Groq / Secondary", groq_url, groq_key, groq_model)

    if args.provider in ("custom", "all"):
        custom_url = args.base_url or settings.effective_tier3_base_url
        custom_key = args.api_key or settings.effective_tier3_api_key
        custom_model = args.model or settings.effective_tier3_model
        results["custom"] = await test_openai_compatible("Custom Third-Party Endpoint", custom_url, custom_key, custom_model)

    print("\n" + "=" * 50)
    print("LLM Connectivity Diagnostic Summary:")
    for prov, ok in results.items():
        print(f"  {prov.upper():<10}: {'✅ COMPATIBLE' if ok else '❌ FAILED'}")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
