from __future__ import annotations

import os
import sys
from typing import Any

import requests
from dotenv import load_dotenv


def model_supports_generate_content(model: dict[str, Any]) -> bool:
    methods = model.get("supportedGenerationMethods") or []
    return any(method == "generateContent" for method in methods)


def main() -> int:
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not set in .env", file=sys.stderr)
        return 1

    response = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": api_key},
        timeout=30,
    )
    if not response.ok:
        print(
            f"ERROR: Gemini ListModels failed with {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        return 1

    data = response.json()
    models = data.get("models", [])
    usable = [model for model in models if isinstance(model, dict) and model_supports_generate_content(model)]

    if not usable:
        print("No generateContent-capable Gemini models were returned for this API key.")
        return 0

    print("Gemini models available for generateContent:")
    for model in usable:
        name = model.get("name", "<unknown>")
        display_name = model.get("displayName", "")
        description = model.get("description", "")
        if display_name:
            print(f"- {name} ({display_name})")
        else:
            print(f"- {name}")
        if description:
            print(f"  {description}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
