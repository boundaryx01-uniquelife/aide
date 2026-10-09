import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from aide import llm, netutil
from aide.config import Config, ConfigError, load_config
from aide.models import Finding

NOW = datetime(2026, 10, 9, 9, 0)


def finding(key, source="mail", title="제목", account="", detail="", priority=1):
    return Finding(key=key, source=source, title=title, detail=detail, priority=priority, account=account)


def ok_response(labels):
    """A valid Anthropic response body for the given [(i, p), ...] pairs."""
    text = json.dumps({"items": [{"i": i, "p": p} for i, p in labels]})
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}


class EligibleTests(unittest.TestCase):
    def test_source_and_account_filter(self):
        findings = [
            finding("1", source="mail", account="개인"),
            finding("2", source="mail", account="학교"),
            finding("3", source="git"),
            finding("4", source="notice"),
        ]
        cfg = Config(llm_sources=["mail", "notice"], llm_exclude_accounts=["학교"])
        self.assertEqual(llm._eligible(findings, cfg), [0, 3])


class BuildRequestTests(unittest.TestCase):
    def test_shape_and_schema_and_profile(self):
        findings = [finding("1", title="보낸이: 제목", detail="상세")]
        cfg = Config(llm_model="claude-haiku-5-5", llm_profile="발명교육센터 교사")
        body = llm._build_request(findings, [0], cfg)
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertEqual(body["output_config"]["format"]["schema"], llm.SCHEMA)
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertIn("발명교육센터 교사", body["system"])
        content = body["messages"][0]["content"]
        self.assertIn("데이터", content)
        items = json.loads(content[content.index("["):])
        self.assertEqual(items, [{"i": 0, "source": "mail", "text": "보낸이: 제목", "detail": "상세"}])

    def test_long_title_is_cleaned_and_capped(self):
        findings = [finding("1", title="x" * 300)]
        cfg = Config()
        body = llm._build_request(findings, [0], cfg)
        content = body["messages"][0]["content"]
        items = json.loads(content[content.index("["):])
        self.assertLessEqual(len(items[0]["text"]), 120)

    def test_no_profile_omits_the_section(self):
        body = llm._build_request([finding("1")], [0], Config(llm_profile=""))
        self.assertEqual(body["system"], llm.SYSTEM_PREFIX)


class ParseResponseTests(unittest.TestCase):
    def test_valid_response(self):
        data = ok_response([(0, "high"), (1, "low")])
        self.assertEqual(llm._parse_response(data, 2), {0: "high", 1: "low"})

    def test_wrong_stop_reason_rejected(self):
        data = ok_response([(0, "high")])
        data["stop_reason"] = "max_tokens"
        self.assertIsNone(llm._parse_response(data, 1))

    def test_refusal_rejected(self):
        data = {"stop_reason": "refusal", "stop_details": {"type": "refusal"}}
        self.assertIsNone(llm._parse_response(data, 1))

    def test_wrong_count_rejected(self):
        self.assertIsNone(llm._parse_response(ok_response([(0, "high")]), 2))

    def test_duplicate_index_rejected(self):
        self.assertIsNone(llm._parse_response(ok_response([(0, "high"), (0, "low")]), 2))

    def test_out_of_range_index_rejected(self):
        self.assertIsNone(llm._parse_response(ok_response([(0, "high"), (5, "low")]), 2))

    def test_unknown_priority_value_rejected(self):
        self.assertIsNone(llm._parse_response(ok_response([(0, "urgent")]), 1))

    def test_bool_as_index_rejected(self):
        """json.loads can hand back True/False where an int is expected; bool is an int
        subclass in Python so this must be checked explicitly."""
        data = ok_response([(0, "high")])
        data["content"][0]["text"] = json.dumps({"items": [{"i": True, "p": "high"}]})
        self.assertIsNone(llm._parse_response(data, 1))

    def test_non_json_text_rejected(self):
        data = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]}
        self.assertIsNone(llm._parse_response(data, 1))

    def test_no_text_block_rejected(self):
        data = {"stop_reason": "end_turn", "content": [{"type": "tool_use"}]}
        self.assertIsNone(llm._parse_response(data, 1))

    def test_garbage_shapes_never_raise(self):
        for bad in [None, "x", 3, {}, {"content": None}, {"content": "x"}]:
            self.assertIsNone(llm._parse_response(bad, 1))


class RankTests(unittest.TestCase):
    def cfg(self, **kw):
        return Config(llm_enabled=True, **kw)

    def test_disabled_returns_none_without_calling_post(self):
        def boom(*a, **k):
            raise AssertionError("must not call the network")
        out = llm.rank([finding("1")], Config(llm_enabled=False), now=NOW, api_key="k", post=boom)
        self.assertIsNone(out)

    def test_empty_findings_returns_none(self):
        out = llm.rank([], self.cfg(), now=NOW, api_key="k", post=lambda *a, **k: {})
        self.assertIsNone(out)

    def test_no_api_key_returns_none_without_calling_post(self):
        def boom(*a, **k):
            raise AssertionError("must not call the network")
        out = llm.rank([finding("1")], self.cfg(), now=NOW, api_key="", post=boom)
        self.assertIsNone(out)

    def test_over_daily_budget_returns_none_without_calling_post(self):
        def boom(*a, **k):
            raise AssertionError("must not call the network")
        cfg = self.cfg(llm_max_calls_per_day=5)
        out = llm.rank([finding("1")], cfg, now=NOW, calls_today=5, api_key="k", post=boom)
        self.assertIsNone(out)

    def test_nothing_eligible_returns_all_normal_without_calling_post(self):
        def boom(*a, **k):
            raise AssertionError("must not call the network")
        cfg = self.cfg(llm_sources=["mail"])
        findings = [finding("1", source="git"), finding("2", source="git")]
        out = llm.rank(findings, cfg, now=NOW, api_key="k", post=boom)
        self.assertEqual(out, ["normal", "normal"])

    def test_successful_call_maps_labels_back_by_position(self):
        findings = [finding("1", source="mail"), finding("2", source="git"), finding("3", source="notice")]
        cfg = self.cfg(llm_sources=["mail", "notice"])
        calls = []

        def fake_post(url, payload, headers=None, timeout=None):
            calls.append((url, payload, headers))
            return ok_response([(0, "high"), (1, "low")])

        out = llm.rank(findings, cfg, now=NOW, api_key="k", post=fake_post)
        self.assertEqual(out, ["high", "normal", "low"])  # git (index 1) forced normal, never sent
        url, payload, headers = calls[0]
        self.assertEqual(url, llm.API_URL)
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(headers["anthropic-version"], llm.API_VERSION)
        content = payload["messages"][0]["content"]
        sent_items = json.loads(content[content.index("["):])
        self.assertEqual([it["source"] for it in sent_items], ["mail", "notice"])

    def test_network_error_returns_none_and_does_not_set_call_status(self):
        def fail(*a, **k):
            raise netutil.NetError("연결 실패")
        status = {}
        out = llm.rank([finding("1")], self.cfg(), now=NOW, api_key="k", post=fail, call_status=status)
        self.assertIsNone(out)
        self.assertNotIn("attempted", status)

    def test_successful_call_sets_call_status_even_on_validation_failure(self):
        status = {}
        out = llm.rank([finding("1")], self.cfg(), now=NOW, api_key="k",
                        post=lambda *a, **k: {"stop_reason": "max_tokens"}, call_status=status)
        self.assertIsNone(out)
        self.assertTrue(status.get("attempted"))

    def test_more_than_max_items_truncated(self):
        findings = [finding(str(i)) for i in range(40)]
        seen_counts = []

        def fake_post(url, payload, headers=None, timeout=None):
            content = payload["messages"][0]["content"]
            items = json.loads(content[content.index("["):])
            seen_counts.append(len(items))
            return ok_response([(it["i"], "normal") for it in items])

        llm.rank(findings, self.cfg(), now=NOW, api_key="k", post=fake_post)
        self.assertEqual(seen_counts, [llm.MAX_ITEMS])

    def test_no_url_or_key_leaks_into_request_body(self):
        f = finding("1", title="보낸이: 제목", detail="https://mail.google.com/secret")
        body_text = {}

        def fake_post(url, payload, headers=None, timeout=None):
            body_text["v"] = json.dumps(payload, ensure_ascii=False)
            return ok_response([(0, "normal")])

        llm.rank([f], self.cfg(), now=NOW, api_key="super-secret-key", post=fake_post)
        self.assertNotIn("super-secret-key", body_text["v"])


class LabelsByKeyTests(unittest.TestCase):
    def test_pairs_by_order_then_key(self):
        findings = [finding("a"), finding("b")]
        self.assertEqual(llm.labels_by_key(findings, ["high", "low"]), {"a": "high", "b": "low"})

    def test_none_labels_is_empty(self):
        self.assertEqual(llm.labels_by_key([finding("a")], None), {})


class ApplyPriorityTests(unittest.TestCase):
    def test_sets_priority_by_key_regardless_of_order(self):
        findings = [finding("a", priority=1), finding("b", priority=1)]
        out = llm.apply_priority(findings, {"b": "high", "a": "low"})
        self.assertEqual({f.key: f.priority for f in out}, {"a": 0, "b": 2})

    def test_unranked_key_keeps_default_priority(self):
        out = llm.apply_priority([finding("a", priority=1)], {"other": "high"})
        self.assertEqual(out[0].priority, 1)

    def test_empty_map_is_a_no_op(self):
        findings = [finding("a")]
        self.assertEqual(llm.apply_priority(findings, {}), findings)


class ConfigValidationTests(unittest.TestCase):
    def write(self, data):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_defaults(self):
        cfg = Config()
        self.assertFalse(cfg.llm_enabled)
        self.assertEqual(cfg.llm_model, "claude-haiku-5-5")
        self.assertEqual(cfg.llm_sources, ["mail", "notice", "news"])
        self.assertEqual(cfg.llm_exclude_accounts, [])
        self.assertEqual(cfg.llm_max_calls_per_day, 30)

    def test_valid_config_loads(self):
        cfg = load_config(self.write({
            "llm_enabled": True, "llm_model": "claude-haiku-5-5", "llm_profile": "교사",
            "llm_sources": ["mail"], "llm_exclude_accounts": ["학교"], "llm_max_calls_per_day": 10,
        }))
        self.assertTrue(cfg.llm_enabled)
        self.assertEqual(cfg.llm_exclude_accounts, ["학교"])

    def test_empty_model_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"llm_model": ""}))

    def test_unknown_source_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"llm_sources": ["mail", "calendar"]}))

    def test_non_string_exclude_account_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"llm_exclude_accounts": [1]}))

    def test_zero_or_negative_daily_budget_rejected(self):
        for n in (0, -1, True):
            with self.assertRaises(ConfigError):
                load_config(self.write({"llm_max_calls_per_day": n}))

    def test_overlong_profile_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"llm_profile": "x" * 501}))


if __name__ == "__main__":
    unittest.main()
