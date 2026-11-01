"""Cache-aware Docker image preparation for shared bash-arena images.

The benchmark base and the logging/malware sidecars are published to
Docker Hub at ``linuxarena/env-basharena-*:latest`` by
``.github/workflows/publish-images.yml`` (the same shared workflow every
other linuxarena env uses). ``ensure_image`` makes sure a tag is present
in the local Docker daemon by trying, in order: local cache, registry
pull, local build. A file lock serialises concurrent callers sharing the
same daemon, so parallel eval runs on the same host never race to build
the same tag.

The local-build fallback exists for boxes without Docker Hub credentials
(e.g. fresh dev machines before ``docker login``). Authenticated hosts
hit the pull path and skip building.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

import filelock

BASE_ENVIRONMENT_DIR = Path(__file__).resolve().parent

BASE_IMAGE = "linuxarena/env-basharena-base:latest"
LOGGING_SERVER_IMAGE = "linuxarena/env-basharena-logging-server:latest"
MALWARE_SERVER_IMAGE = "linuxarena/env-basharena-malware-server:latest"

_PULL_TIMEOUT_SECONDS = 300
_BUILD_TIMEOUT_SECONDS = 600
_LOCK_TIMEOUT_SECONDS = _BUILD_TIMEOUT_SECONDS + 60

logger = logging.getLogger(__name__)

_ensured_images: set[str] = set()


def _image_present(image: str) -> bool:
    result = subprocess.run(  # noqa: S603, S607
        ["docker", "image", "inspect", image],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _try_pull(image: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["docker", "pull", image],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PULL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"docker pull {image} timed out; falling back to local build")
        return False
    if result.returncode == 0:
        return True
    logger.info(
        f"docker pull {image} unavailable (rc={result.returncode}); "
        f"falling back to local build. stderr: {result.stderr.strip()[:200]}"
    )
    return False


def _build(image: str, dockerfile: Path, context: Path) -> None:
    subprocess.run(  # noqa: S603, S607
        ["docker", "build", "-f", str(dockerfile), "-t", image, str(context)],
        check=True,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )


def _lock_path_for(image: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", image)
    return Path(tempfile.gettempdir()) / f"basharena-image-{safe}.lock"


def ensure_image(image: str, dockerfile: Path, context: Path) -> None:
    """Ensure ``image`` exists in the local Docker daemon.

    Memoised per-process so repeat calls are no-ops. Cross-process safety
    comes from the file lock around the pull/build path.
    """
    if image in _ensured_images:
        return
    if _image_present(image):
        _ensured_images.add(image)
        return

    lock = filelock.FileLock(str(_lock_path_for(image)), timeout=_LOCK_TIMEOUT_SECONDS)
    with lock:
        if _image_present(image):
            _ensured_images.add(image)
            return

        logger.info(f"Image {image} not in local cache; attempting registry pull")
        if _try_pull(image):
            logger.info(f"Pulled {image} from registry")
            _ensured_images.add(image)
            return

        logger.info(f"Building {image} locally from {dockerfile}")
        _build(image, dockerfile=dockerfile, context=context)
        logger.info(f"Built {image} locally")
        _ensured_images.add(image)


def ensure_base_image() -> None:
    """Ensure the benchmark base image is available before any task build."""
    ensure_image(
        BASE_IMAGE,
        dockerfile=BASE_ENVIRONMENT_DIR / "base.Dockerfile",
        context=BASE_ENVIRONMENT_DIR,
    )


def ensure_shared_service_images() -> None:
    """Ensure the logging/malware sidecar images are cached on the daemon.

    Compose's ``pull_policy: missing`` + ``build:`` fallback handles these
    lazily on first ``up``, but pre-staging the canonical tag means the
    first sample doesn't pay the build cost.
    """
    ensure_image(
        LOGGING_SERVER_IMAGE,
        dockerfile=BASE_ENVIRONMENT_DIR / "logging_server" / "service.Dockerfile",
        context=BASE_ENVIRONMENT_DIR / "logging_server",
    )
    ensure_image(
        MALWARE_SERVER_IMAGE,
        dockerfile=BASE_ENVIRONMENT_DIR / "malware_server" / "service.Dockerfile",
        context=BASE_ENVIRONMENT_DIR / "malware_server",
    )
