from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import VideoResult


class VeoProvider:
    """Google Veo REST adapter with polling and optional first-frame reference image."""

    def __init__(self, *, project: str, location: str, model: str, output_gcs_uri: str | None, credentials_file: str | None):
        if not project:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required in live mode")
        self.project = project
        self.location = location
        self.model = model
        self.output_gcs_uri = output_gcs_uri
        self.credentials_file = credentials_file

    def _token(self) -> str:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError("Install google-auth to use Veo") from exc
        credentials_path = self.credentials_file or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not credentials_path:
            raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is required")
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())
        return credentials.token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        output_path: Path,
        seed: int,
        reference_image: Path | None = None,
    ) -> VideoResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        endpoint = (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}/locations/"
            f"{self.location}/publishers/google/models/{self.model}:predictLongRunning"
        )
        instance: dict = {"prompt": prompt}
        if reference_image and reference_image.exists():
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(reference_image.read_bytes()).decode("ascii"),
                "mimeType": "image/png" if reference_image.suffix.lower() == ".png" else "image/jpeg",
            }
        generation_duration = 4 if duration_seconds <= 4 else 6 if duration_seconds <= 6 else 8
        parameters: dict = {
            "aspectRatio": "16:9",
            "durationSeconds": generation_duration,
            "sampleCount": 1,
            "seed": seed,
            "generateAudio": False,
            "resolution": "720p",
            "resizeMode": "pad",
            "negativePrompt": "flashing lights, rapid cuts, frightening face, extra limbs, text, logos, clutter",
            "personGeneration": "disallow",
        }
        if self.output_gcs_uri:
            parameters["storageUri"] = self.output_gcs_uri.rstrip("/") + "/"
        response = httpx.post(endpoint, headers=self._headers(), json={"instances": [instance], "parameters": parameters}, timeout=90)
        response.raise_for_status()
        operation = response.json()
        operation_name = operation.get("name")
        if not operation_name:
            raise RuntimeError(f"Veo did not return an operation name: {operation}")
        poll_url = (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}/locations/"
            f"{self.location}/publishers/google/models/{self.model}:fetchPredictOperation"
        )
        result: dict | None = None
        for _ in range(120):
            poll = httpx.post(
                poll_url, headers=self._headers(), json={"operationName": operation_name}, timeout=60
            )
            poll.raise_for_status()
            data = poll.json()
            if data.get("done"):
                if data.get("error"):
                    raise RuntimeError(f"Veo generation failed: {data['error']}")
                result = data.get("response", data)
                break
            time.sleep(5)
        if result is None:
            raise TimeoutError("Veo generation did not complete within the polling window")
        generated = result.get("generatedSamples") or result.get("videos") or result.get("predictions") or []
        if not generated:
            raise RuntimeError(f"Unexpected Veo response: {result}")
        item = generated[0]
        video_bytes = item.get("bytesBase64Encoded") or item.get("video", {}).get("bytesBase64Encoded")
        uri = item.get("gcsUri") or item.get("uri") or item.get("video", {}).get("gcsUri")
        if video_bytes:
            output_path.write_bytes(base64.b64decode(video_bytes))
        elif uri and uri.startswith("gs://"):
            try:
                from google.cloud import storage
            except ImportError as exc:
                raise RuntimeError("Install google-cloud-storage to download Veo output") from exc
            parsed = urlparse(uri)
            client = storage.Client(project=self.project)
            client.bucket(parsed.netloc).blob(parsed.path.lstrip("/")).download_to_filename(output_path)
        elif uri:
            download = httpx.get(uri, timeout=300)
            download.raise_for_status()
            output_path.write_bytes(download.content)
        else:
            raise RuntimeError(f"Veo output contains no downloadable video: {item}")
        estimated_cost = generation_duration * 0.03
        return VideoResult(
            path=output_path,
            provider="google-veo",
            duration_seconds=float(generation_duration),
            cost_usd=round(estimated_cost, 4),
            metadata={"model": self.model, "operation": operation_name, "reference_used": bool(reference_image)},
        )
