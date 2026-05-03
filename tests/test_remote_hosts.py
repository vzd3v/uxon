"""Tests for ``[[remote_hosts]]`` parsing in ``uxon.remote_hosts``.

The schema is the entry surface for multi-host SSH support. These
tests pin:

- The happy path: a list of valid table dicts becomes a list of
  immutable :class:`RemoteHost` records with defaults applied.
- Strict validation: every documented invariant raises
  :class:`RemoteHostError` with a message that names the offending
  field, so an operator with a typo in ``config.toml`` sees a clean
  failure and not a confused traceback later when the SSH collector
  shells out.
- ``name`` charset: rejects anything that would cause filename
  trouble in the snapshot cache (``~/.local/state/uxon/remote/<name>.json``).
- Unknown keys are rejected — typos like ``ssh_alaias`` must fail
  loud rather than silently disabling the host.

Pure module: no I/O, no subprocess.
"""

from __future__ import annotations

import unittest

from uxon.remote_hosts import (
    RemoteHost,
    RemoteHostError,
    find_host,
    load_remote_hosts,
)


class LoadRemoteHostsHappyPathTests(unittest.TestCase):
    def test_none_means_empty(self) -> None:
        self.assertEqual(load_remote_hosts(None), [])

    def test_missing_section_treated_as_empty(self) -> None:
        # The CLI passes ``merged.get("remote_hosts", [])`` — an absent
        # section becomes an empty list. Round-trip that through.
        self.assertEqual(load_remote_hosts([]), [])

    def test_minimum_fields_with_defaults(self) -> None:
        [host] = load_remote_hosts([{"name": "vz-prod1", "ssh_alias": "vz-prod1"}])
        self.assertEqual(host.name, "vz-prod1")
        self.assertEqual(host.ssh_alias, "vz-prod1")
        self.assertEqual(host.description, "")
        self.assertEqual(host.remote_uxon, "uxon")  # documented default

    def test_all_fields_passed_through(self) -> None:
        [host] = load_remote_hosts(
            [
                {
                    "name": "edge.eu",
                    "ssh_alias": "edge-eu",
                    "description": "EU edge node",
                    "remote_uxon": "/opt/uxon/bin/uxon",
                }
            ]
        )
        self.assertEqual(host.name, "edge.eu")
        self.assertEqual(host.ssh_alias, "edge-eu")
        self.assertEqual(host.description, "EU edge node")
        self.assertEqual(host.remote_uxon, "/opt/uxon/bin/uxon")

    def test_order_preserved(self) -> None:
        hosts = load_remote_hosts(
            [
                {"name": "a", "ssh_alias": "a"},
                {"name": "b", "ssh_alias": "b"},
                {"name": "c", "ssh_alias": "c"},
            ]
        )
        self.assertEqual([h.name for h in hosts], ["a", "b", "c"])

    def test_strings_are_stripped(self) -> None:
        [host] = load_remote_hosts(
            [{"name": "  foo  ", "ssh_alias": "  bar  ", "description": "  hi  "}]
        )
        self.assertEqual(host.name, "foo")
        self.assertEqual(host.ssh_alias, "bar")
        self.assertEqual(host.description, "hi")

    def test_record_is_immutable(self) -> None:
        [host] = load_remote_hosts([{"name": "x", "ssh_alias": "x"}])
        with self.assertRaises(AttributeError):
            host.name = "y"  # type: ignore[misc]


class LoadRemoteHostsValidationTests(unittest.TestCase):
    def test_top_level_must_be_list(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, "must be an array of tables"):
            load_remote_hosts("vz-prod1")  # type: ignore[arg-type]

    def test_entry_must_be_table(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, r"remote_hosts\[0\] must be a table"):
            load_remote_hosts(["not-a-dict"])  # type: ignore[list-item]

    def test_missing_name(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, "missing or empty 'name'"):
            load_remote_hosts([{"ssh_alias": "x"}])

    def test_empty_name(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, "missing or empty 'name'"):
            load_remote_hosts([{"name": "  ", "ssh_alias": "x"}])

    def test_missing_ssh_alias(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, "missing or empty 'ssh_alias'"):
            load_remote_hosts([{"name": "x"}])

    def test_duplicate_names_rejected(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, "duplicate name 'foo'"):
            load_remote_hosts(
                [
                    {"name": "foo", "ssh_alias": "a"},
                    {"name": "foo", "ssh_alias": "b"},
                ]
            )

    def test_invalid_name_charset(self) -> None:
        # ``name`` becomes a filename — reject anything outside the
        # documented ASCII whitelist.
        for bad in ("foo bar", "foo/bar", "foo:bar", "foo$bar", "../etc"):
            with self.assertRaises(RemoteHostError, msg=f"name {bad!r} should be rejected"):
                load_remote_hosts([{"name": bad, "ssh_alias": "x"}])

    def test_valid_name_charset_accepted(self) -> None:
        # Letters, digits, ``_``, ``-``, ``.`` are all allowed.
        for good in ("vz-prod1", "edge.eu", "host_42", "A.B-C_d.1"):
            hosts = load_remote_hosts([{"name": good, "ssh_alias": "x"}])
            self.assertEqual(hosts[0].name, good)

    def test_description_must_be_string(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, "description must be a string"):
            load_remote_hosts([{"name": "x", "ssh_alias": "x", "description": 42}])

    def test_remote_uxon_empty_rejected(self) -> None:
        with self.assertRaisesRegex(RemoteHostError, "remote_uxon must be a non-empty string"):
            load_remote_hosts([{"name": "x", "ssh_alias": "x", "remote_uxon": ""}])

    def test_unknown_key_rejected(self) -> None:
        # Typo protection: a misspelled key would silently disable the
        # host's intended config. Better to fail loud.
        with self.assertRaisesRegex(RemoteHostError, "unknown key"):
            load_remote_hosts([{"name": "x", "ssh_alias": "x", "ssh_alaias": "y"}])


class FindHostTests(unittest.TestCase):
    def test_returns_match_or_none(self) -> None:
        hosts = load_remote_hosts(
            [{"name": "a", "ssh_alias": "a"}, {"name": "b", "ssh_alias": "b"}]
        )
        self.assertIsInstance(find_host(hosts, "a"), RemoteHost)
        self.assertEqual(find_host(hosts, "a").name, "a")  # type: ignore[union-attr]
        self.assertIsNone(find_host(hosts, "missing"))


if __name__ == "__main__":
    unittest.main()
