"""
Live demonstration of Tier 3 LLM Entity Disambiguation.
Resolves genuinely ambiguous entities in the 70-90 fuzzy match band
using MultiTierLLMEngine (Gemini -> Groq -> Third-Party Gateway -> Deterministic).
"""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.resolution.normalizer import EntityResolver
from src.schemas.entities import MatchMethodEnum


async def main():
    print("=" * 80)
    print("🎯 FrontierAtlas Tier 3: LLM Entity Disambiguation Live Demonstration")
    print("=" * 80)

    # Initialize resolver with LLM disambiguation enabled
    resolver = EntityResolver(enable_llm_disambiguation=True)

    test_cases = [
        {
            "raw_name": "MistralHQ",
            "source_url": "https://techcrunch.com/article/mistral-update",
            "entity_type": "STARTUP",
            "context": "Corporate Twitter / organization handle for Mistral AI",
        },
        {
            "raw_name": "Eleven Labs Voice",
            "source_url": "https://theverge.com/article/elevenlabs-feature",
            "entity_type": "PRODUCT",
            "context": "Product suite brand for ElevenLabs voice AI",
        },
        {
            "raw_name": "ChromaDB",
            "source_url": "https://github.com/chroma-core/chroma",
            "entity_type": "PRODUCT",
            "context": "Popular developer alias / library name for Chroma",
        },
        {
            "raw_name": "Midjourney Art",
            "source_url": "https://theverge.com/article/midjourney-v6",
            "entity_type": "PRODUCT",
            "context": "Community / media reference to Midjourney image generator",
        },
        {
            "raw_name": "Cohere Health Care",
            "source_url": "https://venturebeat.com/article/cohere-health",
            "entity_type": "STARTUP",
            "context": "Distinct clinical healthcare company; shares token with Cohere AI (expected: NEW_ENTITY)",
        },
    ]

    for tc in test_cases:
        print(f"\nEvaluating: '{tc['raw_name']}'")
        print(f"  Context      : {tc['context']}")
        print(f"  Source URL   : {tc['source_url']}")
        
        canonical, log = await resolver.resolve_async(
            raw_name=tc["raw_name"],
            source_url=tc["source_url"],
            entity_type=tc["entity_type"],
        )

        method_str = log.matchMethod.value if hasattr(log.matchMethod, "value") else str(log.matchMethod)
        print(f"  Canonical    : {canonical}")
        print(f"  Match Method : {method_str}")
        print(f"  Confidence   : {log.confidenceScore:.2f}")

    print("\n" + "=" * 80)
    print("📋 Final Audit Log Rows (EntityResolutionLog)")
    print("=" * 80)
    for entry in resolver.audit_log:
        method_str = entry.matchMethod.value if hasattr(entry.matchMethod, "value") else str(entry.matchMethod)
        row_dict = {
            "rawName": entry.rawName,
            "canonicalName": entry.canonicalName,
            "matchMethod": method_str,
            "confidenceScore": entry.confidenceScore,
            "sourceUrl": entry.sourceUrl,
            "entityType": entry.entityType,
        }
        print(json.dumps(row_dict, indent=2))

    llm_hits = [
        e for e in resolver.audit_log
        if (e.matchMethod.value if hasattr(e.matchMethod, "value") else str(e.matchMethod)) == MatchMethodEnum.LLM_DISAMBIGUATION.value
    ]
    print("\n" + "-" * 80)
    print(f"Summary: {len(llm_hits)}/{len(test_cases)} cases successfully resolved via LLM_DISAMBIGUATION.")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
