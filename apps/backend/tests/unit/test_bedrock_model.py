"""Tests for BedrockLanguageModel adapter (worldgm package).

Import path: ``worldgm.bedrock_model`` — pytest runs from ``apps/backend/``
where ``worldgm/`` is a package on the path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_converse_response(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}}


# ---------------------------------------------------------------------------
# sample_text
# ---------------------------------------------------------------------------


@patch("worldgm.bedrock_model.boto3.client")
def test_sample_text_returns_first_content_block(mock_boto):
    from worldgm.bedrock_model import BedrockLanguageModel

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_converse_response("hello world")
    mock_boto.return_value = mock_client

    model = BedrockLanguageModel()
    result = model.sample_text("test prompt")
    assert result == "hello world"

    call_kwargs = mock_client.converse.call_args.kwargs
    assert call_kwargs["modelId"] == "amazon.nova-lite-v1:0"
    assert call_kwargs["messages"][0]["role"] == "user"
    assert call_kwargs["messages"][0]["content"][0]["text"] == "test prompt"


@patch("worldgm.bedrock_model.boto3.client")
def test_sample_text_passes_inference_config(mock_boto):
    from worldgm.bedrock_model import BedrockLanguageModel

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_converse_response("x")
    mock_boto.return_value = mock_client

    model = BedrockLanguageModel()
    model.sample_text("p", max_tokens=512, temperature=0.3, terminators=["END"])

    config = mock_client.converse.call_args.kwargs["inferenceConfig"]
    assert config["maxTokens"] == 512
    assert config["temperature"] == 0.3
    assert config["stopSequences"] == ["END"]


@patch("worldgm.bedrock_model.boto3.client")
def test_sample_text_omits_empty_terminators(mock_boto):
    """Empty / whitespace terminators must not be passed to Bedrock (it rejects them)."""
    from worldgm.bedrock_model import BedrockLanguageModel

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_converse_response("x")
    mock_boto.return_value = mock_client

    model = BedrockLanguageModel()
    model.sample_text("p", terminators=[])

    config = mock_client.converse.call_args.kwargs["inferenceConfig"]
    assert "stopSequences" not in config


# ---------------------------------------------------------------------------
# sample_choice
# ---------------------------------------------------------------------------


@patch("worldgm.bedrock_model.boto3.client")
def test_sample_choice_finds_match_in_response(mock_boto):
    from worldgm.bedrock_model import BedrockLanguageModel

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_converse_response("I think yes is correct")
    mock_boto.return_value = mock_client

    model = BedrockLanguageModel()
    idx, response, info = model.sample_choice("question", ["yes", "no"])
    assert idx == 0
    assert response == "yes"
    assert isinstance(info, dict)


@patch("worldgm.bedrock_model.boto3.client")
def test_sample_choice_falls_back_to_first_when_no_match(mock_boto):
    from worldgm.bedrock_model import BedrockLanguageModel

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_converse_response("completely unrelated answer")
    mock_boto.return_value = mock_client

    model = BedrockLanguageModel()
    idx, response, info = model.sample_choice("question", ["yes", "no"])
    assert idx == 0
    assert response == "yes"


# ---------------------------------------------------------------------------
# Constructor / model_id
# ---------------------------------------------------------------------------


@patch("worldgm.bedrock_model.boto3.client")
def test_uses_custom_model_id(mock_boto):
    from worldgm.bedrock_model import BedrockLanguageModel

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_converse_response("x")
    mock_boto.return_value = mock_client

    model = BedrockLanguageModel(model_id="amazon.nova-micro-v1:0")
    model.sample_text("p")
    assert mock_client.converse.call_args.kwargs["modelId"] == "amazon.nova-micro-v1:0"
