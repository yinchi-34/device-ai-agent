import math

import pytest

from evals.rag import annotation, difficulty, general_track, power, stages

# --- power / comparability ---------------------------------------------------


def test_se_and_ci_match_closed_form():
    xs = [1, 0, 1, 1, 0, 1, 0, 1]
    mu = 5 / 8
    expected = math.sqrt(sum((x - mu) ** 2 for x in xs) / 7 / 8)
    assert power.se_mean(xs) == pytest.approx(expected)
    lo, hi = power.ci95(xs)
    assert hi - lo == pytest.approx(2 * 1.96 * expected)


def test_clustered_se_equals_iid_for_singleton_clusters_and_grows_with_correlation():
    xs = [1, 1, 0, 0, 1, 1, 0, 0]
    assert power.clustered_se(xs, list("abcdefgh")) == pytest.approx(power.se_mean(xs))
    assert power.clustered_se(xs, list("aabbccdd")) > power.se_mean(xs)


def test_required_n_reproduces_miller_example():
    # Miller (2024) Eq. 9 example: omega^2 = 1/9, delta = 0.03, alpha .05, power .8 -> ~969 questions
    assert power.required_n_paired(math.sqrt(1 / 9), 0.03) in range(968, 972)


def test_mde_is_inverse_of_required_n():
    n = power.required_n_paired(0.4, 0.05)
    assert power.mde_paired(0.4, n) <= 0.05 + 1e-9


def test_proportion_sample_size_classic_values():
    assert power.required_n_proportion(0.05) == 385
    assert power.required_n_proportion(0.10) == 97


# --- difficulty --------------------------------------------------------------


def test_difficulty_levels_and_composite():
    assert [difficulty.hop_level(h) for h in (1, 2, 3, 5)] == [0, 1, 2, 2]
    assert [difficulty.gap_level(o) for o in (0.9, 0.6, 0.45, 0.1)] == [0, 0, 1, 2]
    assert [difficulty.empirical_level(p) for p in (1.0, 2 / 3, 1 / 3, 0.0)] == [0, 1, 1, 2]
    assert difficulty.composite([0, 1, 0]) == "easy"
    assert difficulty.composite([1, 1, 1]) == "medium"
    assert difficulty.composite([2, 1, 1]) == "hard"


def test_overlap_is_query_term_coverage():
    assert difficulty.overlap("40013 过滤器压差", "40013 | 过滤器压差 | UINT16") == 1.0
    assert difficulty.overlap("房间越来越热", "40013 | 过滤器压差") == 0.0


def test_conflicting_indicators_trigger_review():
    lab = difficulty._label({"id": "x", "category": "c"}, [0, 0, 2], 0.9, 0.0, 1, 3)
    assert "indicators_disagree" in lab["review_reasons"]
    lab = difficulty._label({"id": "y", "category": "c"}, [0, 1, 1], 0.5, 0.5, 1, 3)
    assert lab["review_reasons"] == []


# --- annotation agreement ------------------------------------------------------


def test_cohen_kappa_perfect_chance_and_weighted():
    assert annotation.cohen_kappa(list("YNYN"), list("YNYN")) == pytest.approx(1.0)
    assert annotation.cohen_kappa(list("YYNN"), list("YNYN")) == pytest.approx(0.0)
    a = ["easy", "medium", "hard", "hard"]
    near = ["easy", "medium", "medium", "hard"]
    far = ["hard", "medium", "easy", "hard"]
    w = annotation.ORDINAL
    assert annotation.cohen_kappa(a, near, weights=w) > annotation.cohen_kappa(a, far, weights=w)


# --- general track -----------------------------------------------------------------

FAKE_CRUD = {
    "questanswer_1doc": [{"ID": "e1", "news1": "天津港今年吞吐量增长百分之五", "questions": "天津港吞吐量增长多少", "answers": "5%"}],
    "questanswer_2docs": [{"ID": "e2", "news1": "某市发布暴雨预警", "news2": "某市地铁临时停运",
                           "questions": "暴雨预警后地铁如何运营", "answers": "停运"}],
    "questanswer_3docs": [{"ID": "e3", "news1": "甲公司发布新车", "news2": "新车续航六百公里", "news3": "新车售价二十万",
                           "questions": "甲公司新车续航和售价", "answers": "600km 20万"}],
}


def test_general_track_build_and_evaluate():
    passages, cases = general_track.build_general_track(FAKE_CRUD, per_level=5)
    assert len(passages) == 6 and [len(c["gold_passages"]) for c in cases] == [1, 2, 3]
    res = general_track.evaluate_general(passages, cases, {"bm25": general_track.bm25_ranker(passages)})
    assert res["bm25"][0]["hit@1"] == 1.0


# --- stage sets -------------------------------------------------------------------


def test_key_facts_and_generation_scoring():
    assert {"40002", "0.1"} <= set(stages.key_facts("40002 送风温度缩放系数 0.1。"))
    gen = [{"id": "a", "must_refuse": False, "key_facts": ["40002", "0.1"]},
           {"id": "b", "must_refuse": True, "key_facts": []}]
    s = stages.score_generation(gen, {"a": "地址 40002，系数 0.1", "b": "手册中没有相关信息"})
    assert s["key_fact_recall"] == 1.0 and s["refusal_accuracy"] == 1.0 and s["false_refusal_rate"] == 0.0
