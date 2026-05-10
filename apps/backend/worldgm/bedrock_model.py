"""Bedrock language-model adapter for Concordia.

Concordia's LanguageModel is the abstraction GM components call to generate
narrative text.  We back it with Amazon Bedrock's ``converse`` API.  Two model
choices baked in:

  - Nova Lite  (narrative generation, ~$0.06/Mtok input)
  - Nova Micro (structured/JSON,     ~$0.035/Mtok input)

The Concordia contrib package ships ``AmazonBedrockLanguageModel`` but it does
not include Nova model IDs in its hard-coded allowlist and raises ``ValueError``
for any unknown model.  This adapter handles Nova explicitly and keeps the same
``converse`` call shape.

Deviation from plan: ``sample_choice`` signature matches the Concordia ABC
exactly — ``(prompt, responses, *, seed=None)`` with no ``**kwargs`` — because
the ABC declares keyword-only ``seed`` and abstract enforcement is strict in
Python 3.12.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from typing import Any, override

import boto3
from concordia.language_model import language_model

logger = logging.getLogger(__name__)

_DEFAULT_LITE = "amazon.nova-lite-v1:0"
_DEFAULT_MICRO = "amazon.nova-micro-v1:0"

# Nova models available via Bedrock converse API as of 2026-05.
# Token limits from the Bedrock docs (max output tokens per request).
_NOVA_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "amazon.nova-micro-v1:0": 5120,
    "amazon.nova-lite-v1:0": 5120,
    "amazon.nova-pro-v1:0": 5120,
}


class BedrockLanguageModel(language_model.LanguageModel):
    """Concordia LanguageModel adapter that routes to Amazon Bedrock.

    Supports Amazon Nova models (Micro / Lite / Pro) via the Bedrock ``converse``
    API.  AWS credentials are resolved from the standard boto3 credential chain
    (IAM role in ECS; ``~/.aws/credentials`` locally).
    """

    def __init__(self, model_id: str = _DEFAULT_LITE, region: str = "us-east-1"):
        self._client = boto3.client("bedrock-runtime", region_name=region)
        self._model_id = model_id
        self._max_tokens_limit = _NOVA_MAX_OUTPUT_TOKENS.get(model_id, language_model.DEFAULT_MAX_TOKENS)
        logger.info(
            "BedrockLanguageModel initialized: model_id=%s region=%s max_out=%d",
            model_id,
            region,
            self._max_tokens_limit,
        )

    @override
    def sample_text(
        self,
        prompt: str,
        *,
        max_tokens: int = language_model.DEFAULT_MAX_TOKENS,
        terminators: Collection[str] = language_model.DEFAULT_TERMINATORS,
        temperature: float = language_model.DEFAULT_TEMPERATURE,
        top_p: float = language_model.DEFAULT_TOP_P,
        top_k: int = language_model.DEFAULT_TOP_K,
        timeout: float = language_model.DEFAULT_TIMEOUT_SECONDS,
        seed: int | None = None,
    ) -> str:
        del timeout, seed, top_k  # Unused — Bedrock converse does not expose these

        capped = min(self._max_tokens_limit, max_tokens)

        inference_config: dict[str, Any] = {
            "maxTokens": capped,
            "temperature": temperature,
            "topP": top_p,
        }

        # Bedrock rejects an empty stopSequences list; omit when unused.
        stop_seqs = [t for t in terminators if t and not t.isspace()]
        if stop_seqs:
            inference_config["stopSequences"] = stop_seqs

        resp = self._client.converse(
            modelId=self._model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig=inference_config,
        )
        return resp["output"]["message"]["content"][0]["text"]

    @override
    def sample_choice(
        self,
        prompt: str,
        responses: Sequence[str],
        *,
        seed: int | None = None,
    ) -> tuple[int, str, dict[str, Any]]:
        """Multiple-choice via structured prompt.

        Instructs the model to reply with exactly one of the candidate strings,
        then string-matches the reply.  Falls back to the first candidate if no
        match is found (avoids ``InvalidResponseError`` for GM use cases where
        a graceful default beats a hard crash).
        """
        del seed  # Unused

        choice_prompt = prompt + "\nRespond EXACTLY with one of the following strings:\n" + "\n".join(responses) + "."
        sample = self.sample_text(choice_prompt, max_tokens=64, temperature=0.0)
        for i, r in enumerate(responses):
            if r.lower() in sample.lower():
                return i, r, {}
        logger.warning("sample_choice: no match in %r for responses=%r; defaulting to index 0", sample, responses)
        return 0, responses[0], {}
