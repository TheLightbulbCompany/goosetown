# Sprite Pipeline Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PixelLab sprite-animation generation reliable under concurrency by pooling the HTTP client, batching all directions into one PixelLab call, gating every generation path behind one concurrency semaphore, and classifying error codes — eliminating the `connect_tcp` bursts that drop animations (#81).

**Architecture:** All PixelLab calls share one pooled `httpx.AsyncClient` (caps sockets). A single `queue_animation()` submits all 4 directions of one animation in one `POST /characters/animations` call (template or v3), replacing the per-direction `animate-with-text-v3` loop *and* the manual rotation-fetch. `generate_animation()` wraps submit+poll+frame-collect behind a global `asyncio.Semaphore` that signup, activity, and admin paths all acquire. Frames are fetched as each direction completes (jobs 404 after cleanup).

**Tech Stack:** Python 3.12, FastAPI, httpx (async), PIL, pytest. Spec: `docs/superpowers/specs/2026-06-09-sprite-pipeline-reliability-design.md`.

**Working branch:** `feat/sprite-pipeline-reliability` (already created off origin/main).

**Run tests from `apps/backend/`:** `uv run pytest <path> -v` and `uv run ruff check . && uv run ruff format --check .`

---

### Task 1: Config — pool + concurrency env settings

**Files:**
- Modify: `apps/backend/core/config.py`
- Test: `apps/backend/tests/unit/test_config_pixellab.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/test_config_pixellab.py
"""PixelLab pool + concurrency settings exist with sane defaults."""

from core.config import settings


def test_pixellab_concurrency_defaults():
    assert settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS == 2
    assert settings.PIXELLAB_POOL_MAX_CONNECTIONS == 10
    assert settings.PIXELLAB_POOL_MAX_KEEPALIVE == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config_pixellab.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'PIXELLAB_MAX_CONCURRENT_ANIMATIONS'`

- [ ] **Step 3: Add the settings**

Find the `Settings` class in `core/config.py` (it reads env vars, e.g. `DYNAMODB_TABLE_PREFIX`). Add these three fields alongside the other PixelLab-adjacent settings, following the file's existing pattern (env-var read with default). If the class uses pydantic `BaseSettings`, add typed fields; if it reads `os.environ`, mirror that. Example for the `os.environ` pattern:

```python
    PIXELLAB_MAX_CONCURRENT_ANIMATIONS: int = int(os.environ.get("PIXELLAB_MAX_CONCURRENT_ANIMATIONS", "2"))
    PIXELLAB_POOL_MAX_CONNECTIONS: int = int(os.environ.get("PIXELLAB_POOL_MAX_CONNECTIONS", "10"))
    PIXELLAB_POOL_MAX_KEEPALIVE: int = int(os.environ.get("PIXELLAB_POOL_MAX_KEEPALIVE", "5"))
```

(Read `core/config.py` first and match its exact idiom — pydantic `Field(...)` vs class attr.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config_pixellab.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/config.py apps/backend/tests/unit/test_config_pixellab.py
git commit -m "feat(sprites): pixellab pool + concurrency settings"
```

---

### Task 2: Pooled httpx client + route `_request` through it

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (top-of-module helpers; `_request` ~line 182)
- Test: `apps/backend/tests/unit/services/test_pixellab_pool.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_pixellab_pool.py
"""The PixelLab service reuses a single pooled httpx client across calls."""

import httpx
import pytest

from core.services import pixellab_service as pxl_mod


def test_get_client_returns_same_instance():
    c1 = pxl_mod._get_pixellab_client()
    c2 = pxl_mod._get_pixellab_client()
    assert c1 is c2
    assert isinstance(c1, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_close_client_lets_a_fresh_one_be_made():
    c1 = pxl_mod._get_pixellab_client()
    await pxl_mod.close_pixellab_client()
    c2 = pxl_mod._get_pixellab_client()
    assert c1 is not c2
    await pxl_mod.close_pixellab_client()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_pixellab_pool.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_get_pixellab_client'`

- [ ] **Step 3: Add the pooled client helpers**

In `pixellab_service.py`, after the `_RETRY_*` constants (~line 23) add:

```python
# One pooled client shared across all PixelLab calls — keep-alive reuse + an
# explicit socket cap, instead of a fresh AsyncClient (new TLS handshake) per
# call. asyncio is single-threaded so the lazy create is race-free (no await
# between the None check and the assignment).
_pixellab_client: httpx.AsyncClient | None = None


def _get_pixellab_client() -> httpx.AsyncClient:
    global _pixellab_client
    if _pixellab_client is None or _pixellab_client.is_closed:
        _pixellab_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=settings.PIXELLAB_POOL_MAX_CONNECTIONS,
                max_keepalive_connections=settings.PIXELLAB_POOL_MAX_KEEPALIVE,
            ),
            timeout=httpx.Timeout(30.0),
        )
    return _pixellab_client


async def close_pixellab_client() -> None:
    """Close the pooled client on app shutdown."""
    global _pixellab_client
    if _pixellab_client is not None and not _pixellab_client.is_closed:
        await _pixellab_client.aclose()
    _pixellab_client = None
```

- [ ] **Step 4: Route `_request` through the pooled client**

In `_request` (~line 196), replace the per-call client:

```python
        client = _get_pixellab_client()
        for attempt in range(retries):
            try:
                resp = await client.request(method, url, headers=headers, json=json, timeout=timeout)
```

(Delete the `async with httpx.AsyncClient() as client:` wrapper and de-indent the `resp = await ...` line accordingly. Keep the rest of the retry loop unchanged.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/services/test_pixellab_pool.py tests/unit/services/test_pixellab_service.py -v`
Expected: PASS (the existing `test_pixellab_service.py` patches `httpx.AsyncClient`; update those mocks in Task 12 if any fail — for now the retry tests construct the client themselves, so verify they still pass; if they patch `pixellab_service.httpx.AsyncClient`, they still intercept `_get_pixellab_client`'s construction).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_pixellab_pool.py
git commit -m "feat(sprites): pooled httpx client for PixelLab (no per-call socket churn)"
```

---

### Task 3: Error classification — `402` credit alarm, distinct codes

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (`_request` retry/return logic + new exception)
- Test: `apps/backend/tests/unit/services/test_pixellab_service.py` (add cases)

- [ ] **Step 1: Write the failing tests**

Add to `test_pixellab_service.py` (it already has `_resp`/`_client_yielding` helpers):

```python
@pytest.mark.asyncio
async def test_402_raises_credit_error_no_retry():
    from core.services.pixellab_service import PixelLabCreditError

    service = PixelLabService(api_key="stub")
    client = _client_yielding([_resp(402)])
    with (
        patch("core.services.pixellab_service.httpx.AsyncClient", return_value=client),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()) as sleep,
    ):
        with pytest.raises(PixelLabCreditError):
            await service.get_character("char-x")
    assert client.request.await_count == 1   # no retry on 402
    assert sleep.await_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_pixellab_service.py::test_402_raises_credit_error_no_retry -v`
Expected: FAIL — `ImportError: cannot import name 'PixelLabCreditError'`

> Note: `_client_yielding` builds a fake whose `.request` walks a side-effect list; this test relies on Task 2's pooled client still being patchable via `pixellab_service.httpx.AsyncClient`. Since `_get_pixellab_client` calls `httpx.AsyncClient(...)`, patching `pixellab_service.httpx.AsyncClient` returns the fake. Call `await pixellab_service.close_pixellab_client()` is NOT needed because the patched fake replaces construction; but if a real client leaks between tests, add an autouse fixture in a later step.

- [ ] **Step 3: Add the exception + 402 handling**

Near the top of `pixellab_service.py` (after the constants):

```python
class PixelLabCreditError(Exception):
    """Raised on HTTP 402 — out of PixelLab generations/credits. Not retryable;
    alarm-worthy (a fresh-signup spike can drain credits silently)."""
```

In `_request`, inside the `else:` branch (after a response is received), BEFORE the retryable-status check (~line 209):

```python
                if resp.status_code == 402:
                    logger.error("PixelLab CREDIT EXHAUSTED (402) on %s %s — sprite gen will fail until topped up", method, endpoint)
                    raise PixelLabCreditError(f"PixelLab 402 on {endpoint}")
                if resp.status_code not in _RETRYABLE_STATUS:
                    return resp
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/services/test_pixellab_service.py -v`
Expected: PASS (new 402 test + all existing retry tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_pixellab_service.py
git commit -m "feat(sprites): classify PixelLab 402 as non-retryable credit error"
```

---

### Task 4: Global animation-concurrency semaphore

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py`
- Test: `apps/backend/tests/unit/services/test_pixellab_pool.py` (add)

- [ ] **Step 1: Write the failing test**

```python
def test_animation_semaphore_bound_matches_setting():
    from core.config import settings
    from core.services.pixellab_service import _get_animation_semaphore

    sem = _get_animation_semaphore()
    assert _get_animation_semaphore() is sem  # singleton
    assert sem._value == settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_pixellab_pool.py::test_animation_semaphore_bound_matches_setting -v`
Expected: FAIL — `ImportError: cannot import name '_get_animation_semaphore'`

- [ ] **Step 3: Add the semaphore helper**

In `pixellab_service.py` after the pooled-client helpers:

```python
# One global gate on concurrent in-flight animation generations across EVERY
# path (signup, activity, admin). Each animation = up to 4 PixelLab jobs, so the
# default of 2 keeps us ~<=8 concurrent jobs — under PixelLab's (undocumented)
# concurrency cap. Tunable via PIXELLAB_MAX_CONCURRENT_ANIMATIONS.
_animation_semaphore: "asyncio.Semaphore | None" = None


def _get_animation_semaphore() -> asyncio.Semaphore:
    global _animation_semaphore
    if _animation_semaphore is None:
        _animation_semaphore = asyncio.Semaphore(settings.PIXELLAB_MAX_CONCURRENT_ANIMATIONS)
    return _animation_semaphore
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_pixellab_pool.py::test_animation_semaphore_bound_matches_setting -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_pixellab_pool.py
git commit -m "feat(sprites): global animation-concurrency semaphore"
```

---

### Task 5: `CARDINAL_4` + `queue_animation()` unified helper

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py`
- Test: `apps/backend/tests/unit/services/test_queue_animation.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_queue_animation.py
"""queue_animation builds the right characters/animations request per type and
returns (directions, job_ids) — one batched call, all directions, no rotation fetch."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.pixellab_service import PixelLabService


def _ok_resp(body):
    r = MagicMock()
    r.status_code = 200
    r.json = MagicMock(return_value=body)
    r.raise_for_status = MagicMock()
    return r


@pytest.mark.asyncio
async def test_queue_animation_template_walk():
    service = PixelLabService(api_key="stub")
    captured = {}

    async def fake_request(method, url, *, json=None, timeout=30.0, retries=4):
        captured["url"] = url
        captured["json"] = json
        return _ok_resp({"directions": ["south", "west", "east", "north"], "background_job_ids": ["j1", "j2", "j3", "j4"]})

    with patch.object(service, "_request", side_effect=fake_request):
        dirs, jobs = await service.queue_animation("char-1", "walk")

    assert captured["url"].endswith("/characters/animations")
    assert captured["json"]["mode"] == "template"
    assert captured["json"]["template_animation_id"] == "walk"
    assert captured["json"]["directions"] == ["south", "west", "east", "north"]
    assert "action_description" not in captured["json"]
    assert len(jobs) == 4


@pytest.mark.asyncio
async def test_queue_animation_v3_talking():
    service = PixelLabService(api_key="stub")
    captured = {}

    async def fake_request(method, url, *, json=None, timeout=30.0, retries=4):
        captured["json"] = json
        return _ok_resp({"directions": ["south", "west", "east", "north"], "background_job_ids": ["j1", "j2", "j3", "j4"]})

    with patch.object(service, "_request", side_effect=fake_request):
        dirs, jobs = await service.queue_animation("char-1", "talking")

    assert captured["json"]["mode"] == "v3"
    assert captured["json"]["action_description"]  # non-empty prompt
    assert captured["json"]["enhance_prompt"] is True
    assert captured["json"]["frame_count"] >= 4
    assert "template_animation_id" not in captured["json"]
    assert len(jobs) == 4


@pytest.mark.asyncio
async def test_queue_animation_unknown_returns_empty():
    service = PixelLabService(api_key="stub")
    dirs, jobs = await service.queue_animation("char-1", "not_a_real_animation")
    assert dirs == [] and jobs == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_queue_animation.py -v`
Expected: FAIL — `AttributeError: 'PixelLabService' object has no attribute 'queue_animation'`

- [ ] **Step 3: Add `CARDINAL_4` and `queue_animation`**

After `PIXELLAB_API_URL` constant add:

```python
# We composite 4-direction spritesheets even though characters carry 8 rotations.
# v3/custom mode defaults to south-only if directions is omitted (template
# defaults to all) — so we ALWAYS pass the 4 cardinals explicitly.
CARDINAL_4: list[str] = ["south", "west", "east", "north"]
```

Add the method to `PixelLabService` (replaces `animate_character` + `queue_custom_animation`):

```python
    async def queue_animation(self, character_id: str, anim_name: str) -> tuple[list[str], list[str]]:
        """Queue ALL cardinal directions of one animation in a single
        ``POST /characters/animations`` call. Returns ``(directions, job_ids)``
        aligned by index. Handles template + v3 from ANIMATION_REGISTRY; v3 uses
        the character's own rotations as the per-direction reference (no manual
        reference-frame fetch). Returns ([], []) for an unknown animation."""
        config = ANIMATION_REGISTRY.get(anim_name)
        if not config:
            logger.warning("queue_animation: unknown animation %r — skipping", anim_name)
            return [], []

        body: dict[str, Any] = {"character_id": character_id, "directions": CARDINAL_4}
        if config["type"] == "template":
            body["mode"] = "template"
            body["template_animation_id"] = config["template_id"]
        else:
            body["mode"] = "v3"
            body["action_description"] = config["prompt"]
            body["frame_count"] = int(config.get("frames", 8))
            body["enhance_prompt"] = True
            body["animation_name"] = anim_name

        resp = await self._request("POST", f"{PIXELLAB_API_URL}/characters/animations", json=body, timeout=60.0)
        if resp.status_code not in (200, 202):
            logger.error("PixelLab characters/animations %s -> %s: %s", anim_name, resp.status_code, resp.text[:300])
            resp.raise_for_status()
        data = resp.json()
        directions = data.get("directions") or CARDINAL_4
        job_ids = data.get("background_job_ids") or []
        logger.info("queue_animation %s/%s: %d jobs (%s)", character_id[:8], anim_name, len(job_ids), config["type"])
        return directions, job_ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_queue_animation.py -v`
Expected: PASS (3 tests)

> Note: `test_queue_animation_v3_talking` requires `ANIMATION_REGISTRY["talking"]` to be a `type=="custom"` entry with a `prompt` — already true today. `test_queue_animation_template_walk` requires `ANIMATION_REGISTRY["walk"]` to be `type=="template"` with `template_id=="walk"` — already true today.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_queue_animation.py
git commit -m "feat(sprites): unified queue_animation (batched directions, template+v3)"
```

---

### Task 6: 404-safe frame collection — `_frames_from_job` + `_poll_and_collect`

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py`
- Test: `apps/backend/tests/unit/services/test_poll_and_collect.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_poll_and_collect.py
"""Frames are collected as each direction completes; a vanished (404) job is
terminal, not an infinite wait."""

import base64
import io
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

from core.services.pixellab_service import PixelLabService


def _rgba_b64(w=8, h=8):
    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    return base64.b64encode(img.tobytes()).decode()


def _completed_job(n_frames=2, w=8, h=8):
    return {
        "status": "completed",
        "last_response": {"images": [{"type": "rgba_bytes", "width": w, "height": h, "base64": _rgba_b64(w, h)} for _ in range(n_frames)]},
    }


def test_frames_from_job_decodes_rgba_bytes():
    service = PixelLabService(api_key="stub")
    frames = service._frames_from_job(_completed_job(n_frames=3))
    assert len(frames) == 3
    # each frame is decodable PNG bytes
    Image.open(io.BytesIO(frames[0]))


@pytest.mark.asyncio
async def test_poll_collects_per_direction_and_handles_404():
    service = PixelLabService(api_key="stub")
    # j1 completes; j2 vanishes (404)
    statuses = {
        "j1": _completed_job(n_frames=2),
        "j2": httpx.HTTPStatusError("404", request=None, response=type("R", (), {"status_code": 404})()),
    }

    async def fake_get_job_status(jid):
        v = statuses[jid]
        if isinstance(v, Exception):
            raise v
        return v

    with (
        patch.object(service, "get_job_status", side_effect=fake_get_job_status),
        patch("core.services.pixellab_service.asyncio.sleep", AsyncMock()),
    ):
        out = await service._poll_and_collect(["south", "west"], ["j1", "j2"], None, "walk", max_attempts=3)

    assert "south" in out and len(out["south"]) == 2
    assert "west" not in out  # vanished job yields no frames but does not hang
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_poll_and_collect.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_frames_from_job'`

- [ ] **Step 3: Implement both methods**

Add to `PixelLabService`:

```python
    def _frames_from_job(self, job: dict) -> list[bytes]:
        """Decode a completed job's frames (last_response.images, rgba_bytes) into
        PNG bytes for compositing. Returns [] if no usable images."""
        import base64
        import io

        from PIL import Image

        images = (job.get("last_response") or {}).get("images") or []
        frames: list[bytes] = []
        for im in images:
            b64 = im.get("base64")
            if not b64:
                continue
            raw = base64.b64decode(b64)
            t = (im.get("type") or "").lower()
            try:
                if "rgba" in t or t == "bytes":
                    img = Image.frombytes("RGBA", (im["width"], im["height"]), raw)
                else:
                    img = Image.open(io.BytesIO(raw)).convert("RGBA")
            except Exception:
                logger.warning("Could not decode an animation frame (type=%s)", t)
                continue
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            frames.append(buf.getvalue())
        return frames

    async def _poll_and_collect(
        self,
        directions: list[str],
        job_ids: list[str],
        on_progress,
        label: str,
        max_attempts: int = 30,
        poll_interval: float = 10.0,
    ) -> dict[str, list[bytes]]:
        """Poll the per-direction jobs, fetching each direction's frames AS it
        completes (PixelLab cleans finished jobs up — a fetch deferred to after
        the whole batch can 404). Returns {direction: [png_bytes]}."""
        pending = dict(zip(job_ids, directions))  # job_id -> direction
        collected: dict[str, list[bytes]] = {}
        for _attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)
            done_now: list[str] = []
            for jid, direction in pending.items():
                try:
                    job = await self.get_job_status(jid)
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        logger.warning("Job %s (%s/%s) vanished (404) before fetch", jid, label, direction)
                        done_now.append(jid)
                    continue
                except Exception:
                    continue
                status = job.get("status")
                if status == "completed":
                    frames = self._frames_from_job(job)
                    if frames:
                        collected[direction] = frames
                    done_now.append(jid)
                elif status == "failed":
                    logger.warning("Job %s (%s/%s) failed", jid, label, direction)
                    done_now.append(jid)
            for jid in done_now:
                pending.pop(jid, None)
            if on_progress:
                await on_progress(f"{label} {len(collected)}/{len(directions)} done")
            if not pending:
                break
        return collected
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_poll_and_collect.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_poll_and_collect.py
git commit -m "feat(sprites): 404-safe per-direction frame collection"
```

---

### Task 7: `generate_animation()` — semaphore-gated submit+collect

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py`
- Test: `apps/backend/tests/unit/services/test_generate_animation.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_generate_animation.py
"""generate_animation acquires the global semaphore, submits one batched call,
and returns the collected per-direction frames."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.pixellab_service import PixelLabService


@pytest.mark.asyncio
async def test_generate_animation_happy_path():
    service = PixelLabService(api_key="stub")
    with (
        patch.object(service, "queue_animation", AsyncMock(return_value=(["south", "west"], ["j1", "j2"]))),
        patch.object(service, "_poll_and_collect", AsyncMock(return_value={"south": [b"png"], "west": [b"png"]})),
    ):
        out = await service.generate_animation("char-1", "walk")
    assert set(out.keys()) == {"south", "west"}


@pytest.mark.asyncio
async def test_generate_animation_no_jobs_returns_empty():
    service = PixelLabService(api_key="stub")
    with patch.object(service, "queue_animation", AsyncMock(return_value=([], []))):
        out = await service.generate_animation("char-1", "walk")
    assert out == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_generate_animation.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'generate_animation'`

- [ ] **Step 3: Implement `generate_animation`**

```python
    async def generate_animation(
        self,
        character_id: str,
        anim_name: str,
        *,
        on_progress=None,
        max_attempts: int = 30,
    ) -> dict[str, list[bytes]]:
        """Generate ONE animation (all 4 directions) end-to-end under the global
        concurrency gate: submit the single batched call, then collect frames as
        each direction completes. Returns {direction: [png_bytes]} (empty on no
        jobs). This is the single entry point signup, activity, and admin all use."""
        async with _get_animation_semaphore():
            directions, job_ids = await self.queue_animation(character_id, anim_name)
            if not job_ids:
                return {}
            return await self._poll_and_collect(directions, job_ids, on_progress, anim_name, max_attempts=max_attempts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_generate_animation.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_generate_animation.py
git commit -m "feat(sprites): generate_animation — semaphore-gated submit+collect"
```

---

### Task 8: Registry template/v3 split + cleanup

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (`ANIMATION_REGISTRY` ~line 35-169)
- Test: `apps/backend/tests/unit/services/test_animation_registry.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_animation_registry.py
"""Template/v3 split: only walk+idle are templates; everything else is v3.
Every activity_registry animation resolves to a registry key."""

from core.services.pixellab_service import ANIMATION_REGISTRY


def test_only_walk_idle_are_templates():
    templates = {k for k, v in ANIMATION_REGISTRY.items() if v["type"] == "template"}
    assert templates == {"walk", "idle"}


def test_unused_templates_removed():
    for gone in ("run", "crouch", "pick_up"):
        assert gone not in ANIMATION_REGISTRY


def test_drink_is_now_v3():
    assert ANIMATION_REGISTRY["drink"]["type"] == "custom"
    assert ANIMATION_REGISTRY["drink"].get("prompt")


def test_every_activity_animation_resolves():
    from core.services.activity_registry import ACTIVITIES  # adjust import to the real symbol

    used = {a.animation for a in ACTIVITIES.values()} if hasattr(ACTIVITIES, "values") else set()
    # idle/drink/etc. — every referenced animation must be a registry key
    for name in used:
        assert name in ANIMATION_REGISTRY, f"activity animation {name!r} missing from ANIMATION_REGISTRY"
```

> Before writing the last test, read `apps/backend/core/services/activity_registry.py` to find the real collection name (the test grep showed `animation=` on Activity dataclasses). Replace `ACTIVITIES` with the actual exported registry (e.g. a dict or list of `Activity`), and adjust `used` to collect each entry's `.animation`. If a list, use `{a.animation for a in ACTIVITY_LIST}`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_animation_registry.py -v`
Expected: FAIL — `run`/`crouch`/`pick_up` still present; `drink` still `template`.

- [ ] **Step 3: Edit `ANIMATION_REGISTRY`**

In `pixellab_service.py`:
1. **Delete** the three template entries `run`, `crouch`, `pick_up`.
2. **Change `drink`** from a template entry to a custom (v3) entry:

```python
    "drink": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 6.0,
        "prompt": (
            "Raising a cup to the mouth with one hand, taking a sip, lowering "
            "it again, then settling back to a relaxed standing pose. " + _LOOP_SUFFIX
        ),
    },
```
3. Leave `walk` and `idle` as templates; leave the other custom entries as-is. Verify `TEMPLATE_ANIMATIONS`/`CUSTOM_ANIMATIONS`/`SIGNUP_ANIMATIONS` derived dicts still compute correctly (they filter by `type`/`auto_generate` — no change needed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_animation_registry.py -v`
Expected: PASS

- [ ] **Step 5: Run the existing dropped-currency regression + full service tests**

Run: `uv run pytest tests/unit/services/ tests/unit/test_round_1_activity_layer.py -v 2>&1 | tail -20`
Expected: PASS (no entry references the removed templates)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/services/test_animation_registry.py
git commit -m "feat(sprites): template/v3 split — walk+idle templates, drink->v3, drop unused"
```

---

### Task 9: Wire signup animations through `generate_animation`

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (`queue_signup_animations` ~line 451) and `sprite_pipeline.py` (the "animating"/"downloading" phases that consume signup frames)
- Test: `apps/backend/tests/unit/services/test_signup_animations.py` (create)

- [ ] **Step 1: Read the current consumers**

Read `queue_signup_animations` (pixellab_service.py ~451) and `sprite_pipeline.py` phases "animating"+"downloading" (~lines 403-440). Today: `queue_signup_animations` loops `SIGNUP_ANIMATIONS`, calls `animate_character`/`queue_custom_animation`, then `wait_for_jobs`; then `sprite_pipeline` separately `fetch_animation_spritesheets` from the character ZIP and composites. The new path makes `queue_signup_animations` return the collected frames directly (no ZIP round-trip for signup animations).

- [ ] **Step 2: Write the failing test**

```python
# apps/backend/tests/unit/services/test_signup_animations.py
"""queue_signup_animations generates each auto_generate animation via
generate_animation (sequentially) and returns {anim_name: {direction: frames}}."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.pixellab_service import PixelLabService, SIGNUP_ANIMATIONS


@pytest.mark.asyncio
async def test_signup_uses_generate_animation_per_anim():
    service = PixelLabService(api_key="stub")
    calls = []

    async def fake_generate(char, name, *, on_progress=None, max_attempts=30):
        calls.append(name)
        return {"south": [b"f"], "west": [b"f"], "east": [b"f"], "north": [b"f"]}

    with patch.object(service, "generate_animation", side_effect=fake_generate):
        out = await service.queue_signup_animations("char-1")

    assert set(calls) == set(SIGNUP_ANIMATIONS.keys())  # walk, idle, talking
    for name in SIGNUP_ANIMATIONS:
        assert name in out and "south" in out[name]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_signup_animations.py -v`
Expected: FAIL — current `queue_signup_animations` returns `dict[str, list[str]]` (job-id counts), not frames; signature/behavior differs.

- [ ] **Step 4: Rewrite `queue_signup_animations`**

Replace the body with:

```python
    async def queue_signup_animations(
        self,
        character_id: str,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> dict[str, dict[str, list[bytes]]]:
        """Generate every auto_generate signup animation (walk, idle, talking)
        sequentially via generate_animation. Sequential keeps signup within the
        concurrency gate's headroom and matches PixelLab's job limits. Returns
        {anim_name: {direction: [png_bytes]}} for the caller to composite."""
        results: dict[str, dict[str, list[bytes]]] = {}
        items = list(SIGNUP_ANIMATIONS.items())
        for idx, (name, _config) in enumerate(items):
            label = f"{name} ({idx + 1}/{len(items)})"
            try:
                frames = await self.generate_animation(
                    character_id, name, on_progress=on_progress, max_attempts=30
                )
                results[name] = frames
                logger.info("Signup animation %s: %d/%d directions", name, len(frames), len(CARDINAL_4))
            except Exception:
                logger.exception("Signup animation %r failed for %s — continuing", name, character_id)
                results[name] = {}
        return results
```

- [ ] **Step 5: Update `sprite_pipeline.py` "animating"/"downloading" phases**

In `sprite_pipeline.py`, the "animating" phase calls `queue_signup_animations` and the "downloading" phase composites from the ZIP. Change so the "animating" phase consumes the returned `{anim: {dir: frames}}` directly and composites each animation with the existing `composite_animation_spritesheet` (wrapped in `asyncio.to_thread`), uploads via `upload_sprite_to_s3`, and builds `sprite_animations` — removing the separate ZIP-based `fetch_animation_spritesheets` round-trip for the signup set. Read lines ~403-440 and replace the `raw_animations = await pxl.fetch_animation_spritesheets(char_id)` block with iteration over the `queue_signup_animations` return value:

```python
        signup_frames = await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
        sprite_animations: dict = {}
        for friendly, direction_frames in signup_frames.items():
            if not direction_frames:
                continue
            sheet = await asyncio.to_thread(composite_animation_spritesheet, direction_frames)
            if sheet:
                png_bytes, frame_count = sheet
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3, png_bytes, agent_id, settings.SPRITE_S3_BUCKET, f"{friendly}.png"
                )
                registry_entry = ANIMATION_REGISTRY.get(friendly, {})
                sprite_animations[friendly] = {
                    "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                    "frameCount": frame_count,
                    "fps": int(registry_entry.get("fps", 8)),
                }
```

> Verify `composite_animation_spritesheet(direction_frames)` accepts `{direction: [png_bytes]}` — it does today (used by the current ZIP path). The static-rotation step (Phase B, `build_static_spritesheet_from_rotations`) is unchanged. The walk sheet for movement comes from the `walk` signup animation here.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/services/test_signup_animations.py tests/unit/services/test_pixellab_service.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/core/services/sprite_pipeline.py apps/backend/tests/unit/services/test_signup_animations.py
git commit -m "feat(sprites): signup animations via generate_animation (batched, gated)"
```

---

### Task 10: Wire activity lazy-gen through `generate_animation` (template + custom)

**Files:**
- Modify: `apps/backend/core/services/activity_service.py` (`_generate_custom_animation` ~206-300, the `CUSTOM_ANIMATIONS.get` early return at ~217, the trigger at ~160-171)
- Test: `apps/backend/tests/unit/services/test_activity_animation.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_activity_animation.py
"""Activity animation generation covers template AND custom registry entries
(the order_coffee -> drink gap) and uses generate_animation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.services.activity_service as act


@pytest.mark.asyncio
async def test_generate_activity_animation_handles_any_registry_anim():
    # 'idle' is a template, 'reading' is custom — both must generate, not early-return
    gen = AsyncMock(return_value={"south": [b"f"], "west": [b"f"], "east": [b"f"], "north": [b"f"]})
    svc = MagicMock()
    with (
        patch("core.services.activity_service.PixelLabService") as PxlCls,
        patch("core.services.activity_service.TownService", return_value=svc),
        patch("core.services.activity_service.composite_v2_animation", return_value=(b"png", 4)),
        patch("core.services.activity_service.upload_sprites_to_s3", return_value="sprites/a/reading.png"),
    ):
        PxlCls.return_value.generate_animation = gen
        await act._generate_activity_animation("agent-1", "char-1", "reading")

    gen.assert_awaited_once()
    assert gen.await_args.args[1] == "reading"
```

> Read `activity_service.py` first for the real symbol names of the compositing/upload helpers (`composite_v2_animation`, `upload_sprites_to_s3`) and adjust the patch targets to match the actual imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_activity_animation.py -v`
Expected: FAIL — function is named `_generate_custom_animation`, early-returns on non-`CUSTOM_ANIMATIONS`, and calls `queue_custom_animation`.

- [ ] **Step 3: Rewrite the activity generator**

Rename `_generate_custom_animation` → `_generate_activity_animation` and replace its body to (a) accept any `ANIMATION_REGISTRY` entry (drop the `CUSTOM_ANIMATIONS.get(...) ; if not config: return`), (b) call `generate_animation`, (c) composite/upload the returned frames. Keep the `learning_status` set/clear and the `_custom_anim_in_progress` de-dup. Core:

```python
async def _generate_activity_animation(agent_id: str, character_id: str, animation_name: str) -> None:
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import composite_v2_animation, upload_sprites_to_s3

    if animation_name not in ANIMATION_REGISTRY:
        return

    pxl = PixelLabService()
    service = TownService()
    learning_status = f"Learning to {animation_name}…"
    try:
        service.update_agent_state(agent_id, status_message=learning_status)
    except Exception:
        logger.warning("Could not set learning status for agent %s", agent_id, exc_info=True)

    try:
        direction_frames = await pxl.generate_animation(character_id, animation_name)
        if not direction_frames:
            logger.warning("No frames for activity animation %s (agent %s)", animation_name, agent_id)
            return
        # composite + upload (reuse existing helpers; keep the prior shape)
        sheet = composite_v2_animation(direction_frames)  # adjust to the real signature
        if not sheet:
            return
        png_bytes, frame_count = sheet
        s3_key = upload_sprites_to_s3(png_bytes, agent_id, settings.SPRITE_S3_BUCKET, f"{animation_name}.png")
        registry_entry = ANIMATION_REGISTRY.get(animation_name, {})
        service.merge_sprite_animation(
            agent_id,
            animation_name,
            {
                "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                "frameCount": frame_count,
                "fps": int(registry_entry.get("fps", 8)),
            },
        )
    except Exception:
        logger.exception("Activity animation %s failed for agent %s", animation_name, agent_id)
    finally:
        service.update_agent_state(agent_id, status_message="")
```

> Read the existing `_generate_custom_animation` (lines ~260-337) for the EXACT compositing call (`composite_v2_animation` vs `composite_animation_spritesheet`), the exact upload helper signature, and how it merges into `spriteAnimations` (it may use `update_agent_state(..., sprite_animations=...)` rather than a `merge_sprite_animation` method — match whatever exists). Preserve that exact persistence call.

- [ ] **Step 4: Update the trigger call site**

At the lazy-trigger (~160-171), the check `if activity_anim not in agent_animations and anim_key not in _custom_anim_in_progress` should gate on `ANIMATION_REGISTRY` membership (not `CUSTOM_ANIMATIONS`), and the spawned task must call `_generate_activity_animation`. Update:

```python
        from core.services.pixellab_service import ANIMATION_REGISTRY
        if activity_anim in ANIMATION_REGISTRY and activity_anim not in agent_animations and anim_key not in _custom_anim_in_progress:
            if char_id:
                _custom_anim_in_progress.add(anim_key)
                gen_task = asyncio.create_task(_generate_activity_animation(agent_id, char_id, activity_anim))
                _activity_tasks.add(gen_task)
                gen_task.add_done_callback(_activity_tasks.discard)
```

Ensure `_custom_anim_in_progress.discard(anim_key)` still happens (in the `finally` of `_generate_activity_animation` or the done-callback — match the current cleanup).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/services/test_activity_animation.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/activity_service.py apps/backend/tests/unit/services/test_activity_animation.py
git commit -m "feat(sprites): activity animations via generate_animation (covers template drink gap)"
```

---

### Task 11: Delete dead code; admin regenerate alarm; client shutdown

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (delete `animate_character`, `queue_custom_animation`, `_get_rotation_base64`, `get_v2_animation_frames` if now unused)
- Modify: `apps/backend/routers/town.py` (admin regenerate: catch `PixelLabCreditError` → log alarm)
- Modify: `apps/backend/main.py` (call `close_pixellab_client` on shutdown)
- Test: existing suite

- [ ] **Step 1: Grep for remaining callers of the deprecated methods**

Run:
```bash
cd apps/backend
grep -rn "animate_character\|queue_custom_animation\|_get_rotation_base64\|get_v2_animation_frames" core routers tests
```
Expected: only the definitions + tests remain (signup/activity now use `generate_animation`). If a non-test caller remains, migrate it to `generate_animation`/`queue_animation` before deleting.

- [ ] **Step 2: Delete the dead methods**

Remove `animate_character`, `queue_custom_animation`, `_get_rotation_base64` from `pixellab_service.py`. Remove `get_v2_animation_frames` only if Step-1 grep shows no remaining callers (its logic now lives in `_frames_from_job`). Delete or update any unit tests that referenced them directly (e.g. tests asserting the old per-direction loop).

- [ ] **Step 3: Add the credit alarm to admin regenerate**

In `routers/town.py` `regenerate_sprite` `_run()` (and `recomposite_sprites` `_run_all()`), add a `PixelLabCreditError` except arm that logs a distinct ALARM line:

```python
        except PixelLabCreditError:
            logger.error("ALARM: PixelLab credits exhausted during regenerate for %s", agent_id)
```
Add `from core.services.pixellab_service import PixelLabCreditError` to the function's local imports.

- [ ] **Step 4: Close the pooled client on shutdown**

In `main.py`, find the FastAPI shutdown hook (`@app.on_event("shutdown")` or the lifespan context). Add:

```python
    from core.services.pixellab_service import close_pixellab_client
    await close_pixellab_client()
```

- [ ] **Step 5: Run the full unit suite + lint**

Run:
```bash
uv run pytest tests/ -q
uv run ruff check . && uv run ruff format --check .
```
Expected: all pass, lint clean. Fix any test that referenced deleted methods.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/pixellab_service.py apps/backend/routers/town.py apps/backend/main.py apps/backend/tests/
git commit -m "refactor(sprites): drop per-direction code; credit alarm; close pooled client on shutdown"
```

---

### Task 12: Final review, PR, deploy-verify

**Files:** none (verification)

- [ ] **Step 1: Full suite + lint once more**

Run:
```bash
cd apps/backend && uv run pytest tests/ -q && uv run ruff check . && uv run ruff format --check .
```
Expected: green.

- [ ] **Step 2: Push + open PR**

```bash
git push -u origin feat/sprite-pipeline-reliability
gh pr create --title "feat(sprites): reliable PixelLab pipeline (pooled client, batched directions, concurrency gate)" --body "Implements docs/superpowers/specs/2026-06-09-sprite-pipeline-reliability-design.md. Closes #81. Pooled httpx client; one batched characters/animations call per animation (no per-direction loop, no rotation fetch); global animation semaphore across signup/activity/admin; 402 credit alarm; 404-safe frame collection; walk+idle templates, rest v3."
```

- [ ] **Step 3: After merge + dev deploy — live verify the failure scenario**

Re-run the exact #81 repro: register a fresh agent AND fire an activity animation concurrently; confirm the backend log shows `Signup animations done` with all directions for walk/idle/talking and **no `connect_tcp` / "Failed to queue"** lines. Visually confirm the agent's sprite + an activity animation render. (test-scout is a usable throwaway target.)

- [ ] **Step 4: Mark task #81 complete.**

---

## Self-Review

**Spec coverage:**
- §1 pooled client → Task 2 ✓
- §2 unified helper (`queue_animation`, template+v3, batched) → Task 5 ✓; replaces animate_character/queue_custom_animation → deleted Task 11 ✓; removes `_get_rotation_base64` → Task 11 ✓
- §3 frame extraction (rgba_bytes, fetch-as-completes) → Task 6 ✓
- §4 unified semaphore across signup/activity/admin → Task 4 (helper) + Task 7 (generate_animation acquires) + Task 9 (signup) + Task 10 (activity) + Task 11 (admin) ✓
- §5 error classification (402/429/423/404) → Task 3 (402) ✓; 429/5xx retry already in `_request`; 423 stays caller-handled in `download_character_zip` (unchanged); 404-in-poll → Task 6 ✓
- §6 activity covers template+custom (drink gap) → Task 10 ✓
- §7 registry template/v3 split + cleanup → Task 8 ✓; description audit → folded into Task 8 Step 3 (drink prompt) — **gap: the broader "tighten talking + activity descriptions" wording review is light; acceptable as in-place polish during Task 8, not a separate task.**
- §"main.py shutdown" → Task 11 ✓

**Placeholder scan:** Tasks 8/10 contain "adjust to the real symbol/signature" notes — these are *read-then-match* instructions against existing code (composite/upload helper names, the activity registry symbol), not placeholders for logic. They're necessary because the exact helper signatures must be read at execution time; each names the concrete file+function to read. Acceptable.

**Type consistency:** `queue_animation` returns `(directions, job_ids)` (Task 5) consumed by `generate_animation`/`_poll_and_collect` (Tasks 6/7) ✓. `generate_animation` returns `{direction: [png_bytes]}` consumed by `queue_signup_animations` (Task 9) and `_generate_activity_animation` (Task 10) ✓. `_frames_from_job` returns `list[bytes]` PNG, matching what `composite_animation_spritesheet`/`composite_v2_animation` expect ✓. `PixelLabCreditError` defined Task 3, caught Task 11 ✓.
