"""PixelLab credit-balance poller.

Polls the PixelLab API every 15 minutes (driven by EventBridge schedule),
reads the remaining credit balance, and emits it as a CloudWatch custom
metric. The CloudWatch alarm in observability-stack.ts watches that
metric for credit drain.

PixelLab does not push to CloudWatch natively (it's a third-party SaaS),
so this poller is the bridge. Producer's 2026-05-04 launch-readiness
plan, Tier 1 #1.1: PixelLab credit-exhaustion is the largest unbounded
cost exposure (a 100-signup spike drains credits silently and sprite
generation starts failing for everyone).

Configuration (env vars):
- ``PIXELLAB_BALANCE_URL`` — default ``https://api.pixellab.ai/v2/balance``.
  Override if PixelLab's endpoint differs from the assumed shape.
- ``PIXELLAB_BALANCE_JSON_PATH`` — default ``credits_remaining``. Dotted
  path into the response JSON; e.g. ``data.credits`` for nested.
- ``PIXELLAB_API_KEY_SECRET_NAME`` — name of the Secrets Manager secret
  holding the API key. Wired by CDK.
- ``METRIC_NAMESPACE`` — default ``GooseTown``.
- ``METRIC_NAME`` — default ``PixelLabCreditsRemaining``.
- ``ENVIRONMENT`` — ``dev`` or ``prod``; tagged on the metric as a dim.

Outputs a single CloudWatch datapoint per invocation. If the PixelLab
call fails, emits a 0 (so the alarm fires) and re-raises so CloudWatch
Logs surface the error to on-call.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cached at cold-start; the secret value rarely rotates and reading it
# every 15 minutes is wasteful.
_cached_api_key: str | None = None


def _get_api_key() -> str:
    global _cached_api_key
    if _cached_api_key is not None:
        return _cached_api_key
    secret_name = os.environ.get("PIXELLAB_API_KEY_SECRET_NAME", "")
    if not secret_name:
        raise RuntimeError("PIXELLAB_API_KEY_SECRET_NAME env var not set")
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=secret_name)
    secret_string = resp.get("SecretString", "")
    # Secret may be a raw string OR a JSON object with an `api_key` field.
    # Both shapes are tolerated.
    try:
        parsed = json.loads(secret_string)
        if isinstance(parsed, dict):
            api_key = parsed.get("api_key") or parsed.get("PIXELLAB_API_KEY", "")
        else:
            api_key = secret_string
    except json.JSONDecodeError:
        api_key = secret_string
    if not api_key:
        raise RuntimeError(f"PixelLab API key empty in secret {secret_name}")
    _cached_api_key = api_key
    return api_key


def _walk_json_path(obj: object, dotted: str) -> object:
    """Walk a dotted path through a JSON object. ``credits_remaining`` →
    obj["credits_remaining"]; ``data.credits`` → obj["data"]["credits"]."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            raise ValueError(f"path component {part!r} requires a dict, got {type(cur).__name__}")
        if part not in cur:
            raise KeyError(f"path component {part!r} not found in response: keys={list(cur.keys())}")
        cur = cur[part]
    return cur


def _fetch_balance() -> int:
    url = os.environ.get("PIXELLAB_BALANCE_URL", "https://api.pixellab.ai/v2/balance")
    json_path = os.environ.get("PIXELLAB_BALANCE_JSON_PATH", "credits_remaining")
    api_key = _get_api_key()

    req = urllib.request.Request(  # noqa: S310 — URL is operator-configured, not user input
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.error("PixelLab balance HTTP %d: %s", exc.code, exc.read()[:200])
        raise
    except urllib.error.URLError as exc:
        logger.error("PixelLab balance URL error: %s", exc)
        raise

    raw = _walk_json_path(payload, json_path)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"PixelLab balance at path {json_path!r} is {raw!r} ({type(raw).__name__}), expected int"
        ) from exc


def _emit_metric(credits: int) -> None:
    cw = boto3.client("cloudwatch")
    namespace = os.environ.get("METRIC_NAMESPACE", "GooseTown")
    name = os.environ.get("METRIC_NAME", "PixelLabCreditsRemaining")
    environment = os.environ.get("ENVIRONMENT", "unknown")
    cw.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": name,
                "Dimensions": [{"Name": "Environment", "Value": environment}],
                "Value": float(credits),
                "Unit": "Count",
            }
        ],
    )
    logger.info(
        "Emitted %s/%s = %d credits (Environment=%s)",
        namespace,
        name,
        credits,
        environment,
    )


def handler(_event: dict, _context: object) -> dict:
    """Lambda entrypoint — invoked by EventBridge every 15 minutes."""
    try:
        credits = _fetch_balance()
        _emit_metric(credits)
        return {"statusCode": 200, "credits_remaining": credits}
    except Exception as exc:
        logger.exception("PixelLab credit poll failed")
        # Emit a 0-credits datapoint so the alarm fires when the API itself
        # is broken — better to page on-call than to silently miss a real
        # drain. The exception is also re-raised so CloudWatch Logs flag it.
        try:
            _emit_metric(0)
        except Exception:
            logger.exception("Failed to emit fallback 0-credits datapoint")
        raise
