from __future__ import annotations

from types import SimpleNamespace

import pytest

from gepa_engine import lifecycle
from gepa_engine.errors import JobError
from gepa_engine.jobs import JobService, _Run
from gepa_engine.review import build as build_review


class StubAdapter:
    roles = {"executor": "executor", "reflection": "reflection"}
    limit_defaults = {"executorMaxTokens": 64, "reflectionMaxTokens": 64}
    metric_label = "score"

    def seed(self):
        return {"instructions": "original"}

    def artifact(self, texts):
        return dict(texts)

    def run_case(self, texts, case, requests):
        return {"caseId": case["id"], "split": case["split"], "input": case["input"],
                "expected": case["expected"], "score": 1.0 if texts == self.seed() else 0.0,
                "submetrics": {}, "feedback": ""}


CASES = [
    {"id": "train-1", "split": "train", "input": "a", "expected": "x"},
    {"id": "val-1", "split": "val", "input": "b", "expected": "x"},
    {"id": "val-2", "split": "val", "input": "c", "expected": "x"},
    {"id": "test-1", "split": "test", "input": "d", "expected": "x"},
]


def make_run(tmp_path, *, target=None):
    adapter = StubAdapter()
    service = object.__new__(JobService)
    limits = service._limits({"maxProposals": 1, "validationScoreTarget": target}, {"train": 1, "val": 2, "test": 1}, adapter)
    now = lifecycle.now()
    job = {"id": "job-test", "name": "test", "status": "running", "phase": None, "createdAt": now, "updatedAt": now,
           "manifestSha256": "test", "manifest": {"limits": limits, "seed": 42,
           "artifact": {"type": "jev-policy", "source": None},
           "adapter": {"name": "stub", "version": "1", "sha256": "abc"},
           "evaluator": {"name": "stub", "version": "1", "primaryMetric": "score", "higherIsBetter": True},
           "dataset": {"id": "ds-test", "sha256": "abc", "approvedAt": now, "reviewedAllCases": True,
                       "splits": {"train": ["train-1"], "val": ["val-1", "val-2"], "test": ["test-1"]},
                       "counts": {"train": 1, "val": 2, "test": 1}},
           "models": {"executor": {"connection": "stub", "name": "stub", "provider": "local", "url": "http://localhost/v1", "model": "stub", "settingsRole": "executor"}},
           "fixedContract": {}, "mutableSurface": {}, "engine": {}, "objective": "test"},
           "attempts": [lifecycle.new_attempt(1, "run", now)], "events": [], "result": None, "error": None}
    run = _Run(job, adapter, CASES, lambda *a, **kw: {}, lambda: None, cancelled=lambda: None,
               record_spend=lambda _: None, folder=tmp_path / "gepa")
    return run


def test_target_stops_on_complete_baseline_and_preserves_test(tmp_path):
    run = make_run(tmp_path, target=0.8)

    def optimize(**kwargs):
        run.evaluate(run.rows["val"], run.adapter.seed())
        assert run.should_stop(SimpleNamespace(i=0, program_full_scores_val_set=[1.0]))

    run.optimize = optimize
    run.execute()

    assert run.job["status"] == "completed"
    assert run.result["searchStopReason"] == "validation_target"
    assert run.result["selection"]["selectedIsOriginal"] is True
    assert run.result["finalCheck"]["status"] == "skipped"
    assert run.result["counts"]["finalEvaluations"] == 0
    assert run.by_id[run.result["baselineId"]]["test"]["cases"] == []
    assert "final" not in run.attempt["phases"]
    assert lifecycle.consumption(run.job)["final"]["required"] == 0
    assert lifecycle.consumption(run.job)["final"]["resolved"] == 0

    report = build_review(run.job, CASES, [run.job])
    assert report["completeness"]["status"] == "complete"
    assert report["completeness"]["finalCheck"] == "skipped"
    assert report["recommendation"]["reason"] == "original-selected"
    assert report["testObservedBefore"]["observed"] is False
    assert report["comparison"]["test"]["notEvaluated"] == [run.result["baselineId"]]


def test_rejected_iteration_uses_proposal_limit_but_not_reserved_test(tmp_path):
    run = make_run(tmp_path)

    def optimize(**kwargs):
        run.evaluate(run.rows["val"], run.adapter.seed())
        child = run.register({"instructions": "unvalidated"}, origin="reflection", parent=run.result["baselineId"])
        child["searchCases"].append({"caseId": "train-1", "split": "train", "input": "a", "score": 0.0})
        run.result["proposals"].append({"iteration": 1, "status": "valid", "candidateId": child["id"]})
        run.result["counts"]["iterations"] = 1
        assert run.should_stop(SimpleNamespace(i=0, program_full_scores_val_set=[1.0]))

    run.optimize = optimize
    run.execute()

    assert run.result["searchStopReason"] == "max_proposals"
    assert run.result["selection"]["selectedIsOriginal"]
    assert run.result["finalCheck"]["status"] == "skipped"
    assert run.result["counts"]["finalEvaluations"] == 0


def test_final_retry_without_improvement_also_preserves_test(tmp_path):
    run = make_run(tmp_path)
    original = run.by_id[run.result["baselineId"]]
    original["validation"].update(status="complete", score=1.0, correct=2,
                                  cases=[{"caseId": case["id"], "score": 1.0} for case in run.rows["val"]])
    run.attempt["kind"] = "final-retry"
    run.execute()

    assert run.job["status"] == "completed"
    assert run.result["finalCheck"]["status"] == "skipped"
    assert run.result["counts"]["finalEvaluations"] == 0


def test_skipped_test_does_not_allow_export(tmp_path):
    run = make_run(tmp_path)
    original = run.by_id[run.result["baselineId"]]
    original["validation"].update(status="complete", score=1.0, correct=2,
                                  cases=[{"caseId": case["id"], "score": 1.0} for case in run.rows["val"]])
    run.freeze_selection()
    run.skip_final_check()
    service = object.__new__(JobService)
    service._job = lambda _: run.job
    with pytest.raises(JobError, match="prueba reservada"):
        service.export(run.job["id"], tmp_path / "export")


def test_validated_improvement_still_opens_reserved_test(tmp_path):
    run = make_run(tmp_path, target=0.8)

    def optimize(**kwargs):
        run.evaluate(run.rows["val"], run.adapter.seed())
        original = run.by_id[run.result["baselineId"]]
        original["validation"].update(score=0.5, correct=1)
        child = run.register({"instructions": "better"}, origin="reflection", parent=original["id"])
        child["validation"].update(status="complete", score=0.9, correct=1.8,
                                   cases=[{"caseId": case["id"], "score": 0.9} for case in run.rows["val"]])
        assert run.should_stop(SimpleNamespace(i=0, program_full_scores_val_set=[0.5, 0.9]))

    run.optimize = optimize
    run.execute()

    assert run.result["searchStopReason"] == "validation_target"
    assert run.result["selection"]["selectedIsOriginal"] is False
    assert run.result["finalCheck"]["status"] == "complete"
    assert run.result["counts"]["finalEvaluations"] == 2
    assert {case["caseId"] for candidate in run.result["candidates"] for case in candidate["test"]["cases"]} == {"test-1"}


@pytest.mark.parametrize("bad", [-0.01, 1.01, "80%", True, float("nan")])
def test_validation_score_target_rejects_invalid_values(bad):
    service = object.__new__(JobService)
    with pytest.raises(JobError, match="validationScoreTarget"):
        service._limits({"validationScoreTarget": bad}, {"train": 1, "val": 2, "test": 1}, StubAdapter())


def test_default_target_is_optional():
    service = object.__new__(JobService)
    limits = service._limits({}, {"train": 1, "val": 2, "test": 1}, StubAdapter())
    assert limits["validationScoreTarget"] is None
