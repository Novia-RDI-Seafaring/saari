"""Tests for status.build_stages: derived SLR stage states."""

from __future__ import annotations

from saari.status import build_stages


def _funnel(searches=None, by_status=None, n_unique=0):
    return {
        "searches": searches or [],
        "n_unique": n_unique,
        "by_status": by_status or {},
    }


def _stages(**kw):
    defaults = dict(
        study={"question": "", "criteria": ""},
        funnel=_funnel(),
        n_snowball_events=0,
        last_snowball_new=None,
        bundle_exists=False,
        write_slots=None,
    )
    defaults.update(kw)
    return build_stages(**defaults)


def by_key(result):
    return {s["key"]: s for s in result["stages"]}


def test_fresh_project_everything_todo_next_is_protocol():
    r = _stages()
    assert [s["state"] for s in r["stages"]] == ["todo"] * 5
    assert r["next"] == "protocol"


def test_protocol_partial_is_active():
    r = _stages(study={"question": "RQ?", "criteria": ""})
    assert by_key(r)["protocol"]["state"] == "active"
    assert "criteria" in by_key(r)["protocol"]["detail"]
    assert r["next"] == "protocol"


def test_protocol_done_moves_next_to_search():
    r = _stages(study={"question": "RQ?", "criteria": "include X; exclude Y"})
    assert by_key(r)["protocol"]["state"] == "done"
    assert r["next"] == "search"


def test_search_needs_multiple_phrasings():
    one = _funnel(searches=[{"source": "openalex", "n_returned": 25}])
    r = _stages(funnel=one)
    assert by_key(r)["search"]["state"] == "active"
    three = _funnel(
        searches=[{"source": "openalex", "n_returned": 25}] * 3, n_unique=60
    )
    r = _stages(funnel=three)
    assert by_key(r)["search"]["state"] == "done"


def test_snowball_events_do_not_count_as_searches():
    f = _funnel(searches=[{"source": "snowball", "n_returned": 30}] * 3)
    r = _stages(funnel=f)
    assert by_key(r)["search"]["state"] == "todo"


def test_screen_states():
    f = _funnel(
        n_unique=100,
        by_status={"included": 10, "excluded": 20, "maybe": 5, "candidate": 65},
    )
    s = by_key(_stages(funnel=f))["screen"]
    assert s["state"] == "active"
    assert "35/100" in s["detail"]
    f_done = _funnel(n_unique=30, by_status={"included": 10, "excluded": 20})
    assert by_key(_stages(funnel=f_done))["screen"]["state"] == "done"


def test_maybes_block_screen_done():
    f = _funnel(n_unique=30, by_status={"included": 10, "excluded": 19, "maybe": 1})
    assert by_key(_stages(funnel=f))["screen"]["state"] == "active"


def test_snowball_saturation():
    assert by_key(_stages())["snowball"]["state"] == "todo"
    active = _stages(n_snowball_events=2, last_snowball_new=17)
    assert by_key(active)["snowball"]["state"] == "active"
    assert "17" in by_key(active)["snowball"]["detail"]
    done = _stages(n_snowball_events=3, last_snowball_new=0)
    assert by_key(done)["snowball"]["state"] == "done"


def test_report_states():
    assert by_key(_stages())["report"]["state"] == "todo"
    drafting = _stages(bundle_exists=True, write_slots=38)
    assert by_key(drafting)["report"]["state"] == "active"
    assert "38" in by_key(drafting)["report"]["detail"]
    finished = _stages(bundle_exists=True, write_slots=0)
    assert by_key(finished)["report"]["state"] == "done"


def test_next_is_none_when_all_done():
    r = _stages(
        study={"question": "RQ?", "criteria": "c"},
        funnel=_funnel(
            searches=[{"source": "openalex", "n_returned": 25}] * 3,
            n_unique=60,
            by_status={"included": 20, "excluded": 40},
        ),
        n_snowball_events=2,
        last_snowball_new=0,
        bundle_exists=True,
        write_slots=0,
    )
    assert [s["state"] for s in r["stages"]] == ["done"] * 5
    assert r["next"] is None
