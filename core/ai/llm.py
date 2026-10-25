import os
import re
import math
import requests
import json
from typing import Dict, Any, Optional

# Small model per the plan's <4 GB RAM target. Override with EMAIL_AI_MODEL to compare models.
DEFAULT_MODEL = os.environ.get("EMAIL_AI_MODEL", "qwen3:4b")
# ponytail: Ollama's runner caches every distinct prompt in RAM (+70-170 MB each, up to 8 GB), which
# broke the <4 GB target inside long jobs. Reloading every N requests empties it for ~1-2 s each.
# Cleaner, but system-wide: `launchctl setenv LLAMA_ARG_CACHE_RAM 0` and restart Ollama, then drop this.
RELOAD_EVERY = 10
KEEP_ALIVE = "1m"
_requests_since_load = 0


class LocalLLM:
    def __init__(self, model: str = DEFAULT_MODEL, url: str = "http://localhost:11434"):
        self.model = model
        self.url = url.rstrip("/")
        self.last_logprobs = None  # per-token logprobs of the last answer (see value_probability)

    def unload(self):
        """Free the model's RAM now. Ollama's runner also keeps a prompt cache (up to 8 GB by
        default) that grows ~70 MB per distinct prompt; unloading clears it too."""
        global _requests_since_load
        _requests_since_load = 0
        try:
            requests.post(f"{self.url}/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=30)
        except requests.RequestException:
            pass  # Ollama not running: nothing loaded anyway

    def generate_json(self, prompt: str, schema: Dict[str, Any], max_retries: int = 1,
                      json_schema: Optional[Dict] = None) -> Optional[Dict]:
        """json_schema: a real JSON Schema Ollama enforces while generating (e.g. an enum of
        allowed values). Without it the output is only forced to be some JSON object."""
        if json_schema:
            full_prompt = prompt  # the schema is enforced; the hint text only got copied into answers
        else:
            full_prompt = f"{prompt}\n\nReturn ONLY valid JSON matching this structure:\n{json.dumps(schema)}"

        global _requests_since_load
        if _requests_since_load >= RELOAD_EVERY:
            self.unload()  # also resets the counter
        _requests_since_load += 1

        output = ""
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(
                    f"{self.url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": full_prompt,
                        "format": json_schema or "json",
                        "stream": False,
                        # qwen3 otherwise "thinks" ~4-5k tokens per call (~1 min each)
                        "think": False,
                        # token probabilities: a real confidence instead of a number the model makes up
                        "logprobs": True,
                        # Prompts stay under ~1.5k tokens; the 32k default doubles RAM (7.6 GB vs 3.2 GB)
                        # temperature 0: the same email always gets the same answer
                        "options": {"num_ctx": 4096, "temperature": 0},
                        # Unloaded 1 idle minute after the last request (chat follow-ups don't reload;
                        # background jobs unload right away, see LocalLLM.unload)
                        "keep_alive": KEEP_ALIVE
                    },
                    timeout=120
                )
                resp.raise_for_status()
                body = resp.json()
                output = body.get("response", "")
                self.last_logprobs = body.get("logprobs")

                # Strip markdown code blocks if present
                if output.startswith("```"):
                    output = output.split("```")[1]
                    if output.startswith("json"): output = output[4:]
                output = output.strip()

                data = json.loads(output)
                self._validate_json(data, schema)
                return data
            # RequestException: Ollama occasionally answers 500; a retry usually succeeds
            except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError, TypeError, AssertionError) as e:
                if attempt == max_retries:
                    raise RuntimeError(f"LLM failed to return valid JSON after {max_retries} retries. Output: {output}") from e
        return None

    def _validate_json(self, data: Dict, schema: Dict):
        if not isinstance(data, dict):
            raise TypeError(f"LLM output should be a JSON object, got {type(data).__name__}")
        for key, expected_type in schema.items():
            if key not in data:
                raise AssertionError(f"Missing key '{key}' in LLM output")

            val = data[key]
            if expected_type == "string" and not isinstance(val, str):
                raise TypeError(f"Key '{key}' should be string, got {type(val).__name__}")
            elif expected_type == "float" and not isinstance(val, (int, float)):
                raise TypeError(f"Key '{key}' should be numeric, got {type(val).__name__}")
            elif expected_type == "list" and not isinstance(val, list):
                raise TypeError(f"Key '{key}' should be a list, got {type(val).__name__}")
            elif expected_type == "bool" and not isinstance(val, bool):
                raise TypeError(f"Key '{key}' should be true/false, got {type(val).__name__}")
            elif expected_type == "string|null":
                if val is not None and not isinstance(val, str):
                    raise TypeError(f"Key '{key}' should be string or null, got {type(val).__name__}")


def value_probability(logprobs, key: str) -> Optional[float]:
    """Probability the model gave the first token of a JSON value, e.g. the "Ads" in
    {"section": "Ads"} or the "true" in {"has_event": true}. Section names start with different
    tokens, so this is how sure it was. None when there are no logprobs (older Ollama)."""
    if not logprobs:
        return None
    text = "".join(t["token"] for t in logprobs)
    m = re.search(r'"%s"\s*:\s*"?' % re.escape(key), text)
    if not m:
        return None
    pos = 0
    for t in logprobs:
        pos += len(t["token"])
        if pos > m.end():  # first token reaching into the value
            return math.exp(t["logprob"])
    return None
