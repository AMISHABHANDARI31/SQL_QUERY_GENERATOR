import os

import requests
from dotenv import load_dotenv

load_dotenv()


class OllamaService:
    def __init__(self):
        self.model = os.getenv("LOCAL_LLM_MODEL", "llama3:latest")
        self.endpoint = os.getenv("LOCAL_LLM_ENDPOINT", "http://127.0.0.1:11434/api/generate")
        self.timeout_seconds = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))
        self.base_url = self.endpoint.split("/api/")[0]
        self.tags_endpoint = f"{self.base_url}/api/tags"

    def health(self) -> dict:
        try:
            response = requests.get(self.tags_endpoint, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.ConnectionError:
            return {
                "ok": False,
                "error": "Unable to connect to Ollama. Make sure Ollama is running at http://127.0.0.1:11434.",
            }
        except requests.exceptions.Timeout:
            return {"ok": False, "error": "Ollama health check timed out. Make sure Ollama is running and responsive."}
        except requests.exceptions.RequestException as exc:
            return {"ok": False, "error": f"Ollama health check failed: {str(exc)}"}
        except ValueError:
            return {"ok": False, "error": "Ollama returned an invalid health-check response."}

        models = [model.get("name", "") for model in data.get("models", [])]
        if self.model not in models:
            return {
                "ok": False,
                "error": f"Ollama model '{self.model}' is unavailable. Install it with: ollama pull {self.model}",
                "available_models": models,
            }
        return {
            "ok": True,
            "model": self.model,
            "endpoint": self.endpoint,
            "available_models": models,
        }

    def generate(self, prompt: str) -> str:
        try:
            response = requests.post(
                self.endpoint,
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=self.timeout_seconds,
            )
            if response.status_code == 404:
                raise RuntimeError(f"Ollama model '{self.model}' is unavailable. Install it with: ollama pull {self.model}")
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError("Unable to connect to Ollama. Make sure Ollama is running at http://127.0.0.1:11434.") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("Ollama request timed out while generating SQL.") from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Ollama request failed: {str(exc)}") from exc
        except ValueError as exc:
            raise RuntimeError("Ollama returned invalid JSON.") from exc

        generated = data.get("response")
        if not isinstance(generated, str) or not generated.strip():
            raise RuntimeError("Ollama returned an invalid response without generated SQL text.")
        return generated
