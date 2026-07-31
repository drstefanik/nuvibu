from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from ..media import is_valid_video
from .base import VideoResult

VeoBackend = Literal["gemini", "vertex"]

GEMINI_DEFAULT_MODEL = "veo-3.1-fast-generate-preview"
VERTEX_DEFAULT_MODEL = "veo-3.1-generate-001"


class VeoTerminalError(RuntimeError):
    """The provider completed an operation without a billable video output."""


def veo_price_per_second(backend: VeoBackend, model: str) -> float:
    """Return the current video-generation price used by Nuvibù's budget guard."""

    if backend == "gemini":
        return 0.10 if "fast" in model.lower() else 0.40
    if backend == "vertex":
        return 0.08 if "fast" in model.lower() else 0.20
    raise ValueError(f"Unsupported Veo backend: {backend}")


class VeoProvider:
    """Google Veo adapter for Gemini Developer API and Vertex AI."""

    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str | None,
        output_gcs_uri: str | None,
        credentials_file: str | None,
        backend: VeoBackend = "vertex",
        gemini_api_key: str | None = None,
    ):
        if backend not in {"gemini", "vertex"}:
            raise ValueError("VEO_BACKEND must be either 'gemini' or 'vertex'")
        if backend == "vertex" and not project:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required for the Vertex Veo backend")

        self.backend = backend
        self.project = project
        self.location = location
        self.model = model or (
            GEMINI_DEFAULT_MODEL if backend == "gemini" else VERTEX_DEFAULT_MODEL
        )
        self.output_gcs_uri = output_gcs_uri
        self.credentials_file = credentials_file
        self.gemini_api_key = gemini_api_key or os.getenv("GEMINI_API_KEY")
        self._credentials = None

    def _token(self) -> str:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as exc:
            raise RuntimeError("Install google-auth to use the Vertex Veo backend") from exc

        if self._credentials is None:
            scopes = ["https://www.googleapis.com/auth/cloud-platform"]
            credentials_path = self.credentials_file or os.getenv(
                "GOOGLE_APPLICATION_CREDENTIALS"
            )
            if credentials_path:
                self._credentials, _ = google.auth.load_credentials_from_file(
                    credentials_path,
                    scopes=scopes,
                )
            else:
                # Cloud Run supplies short-lived credentials through its service identity.
                self._credentials, _ = google.auth.default(scopes=scopes)

        if not self._credentials.valid or not self._credentials.token:
            self._credentials.refresh(Request())
        return str(self._credentials.token)

    def _headers(self) -> dict[str, str]:
        if self.backend == "gemini":
            if not self.gemini_api_key:
                raise ValueError("GEMINI_API_KEY is required for the Gemini Veo backend")
            return {
                "x-goog-api-key": self.gemini_api_key,
                "Content-Type": "application/json",
            }
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def operation_sidecar_path(output_path: Path) -> Path:
        return output_path.with_name(f"{output_path.name}.operation.json")

    def _load_operation(self, output_path: Path, request_fingerprint: str) -> str | None:
        sidecar = self.operation_sidecar_path(output_path)
        if not sidecar.exists():
            return None
        try:
            state = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot safely resume Veo operation from {sidecar}"
            ) from exc
        if state.get("backend") != self.backend or state.get("model") != self.model:
            raise RuntimeError(
                "Veo operation sidecar belongs to a different backend or model; "
                f"refusing to start a duplicate paid generation: {sidecar}"
            )
        if state.get("state") == "terminal_error":
            # Google documents that failed Veo generations are not charged.
            # Preserve the failure as an audit artifact, then permit a fresh
            # request.  This check deliberately precedes the fingerprint guard:
            # a policy-rejected prompt may be retried with a safer prompt while
            # an accepted/running operation must still match exactly.
            operation = str(state.get("operation_name", "unknown"))
            operation_hash = hashlib.sha256(operation.encode("utf-8")).hexdigest()[:12]
            archive = output_path.with_name(
                f"{output_path.name}.failed.{operation_hash}.json"
            )
            if archive.exists():
                sidecar.unlink()
            else:
                os.replace(sidecar, archive)
            return None
        if state.get("request_fingerprint") != request_fingerprint:
            raise RuntimeError(
                "Veo operation sidecar belongs to a different scene request; "
                f"refusing to reuse or replace it automatically: {sidecar}"
            )
        operation_name = state.get("operation_name") or state.get("operation")
        if not isinstance(operation_name, str) or not operation_name:
            raise RuntimeError(
                "A previous Veo submission has an ambiguous outcome and cannot "
                f"be retried safely without provider reconciliation: {sidecar}"
            )
        return operation_name

    def _save_submission_marker(
        self,
        output_path: Path,
        generation_duration: int,
        request_fingerprint: str,
    ) -> None:
        self.operation_sidecar_path(output_path).write_text(
            json.dumps(
                {
                    "backend": self.backend,
                    "model": self.model,
                    "state": "submitting",
                    "duration_seconds": generation_duration,
                    "request_fingerprint": request_fingerprint,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _save_operation(
        self,
        output_path: Path,
        operation_name: str,
        generation_duration: int,
        request_fingerprint: str,
    ) -> None:
        sidecar = self.operation_sidecar_path(output_path)
        state = {
            "backend": self.backend,
            "model": self.model,
            "state": "running",
            "operation_name": operation_name,
            "duration_seconds": generation_duration,
            "request_fingerprint": request_fingerprint,
        }
        # Closing the file commits the object on Cloud Storage FUSE as well as on a
        # local filesystem; avoid depending on unsupported POSIX locking semantics.
        sidecar.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _request_fingerprint(
        *,
        prompt: str,
        generation_duration: int,
        seed: int,
        reference_images: list[Path] | None = None,
        reference_image: Path | None = None,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(prompt.encode("utf-8"))
        digest.update(f"\0{generation_duration}\0{seed}\0".encode("ascii"))
        references = list(reference_images or [])
        if (
            reference_image
            and reference_image.exists()
            and reference_image not in references
        ):
            references.insert(0, reference_image)
        for reference_image in references:
            digest.update(b"\0reference\0")
            digest.update(reference_image.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _mime_type(path: Path) -> str:
        return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"

    def _start_endpoint(self) -> str:
        if self.backend == "gemini":
            return (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:predictLongRunning"
            )
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}/"
            f"locations/{self.location}/publishers/google/models/"
            f"{self.model}:predictLongRunning"
        )

    def _poll_url(self, operation_name: str) -> str:
        if self.backend == "gemini":
            return (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"{operation_name.lstrip('/')}"
            )
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}/"
            f"locations/{self.location}/publishers/google/models/"
            f"{self.model}:fetchPredictOperation"
        )

    def _request_payload(
        self,
        *,
        prompt: str,
        generation_duration: int,
        seed: int,
        reference_images: list[Path],
    ) -> dict[str, Any]:
        instance: dict[str, Any] = {"prompt": prompt}
        if reference_images:
            if self.backend == "gemini":
                instance["referenceImages"] = [
                    {
                        "image": {
                            "inlineData": {
                                "mimeType": self._mime_type(reference_image),
                                "data": base64.b64encode(
                                    reference_image.read_bytes()
                                ).decode("ascii"),
                            }
                        },
                        "referenceType": "asset",
                    }
                    for reference_image in reference_images
                ]
            else:
                instance["referenceImages"] = [
                    {
                        "image": {
                            "bytesBase64Encoded": base64.b64encode(
                                reference_image.read_bytes()
                            ).decode("ascii"),
                            "mimeType": self._mime_type(reference_image),
                        },
                        "referenceType": "asset",
                    }
                    for reference_image in reference_images
                ]

        parameters: dict[str, Any] = {
            "aspectRatio": "16:9",
            "durationSeconds": generation_duration,
            "seed": seed,
            "resolution": "720p",
            "negativePrompt": (
                "flashing lights, rapid cuts, frightening face, extra limbs, "
                "text, logos, clutter"
            ),
        }
        if self.backend == "vertex":
            parameters.update(
                {
                    "sampleCount": 1,
                    "generateAudio": False,
                    "personGeneration": "disallow",
                }
            )
            if self.output_gcs_uri:
                parameters["storageUri"] = self.output_gcs_uri.rstrip("/") + "/"

        return {"instances": [instance], "parameters": parameters}

    def _start_operation(
        self,
        *,
        prompt: str,
        generation_duration: int,
        seed: int,
        reference_images: list[Path],
    ) -> str:
        response = httpx.post(
            self._start_endpoint(),
            headers=self._headers(),
            json=self._request_payload(
                prompt=prompt,
                generation_duration=generation_duration,
                seed=seed,
                reference_images=reference_images,
            ),
            timeout=90,
        )
        response.raise_for_status()
        operation = response.json()
        operation_name = operation.get("name")
        if not operation_name:
            raise RuntimeError(f"Veo did not return an operation name: {operation}")
        return str(operation_name)

    def _poll_operation(self, operation_name: str) -> dict[str, Any]:
        poll_url = self._poll_url(operation_name)
        for _ in range(120):
            if self.backend == "gemini":
                poll = httpx.get(poll_url, headers=self._headers(), timeout=60)
            else:
                poll = httpx.post(
                    poll_url,
                    headers=self._headers(),
                    json={"operationName": operation_name},
                    timeout=60,
                )
            poll.raise_for_status()
            data = poll.json()
            if data.get("done"):
                if data.get("error"):
                    raise VeoTerminalError(f"Veo generation failed: {data['error']}")
                response = data.get("response", data)
                if not isinstance(response, dict):
                    raise RuntimeError(f"Unexpected Veo operation response: {data}")
                return response
            time.sleep(5)
        raise TimeoutError("Veo generation did not complete within the polling window")

    @classmethod
    def _find_video_output(
        cls, value: Any
    ) -> tuple[str | None, str | None, dict[str, Any]] | None:
        if isinstance(value, list):
            for item in value:
                found = cls._find_video_output(item)
                if found:
                    return found
            return None
        if not isinstance(value, dict):
            return None

        encoded = value.get("bytesBase64Encoded")
        uri = value.get("gcsUri") or value.get("uri")
        if isinstance(encoded, str) or isinstance(uri, str):
            return (
                encoded if isinstance(encoded, str) else None,
                uri if isinstance(uri, str) else None,
                value,
            )

        gcs_uris = value.get("gcsUris")
        if isinstance(gcs_uris, list):
            for candidate in gcs_uris:
                if isinstance(candidate, str):
                    return None, candidate, value

        for key in (
            "generateVideoResponse",
            "generatedSamples",
            "videos",
            "predictions",
            "video",
            "response",
        ):
            if key in value:
                found = cls._find_video_output(value[key])
                if found:
                    return found
        return None

    def _download_output(self, result: dict[str, Any], output_path: Path) -> None:
        found = self._find_video_output(result)
        if not found:
            raise RuntimeError(f"Unexpected Veo response: {result}")
        video_bytes, uri, item = found
        temporary = output_path.with_name(
            f".{output_path.name}.{uuid.uuid4().hex}.downloading"
        )
        try:
            if video_bytes:
                temporary.write_bytes(base64.b64decode(video_bytes, validate=True))
            elif uri and uri.startswith("gs://"):
                try:
                    from google.cloud import storage
                except ImportError as exc:
                    raise RuntimeError(
                        "Install google-cloud-storage to download Veo output"
                    ) from exc
                parsed = urlparse(uri)
                client = storage.Client(project=self.project or None)
                client.bucket(parsed.netloc).blob(
                    parsed.path.lstrip("/")
                ).download_to_filename(str(temporary))
            elif uri:
                headers = self._headers() if self.backend == "gemini" else None
                download = httpx.get(
                    uri,
                    headers=headers,
                    timeout=300,
                    follow_redirects=self.backend == "gemini",
                )
                download.raise_for_status()
                temporary.write_bytes(download.content)
            else:
                raise RuntimeError(
                    f"Veo output contains no downloadable video: {item}"
                )

            if not is_valid_video(temporary):
                raise RuntimeError(
                    "Veo returned an incomplete or invalid video; the saved "
                    "operation will be resumed without promoting the partial file"
                )
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)

    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        output_path: Path,
        seed: int,
        reference_image: Path | None = None,
        reference_images: list[Path] | None = None,
    ) -> VideoResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        references = [
            path
            for path in (reference_images or [])
            if path.exists()
        ]
        if reference_image and reference_image.exists() and reference_image not in references:
            references.insert(0, reference_image)
        if len(references) > 3:
            raise ValueError("Veo 3.1 accepts at most three reference images")
        has_reference = bool(references)
        # Subject-reference generation is fixed to 8 seconds in Veo 3.1.
        generation_duration = (
            8
            if has_reference
            else 4
            if duration_seconds <= 4
            else 6
            if duration_seconds <= 6
            else 8
        )

        request_fingerprint = self._request_fingerprint(
            prompt=prompt,
            generation_duration=generation_duration,
            seed=seed,
            reference_images=references,
        )
        operation_name = self._load_operation(output_path, request_fingerprint)
        if operation_name is None:
            self._save_submission_marker(
                output_path,
                generation_duration,
                request_fingerprint,
            )
            try:
                operation_name = self._start_operation(
                    prompt=prompt,
                    generation_duration=generation_duration,
                    seed=seed,
                    reference_images=references,
                )
            except httpx.HTTPStatusError as exc:
                # A rejected 4xx request was not accepted for generation and is
                # safe to submit again. Timeouts and 5xx responses are ambiguous,
                # so retain the marker and require reconciliation.
                if 400 <= exc.response.status_code < 500:
                    self.operation_sidecar_path(output_path).unlink(missing_ok=True)
                raise
            # This is deliberately durable before the first poll: a worker retry must
            # resume the paid operation instead of submitting a duplicate generation.
            self._save_operation(
                output_path,
                operation_name,
                generation_duration,
                request_fingerprint,
            )

        try:
            result = self._poll_operation(operation_name)
        except VeoTerminalError as exc:
            self.operation_sidecar_path(output_path).write_text(
                json.dumps(
                    {
                        "backend": self.backend,
                        "model": self.model,
                        "state": "terminal_error",
                        "operation_name": operation_name,
                        "duration_seconds": generation_duration,
                        "request_fingerprint": request_fingerprint,
                        "error": str(exc),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            raise
        self._download_output(result, output_path)
        self.operation_sidecar_path(output_path).unlink()

        price_per_second = veo_price_per_second(self.backend, self.model)
        estimated_cost = generation_duration * price_per_second
        return VideoResult(
            path=output_path,
            provider="google-veo",
            duration_seconds=float(generation_duration),
            cost_usd=round(estimated_cost, 4),
            metadata={
                "backend": self.backend,
                "model": self.model,
                "operation": operation_name,
                "reference_used": has_reference,
                "reference_count": len(references),
                "price_per_second_usd": price_per_second,
            },
        )
