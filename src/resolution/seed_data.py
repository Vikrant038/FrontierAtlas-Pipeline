"""
Canonical Seed Data and Dictionaries for 3-Tier Entity Resolution.
Sourced strictly from PROJECT_CONTEXT.md Phase IV.
"""

from typing import Dict, List, Set

CANONICAL_AI_ENTITIES: List[str] = [
    "OpenAI", "Anthropic", "Google DeepMind", "Meta AI", "Microsoft AI", "Amazon AI",
    "Mistral AI", "Cohere", "Hugging Face", "Stability AI", "Midjourney", "ElevenLabs",
    "Scale AI", "Databricks", "Snowflake", "Pinecone", "Weaviate", "Chroma", "Qdrant",
    "LangChain", "LlamaIndex", "Together AI", "Groq", "Perplexity AI", "Character AI",
    "xAI", "DeepSeek", "Inflection AI", "Runway", "Pika Labs", "Suno", "Udio",
    "Notion AI", "Jasper AI", "Copy AI", "Writer AI", "Harvey AI", "Clio", "Ironclad",
    "Adept AI", "Imbue", "Weights & Biases", "Replicate", "Modal", "Banana Dev",
    "Cerebras", "SambaNova", "Tenstorrent", "Graphcore", "Mythic AI",
    "Alibaba AI", "Moonshot AI", "Zhipu AI", "MiniMax AI", "LMSYS", "GitHub",
    "Salesforce AI", "NVIDIA", "Apple AI", "Tencent AI", "Baidu AI"
]

CORPORATE_SUFFIXES: Set[str] = {
    "inc", "corp", "corporation", "llc", "ltd", "limited", "incorporated",
    "plc", "gmbh", "sas", "bv", "pbc", "co", "company", "global llc"
}

TECH_NOISE_WORDS: Set[str] = {
    "ai", "artificial intelligence", "technologies", "technology",
    "tech", "labs", "laboratory", "laboratories", "systems", "solutions"
}

KNOWN_ALIASES: Dict[str, str] = {
    "openai inc": "OpenAI",
    "openai global llc": "OpenAI",
    "openai llc": "OpenAI",
    "open ai": "OpenAI",
    "mistral": "Mistral AI",
    "mistralai": "Mistral AI",
    "mistral ai sas": "Mistral AI",
    "anthropic pbc": "Anthropic",
    "anthropic ai": "Anthropic",
    "deepmind": "Google DeepMind",
    "google deepmind technologies": "Google DeepMind",
    "stability": "Stability AI",
    "stability ai ltd": "Stability AI",
    "huggingface": "Hugging Face",
    "hugging face inc": "Hugging Face",
    "eleven labs": "ElevenLabs",
    "together": "Together AI",
    "together compute": "Together AI",
    "perplexity": "Perplexity AI",
    "character ai": "Character AI",
    "character.ai": "Character AI",
    "pika": "Pika Labs",
    "wandb": "Weights & Biases",
    "weights and biases": "Weights & Biases",
    "facebook": "Meta AI",
    "meta": "Meta AI",
    "grok": "xAI",
    "qwen": "Alibaba AI",
    "minimax": "MiniMax AI",
    "kimi": "Moonshot AI",
    "kimi k2": "Moonshot AI",
    "glm": "Zhipu AI",
    "chatglm": "Zhipu AI",
    "vicuna": "LMSYS",
    "vicuna 13b": "LMSYS",
    "vicuna-13b": "LMSYS",
    "llama": "Meta AI",
    "llama 2": "Meta AI",
    "llama 3": "Meta AI",
    "bing search": "Microsoft AI",
    "github copilot": "GitHub",
    "claude": "Anthropic",
    "chatgpt": "OpenAI",
    "gpt 4": "OpenAI",
    "gemini": "Google DeepMind",
    "bard": "Google DeepMind",
}
