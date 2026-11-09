"""Auto-listing: give it a product name (or a link) and Claude searches the web and
drafts the listing — clean name, short description, category, retail price and
candidate product photos. Staff review the draft before anything is saved.

Needs the `anthropic` package (pip install anthropic) and an Anthropic API key,
either in the ANTHROPIC_API_KEY environment variable or saved on the staff page.
"""

MODEL = "claude-opus-5-5"
MAX_ROUNDS = 4  # web search can pause a long turn (pause_turn); resume this many times at most

SAVE_TOOL = {
    "name": "save_listing",
    "description": "Save the finished store listing. Call this exactly once, after researching the product.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {
                "type": "boolean",
                "description": "False if you could not identify the specific product.",
            },
            "name": {
                "type": "string",
                "description": "Short, clean product name as a shopper would say it, including variant "
                "(flavor/color/size) when one was given. Max ~60 characters. No marketing fluff.",
            },
            "description": {
                "type": "string",
                "description": "One or two short, friendly sentences for students: what it is and why it's fun "
                "or useful. Max ~200 characters. No prices, no emoji.",
            },
            "category": {
                "type": "string",
                "description": "Best category. Reuse one of the store's existing categories when it fits.",
            },
            "retail_price": {
                "type": ["number", "null"],
                "description": "Typical US retail price in dollars from the official or a major store, or null.",
            },
            "image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-5 DIRECT image file URLs (jpg/png/webp) of this exact product, best first. "
                "Clean product shot on a white/plain background, no people, no collage. Prefer the brand's "
                "own site/CDN, then major retailers. Only URLs you actually saw in a fetched page or search "
                "result (e.g. the page's og:image) — never guess or construct one.",
            },
            "source_url": {"type": "string", "description": "The product page you relied on most."},
            "notes": {
                "type": "string",
                "description": "Anything staff should double-check (variant uncertainty, etc.). Empty if none.",
            },
        },
        "required": ["found", "name", "description", "category", "retail_price", "image_urls", "source_url", "notes"],
        "additionalProperties": False,
    },
}

SYSTEM = (
    "You create product listings for a student-run school store. Given a product name or a link, use web search "
    "and web fetch to identify the exact product (brand, model, and the variant asked for), then call save_listing "
    "with the result. Work quickly: a few searches and fetches are enough. For photos, open the product page and "
    "take image URLs that appear in it (og:image, product gallery). If the request is too vague to pin down one "
    "product, pick the most common matching product and say so in notes."
)


class ListingLookupError(Exception):
    pass


def lookup_product(query, api_key, categories=()):
    try:
        import anthropic
    except ImportError:
        raise ListingLookupError("Auto-listing needs the 'anthropic' package. On the server run: pip install anthropic")

    client = anthropic.Anthropic(api_key=api_key, timeout=240.0, max_retries=2)
    cats = ", ".join(sorted(set(categories))) or "(none yet)"
    messages = [{
        "role": "user",
        "content": f"Product: {query}\n\nThe store's existing categories: {cats}",
    }]
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
        SAVE_TOOL,
    ]

    try:
        for _ in range(MAX_ROUNDS):
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                tools=tools,
                messages=messages,
                output_config={"effort": "low"},
                # If Opus declines on safety grounds, the API re-runs the request on Anthropic's
                # recommended fallback model instead of returning a refusal.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                raise ListingLookupError("Claude declined to look up that product.")

            for block in response.content:
                if block.type == "tool_use" and block.name == "save_listing":
                    return _clean(block.input)

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue  # server-side search loop paused; re-send and it resumes where it left off
            # Finished without saving: ask once more, explicitly.
            messages.append({"role": "user", "content": "Please call save_listing now with what you found."})
        raise ListingLookupError("The lookup took too long. Try a more specific product name.")
    except anthropic.AuthenticationError:
        raise ListingLookupError("The AI key was rejected. Check it on the staff page (Settings).")
    except anthropic.PermissionDeniedError:
        raise ListingLookupError("That AI key isn't allowed to do this (permission denied).")
    except anthropic.RateLimitError:
        raise ListingLookupError("Too many lookups at once. Wait a minute and try again.")
    except anthropic.BadRequestError as e:
        raise ListingLookupError(f"The AI service rejected the request: {e.message}")
    except anthropic.APIStatusError as e:
        raise ListingLookupError(f"The AI service had a problem ({e.status_code}). Try again shortly.")
    except anthropic.APIConnectionError:
        raise ListingLookupError("Couldn't reach the AI service. Check the server's internet connection.")


def _clean(data):
    urls = [u for u in (data.get("image_urls") or []) if isinstance(u, str) and u.startswith(("https://", "http://"))]
    price = data.get("retail_price")
    return {
        "found": bool(data.get("found")),
        "name": str(data.get("name") or "")[:120].strip(),
        "description": str(data.get("description") or "")[:500].strip(),
        "category": str(data.get("category") or "Other")[:60].strip() or "Other",
        "retail_price": round(float(price), 2) if isinstance(price, (int, float)) and price > 0 else None,
        "image_urls": list(dict.fromkeys(urls))[:5],
        "source_url": str(data.get("source_url") or "")[:1000],
        "notes": str(data.get("notes") or "")[:500].strip(),
    }
