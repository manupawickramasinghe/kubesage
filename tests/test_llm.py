"""The live client's handling of odd provider replies, with the HTTP call replaced by canned responses."""
import pytest
from pydantic import BaseModel

from kubesage.config import load_models
from kubesage.llm.client import LLMClient, LLMError


class Answer(BaseModel):
    verdict: str


def reply(content=None, finish="stop", tokens=10):
    message = {"role": "assistant", "reasoning": "thinking…"} | ({"content": content} if content is not None else {})
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 5, "completion_tokens": tokens}}


def client(monkeypatch, replies):
    models = load_models("cerebras")
    monkeypatch.setenv(models.provider.api_key_env, "test-key")
    llm = LLMClient(models, "live")
    sent = []

    def post(body):
        sent.append(body)
        return replies.pop(0)
    monkeypatch.setattr(llm, "_post", post)
    return llm, sent


def call(llm):
    return llm.structured("strategist", "system", "user", Answer, lambda: Answer(verdict="mock"), stage="test", workload="w")


def test_reply_without_content_is_retried_with_less_reasoning(monkeypatch):
    # Seen live: gpt-oss-120b at high effort reasoned until max_tokens and the message had no "content" key.
    llm, sent = client(monkeypatch, [reply(finish="length", tokens=16000), reply('{"verdict": "ok"}')])
    result, entry = call(llm)
    assert result.verdict == "ok"
    assert [b["reasoning_effort"] for b in sent] == ["high", "medium"]
    assert entry.output_tokens == 16010  # the wasted attempt is still billed


def test_two_empty_replies_raise_an_llm_error_naming_the_cause(monkeypatch):
    llm, _ = client(monkeypatch, [reply(finish="length"), reply("")])
    with pytest.raises(LLMError, match=r"no answer \(finish_reason 'stop'"):
        call(llm)
    # both replies stay in the trace, so the run's trace.json shows what the provider sent
    assert [(x["attempt"], x["response"]["body"]["choices"][0]["finish_reason"]) for x in llm.trace] == [(1, "length"), (2, "stop")]


def test_invalid_json_is_fed_back_once(monkeypatch):
    llm, sent = client(monkeypatch, [reply('{"wrong": 1}'), reply('{"verdict": "fixed"}')])
    assert call(llm)[0].verdict == "fixed"
    assert "failed validation" in sent[1]["messages"][-1]["content"]
    assert sent[1]["reasoning_effort"] == "high"


def test_a_refusal_is_named_as_one(monkeypatch):
    refusal = {"choices": [{"message": {"role": "assistant", "content": None, "refusal": "I can't help with that."},
                            "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    llm, _ = client(monkeypatch, [refusal, refusal])
    with pytest.raises(LLMError, match="refused: I can't help with that"):
        call(llm)


@pytest.mark.parametrize("envelope", [{"choices": [None]}, {"choices": "x"}, ["not", "a", "dict"], {"choices": []},
                                      {"choices": [{"message": "text"}]}, {"choices": [{"message": {"content": 7}}]}])
def test_a_malformed_envelope_is_a_classified_failure(monkeypatch, envelope):
    # Review F05: choices [null] or a non-mapping message raised AttributeError instead of an LLMError.
    llm, _ = client(monkeypatch, [envelope, envelope])
    with pytest.raises(LLMError) as err:
        call(llm)
    assert len(err.value.exchanges) == 2  # what the provider sent stays in the trace


def test_content_parts_are_read_as_text(monkeypatch):
    parts = {"choices": [{"message": {"content": [{"type": "text", "text": '{"verdict": '}, {"type": "text", "text": '"ok"}'}]},
                          "finish_reason": "stop"}], "usage": {"prompt_tokens": "5", "completion_tokens": 3}}
    llm, _ = client(monkeypatch, [parts])
    result, entry = call(llm)
    assert result.verdict == "ok" and entry.input_tokens == 0 and entry.output_tokens == 3
