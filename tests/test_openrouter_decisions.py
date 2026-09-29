import json
from dataclasses import replace

import httpx
import pytest

from gepa_engine.config import Settings, make_connection, save_settings, load_settings
from gepa_engine.jobs import JobService
from gepa_engine.providers import ModelGateway, ProviderError
from gepa_engine.errors import JobError


POLICY = {"question": "intent", "type": "choice", "instructions": "Clasifica la intención.", "criteria": {"alarm": "Alarma", "calendar": "Calendario"}}
CASES = [{"id": f"{split}-{label}", "split": split, "input": f"{split} {label}", "expected": label}
         for split in ("train", "val", "test") for label in POLICY["criteria"]]


def fixture(tmp_path, protocol="decisions", answer=None, improve=False):
    decider = make_connection(identifier="decider", provider="openrouter", model="fixture/decision", protocol=protocol, api_key_env="FIXTURE_KEY")
    reflection = make_connection(identifier="reflection", provider="openrouter", model="fixture/chat", api_key_env="FIXTURE_KEY")
    settings = Settings(home=tmp_path, data_dir=tmp_path / "data", connections=(decider, reflection), roles={"executor": "decider", "reflection": "reflection"})
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((str(request.url), body))
        assert request.headers["Authorization"] == "Bearer synthetic"
        if str(request.url).endswith("/alpha/decisions"):
            assert set(body) == {"model", "state", "questions"}
            question = body["questions"]["intent"]
            assert set(question) == {"type", "instructions", "criteria"}
            assert question["type"] == "choice"
            assert question["criteria"] == POLICY["criteria"]
            value = body["state"].split()[-1]
            if improve and question["instructions"] == POLICY["instructions"]:
                value = "calendar" if value == "alarm" else "alarm"
            return httpx.Response(200, content=json.dumps({"answers": {"intent": answer or {"type": "choice", "choice": value, "confidence": .9, "probabilities": {"alarm": .5, "calendar": .5}}}, "usage": {"input_tokens": 5, "output_tokens": 1, "cost": .001}}))
        assert str(request.url).endswith("/v1/chat/completions")
        if body["model"] == "fixture/chat":
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"instructions": "Mejorado: clasifica la intención.", "criteria": POLICY["criteria"]})}, "finish_reason": "stop"}]})
        assert body["model"] == "fixture/decision"
        value = json.loads(body["messages"][1]["content"])["state"].split()[-1]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"choice": value})}, "finish_reason": "stop"}]})

    gateway = ModelGateway({"decider": "synthetic", "reflection": "synthetic"}, client=httpx.Client(transport=httpx.MockTransport(handler)))
    service = JobService(settings, gateway=gateway, environ={"FIXTURE_KEY": "synthetic"})
    draft = service.prepare({"artifact": {"type": "jev-policy", "policy": POLICY}, "objective": "Elegir la intención correcta.", "inputs": [{"cases": CASES, "origin": "synthetic"}]})
    return service, draft, calls


@pytest.mark.parametrize("protocol", ["chat", "decisions"])
def test_real_preview_and_frozen_run(tmp_path, protocol):
    service, draft, calls = fixture(tmp_path, protocol)
    preview = service.preview(draft["draftId"], sample=4)
    assert preview["status"] == "complete"
    assert preview["score"]["score"] == 1
    assert len(calls) == 4
    if protocol == "decisions":
        assert all(url == "https://openrouter.ai/api/alpha/decisions" for url, _ in calls)
        assert preview["cases"][0]["confidence"] == .9
        assert preview["cases"][0]["usage"] == {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
    approved = service.approve(draft["draftId"])
    spec = {"artifact": {"type": "jev-policy", "policy": POLICY}, "dataset": approved["datasetId"], "limits": {"validationScoreTarget": 1}}
    created = service.create(spec)
    assert service.store.get("job", created["jobId"])["manifest"]["models"]["decider"]["protocol"] == protocol
    result = service.run(created["jobId"])
    assert result["status"] == "completed", result


@pytest.mark.parametrize("answer", [{"type": "choice", "choice": "unknown"}, {"type": "choice", "choice": "alarm", "confidence": float("nan")}, {"type": "choice", "choice": "alarm", "probabilities": {"alarm": -1}}])
def test_malformed_native_answer_fails_safely(tmp_path, answer):
    service, draft, calls = fixture(tmp_path, answer=answer)
    preview = service.preview(draft["draftId"], sample=4)
    assert preview["status"] == "failed"
    assert preview["error"]["code"] == "invalid-response"
    assert len(calls) == 1


def test_protocol_roundtrip_and_role_guard(tmp_path):
    service, draft, calls = fixture(tmp_path)
    save_settings(service.settings)
    assert load_settings(tmp_path).connection("decider").protocol == "decisions"
    service.settings = replace(service.settings, roles={"executor": "decider", "reflection": "decider"})
    with pytest.raises(JobError, match="decisions"):
        service._models({}, roles={"reflection": "reflection"})
    assert calls == []


def test_local_decisions_rejected():
    with pytest.raises(Exception, match="OpenRouter"):
        make_connection(identifier="a", provider="local", model="m", url="http://localhost/v1", protocol="decisions")


def test_native_decider_with_actual_gepa_chat_reflection(tmp_path):
    service, draft, calls = fixture(tmp_path, improve=True)
    assert service.preview(draft["draftId"], sample=4)["status"] == "complete"
    approved = service.approve(draft["draftId"])
    result = service.start({"artifact": {"type": "jev-policy", "policy": POLICY}, "dataset": approved["datasetId"], "limits": {"maxProposals": 1, "reflectionMinibatchSize": 2}})
    assert result["status"] == "completed", result
    assert any(body["model"] == "fixture/chat" for _, body in calls)
    assert all(url.endswith("/alpha/decisions") for url, body in calls if body["model"] == "fixture/decision")
    stored = service.store.get("job", result["jobId"])
    assert not stored["result"]["selection"]["selectedIsOriginal"]
    assert stored["result"]["finalCheck"]["status"] == "complete"
    assert stored["result"]["counts"]["finalEvaluations"] > 0


def test_doctor_uses_authenticated_decision_without_catalog(tmp_path, monkeypatch):
    from gepa_engine import doctor
    service, _, calls = fixture(tmp_path)
    monkeypatch.setattr(doctor, "ModelGateway", lambda *a, **kw: service.gateway)
    # The real doctor supplies a different tiny policy. Observe endpoint instead of using the dataset fixture assertions.
    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        assert request.headers["Authorization"] == "Bearer synthetic"
        assert set(body["questions"]) == {"setup_check"}
        return httpx.Response(200, json={"answers": {"setup_check": {"type": "choice", "choice": "listo"}}})
    service.gateway.client = httpx.Client(transport=httpx.MockTransport(handler))
    probes = doctor.real_probes(service.settings, {"FIXTURE_KEY": "synthetic"})
    result = doctor._check_connection(service.settings.connection("decider"), probes, run_inference=True)
    assert result.status == "ok"
    assert "nativa autenticada" in result.message


def test_doctor_skips_unassigned_native_inference(tmp_path):
    from gepa_engine import doctor
    service, _, calls = fixture(tmp_path)
    def unexpected(*args):
        pytest.fail("Unused native connection must not make a request")
    probes = doctor.Probes(lambda: "0.1.4", lambda _: True, lambda _: None, unexpected, unexpected, lambda _: True, lambda: True)
    result = doctor._check_connection(service.settings.connection("decider"), probes, run_inference=False)
    assert result.code == "connection-not-tested"
    assert calls == []


def test_doctor_rejects_native_reflection_without_spending(tmp_path):
    from gepa_engine import doctor
    service, _, calls = fixture(tmp_path)
    settings = replace(service.settings, roles={"reflection": "decider"})
    def unexpected(*args):
        pytest.fail("Incompatible native role must not make a request")
    probes = doctor.Probes(lambda: "0.1.4", lambda _: True, lambda _: None, unexpected, unexpected, lambda _: True, lambda: True)
    # Restrict to the native connection: the chat connection would legitimately ask the catalog.
    settings = replace(settings, connections=(settings.connection("decider"),))
    report = doctor.run_doctor(settings, probes)
    assert not report["ok"]
    assert any(item["code"] == "protocol-incompatible" for item in report["findings"])
    assert calls == []


def test_cli_shared_key_captured_once_and_saved_atomically(tmp_path, monkeypatch):
    from io import StringIO
    from gepa_engine import cli
    from gepa_engine.providers import SecretStore
    service, _, _ = fixture(tmp_path)
    save_settings(service.settings)
    store = SecretStore(tmp_path / "shared-keys.json")
    monkeypatch.setattr(cli, "open_secret_store", lambda _: store)
    captures = []
    def capture(connection, save):
        captures.append(connection.id)
        save("synthetic")
        return True
    monkeypatch.setattr(cli, "capture_credential", capture)
    assert cli.main(["--home", str(tmp_path), "setup", "connection", "set-key", "decider", "--also", "reflection", "--ui"], stdout=StringIO(), stderr=StringIO()) == 0
    assert captures == ["decider"]
    assert store.get("decider") == store.get("reflection") == "synthetic"
    captures.clear()
    assert cli.main(["--home", str(tmp_path), "setup", "connection", "set-key", "decider", "--also", "missing", "--ui"], stdout=StringIO(), stderr=StringIO()) != 0
    assert captures == []


def test_secret_store_shared_save_failure_keeps_previous_state(tmp_path, monkeypatch):
    from gepa_engine.providers import SecretStore
    store = SecretStore(tmp_path / "keys.json")
    store.set("old", "synthetic-old")
    def fail(_):
        raise ProviderError("synthetic write failure")
    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(ProviderError):
        store.set_many(["a", "b"], "synthetic-new")
    assert store.get("old") == "synthetic-old"
    assert store.get("a") is None and store.get("b") is None
