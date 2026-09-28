"""Unit tests for github2gitlab pure logic (no network, no git side effects).

Run: python3 -m unittest -q test_github2gitlab
"""
import argparse
import configparser
import contextlib
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github2gitlab as m  # noqa: E402

GHE = "https://ghe.example.com"
GL = "https://gitlab.example.com"


def closed_port():
    """A just-released loopback port: connecting to it is refused."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_cfg(**over):
    cfg = {
        "gh_base_url": GHE,
        "gh_api_base": GHE + "/api/v3",
        "gh_token": "gtk",
        "gh_clone_protocol": "https",
        "gh_login": "me",
        "gl_base_url": GL,
        "gl_api_base": GL + "/api/v4",
        "gl_token": "ltk",
        "gl_push_protocol": "https",
        "clone_dir": ".migrate-clones",
    }
    cfg.update(over)
    return cfg


class FakeApi:
    """Scripted (status, body) responses; records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append((method, url, headers, body))
        if not self.responses:
            raise AssertionError(f"unexpected extra call: {method} {url}")
        return self.responses.pop(0)


class TestNormalizeBase(unittest.TestCase):
    def test_bare_host_gets_https(self):
        self.assertEqual(m.normalize_base("ghe.corp.example.com"),
                         "https://ghe.corp.example.com")

    def test_full_url_kept_and_slash_stripped(self):
        self.assertEqual(m.normalize_base("http://host:8080/"),
                         "http://host:8080")


class TestMask(unittest.TestCase):
    def test_secret_replaced(self):
        self.assertEqual(m.mask("https://u:tk@host/x.git", "tk"),
                         "https://u:***@host/x.git")

    def test_empty_secret_noop(self):
        self.assertEqual(m.mask("text", ""), "text")


class TestHttpJson(unittest.TestCase):
    """http_json is the network boundary: statuses return, transport raises."""

    def _http_error(self, code, body):
        return urllib.error.HTTPError("http://host/api", code, "err", {},
                                      io.BytesIO(body.encode("utf-8")))

    def _response(self, status, raw):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = status
        resp.read.return_value = raw
        return resp

    def _get(self, response):
        with mock.patch.object(m.urllib.request, "urlopen", return_value=response):
            return m.http_json("GET", "http://host/api/x")

    def test_success_returns_status_and_parsed_body(self):
        self.assertEqual(self._get(self._response(200, b'{"login": "me"}')),
                         (200, {"login": "me"}))

    def test_non_json_ok_body_returned_raw(self):
        # a 200 that is an HTML proxy/SSO page is a response, not an exception
        self.assertEqual(self._get(self._response(200, b"<html>SSO login</html>")),
                         (200, "<html>SSO login</html>"))

    def test_non_utf8_ok_body_returned_as_text(self):
        status, body = self._get(self._response(200, b"caf\xe9 expired"))
        self.assertEqual(status, 200)
        self.assertIsInstance(body, str)
        self.assertTrue(body.startswith("caf"))
        self.assertTrue(body.endswith("expired"))

    def test_http_error_status_returns_code_and_parsed_body(self):
        with mock.patch.object(m.urllib.request, "urlopen",
                               side_effect=self._http_error(404, '{"message": "404"}')):
            self.assertEqual(m.http_json("GET", "http://host/api/x"),
                             (404, {"message": "404"}))

    def test_non_json_error_body_returned_raw(self):
        with mock.patch.object(m.urllib.request, "urlopen",
                               side_effect=self._http_error(500, "<html>boom</html>")):
            self.assertEqual(m.http_json("GET", "http://host/api/x"),
                             (500, "<html>boom</html>"))

    def test_non_numeric_port_raises_migrate_error(self):
        # a typo'd port is refused by http.client before any connection attempt
        with self.assertRaises(m.MigrateError) as ctx:
            m.http_json("GET", "https://host:808O/api/v3/user")
        self.assertIn("https://host:808O/api/v3/user", str(ctx.exception))

    def test_credential_pasted_into_url_is_redacted(self):
        # urllib quotes the password back (InvalidURL), so the message must redact
        with self.assertRaises(m.MigrateError) as ctx:
            m.http_json("GET", "https://oauth2:glpat-secret@host/api/v3/user")
        msg = str(ctx.exception)
        self.assertNotIn("glpat-secret", msg)
        self.assertIn("https://host/api/v3/user", msg)

    def test_malformed_ipv6_url_raises_migrate_error(self):
        # Request(...) itself raises ValueError for this one
        with self.assertRaises(m.MigrateError):
            m.http_json("GET", "http://[oops/api/v3/user")

    def test_broken_proxy_reply_raises_migrate_error(self):
        with mock.patch.object(m.urllib.request, "urlopen",
                               side_effect=m.http.client.BadStatusLine("junk status line")):
            with self.assertRaises(m.MigrateError) as ctx:
                m.http_json("GET", "http://host/api/v3/user")
        msg = str(ctx.exception)
        self.assertIn("http://host/api/v3/user", msg)
        self.assertIn("junk status line", msg)

    def test_truncated_body_raises_migrate_error(self):
        with mock.patch.object(m.urllib.request, "urlopen",
                               side_effect=m.http.client.IncompleteRead(b"partial")):
            with self.assertRaises(m.MigrateError):
                m.http_json("GET", "http://host/api/v3/user")

    def test_transport_failure_raises_naming_url_and_reason(self):
        with mock.patch.object(m.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(m.MigrateError) as ctx:
                m.http_json("GET", "http://127.0.0.1:9/api/v3/user",
                            {"PRIVATE-TOKEN": "ltk"})
        msg = str(ctx.exception)
        self.assertIn("http://127.0.0.1:9/api/v3/user", msg)
        self.assertIn("connection refused", msg)
        self.assertNotIn("ltk", msg)


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("GHE_TOKEN", "GITLAB_TOKEN", "GITHUB_TOKEN", "CODEUP_ACCESS_TOKEN"):
            os.environ.pop(name, None)

    BASE_INI = (
        "[github]\n"
        "base_url = ghe.example.com\n"
        "token = ini-gtk\n"
        "clone_protocol = https\n"
        "\n"
        "[gitlab]\n"
        "base_url = gitlab.example.com\n"
        "token = ini-ltk\n"
        "push_protocol = https\n"
        "\n"
        "[run]\n"
        "clone_dir = .migrate-clones\n"
    )

    NO_TOKEN_INI = (
        "[github]\n"
        "base_url = ghe.example.com\n"
        "clone_protocol = https\n"
        "\n"
        "[gitlab]\n"
        "base_url = gitlab.example.com\n"
        "push_protocol = https\n"
        "\n"
        "[run]\n"
        "clone_dir = .migrate-clones\n"
    )

    def _write(self, content):
        fd, path = tempfile.mkstemp(suffix=".ini")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        self.addCleanup(os.remove, path)
        return path

    def test_missing_file(self):
        with self.assertRaises(m.MigrateError):
            m.load_config("no-such-file.ini")

    def test_missing_values_reported_by_ini_names(self):
        path = self._write("[github]\n[gitlab]\n[run]\n")
        with self.assertRaises(m.MigrateError) as ctx:
            m.load_config(path)
        msg = str(ctx.exception)
        for name in ("github:base_url", "github:token", "gitlab:base_url",
                     "gitlab:token", "run:clone_dir"):
            self.assertIn(name, msg)

    def test_bases_derived_from_base_url(self):
        cfg = m.load_config(self._write(self.BASE_INI))
        self.assertEqual(cfg["gh_base_url"], GHE)
        self.assertEqual(cfg["gh_api_base"], f"{GHE}/api/v3")
        self.assertEqual(cfg["gl_base_url"], GL)
        self.assertEqual(cfg["gl_api_base"], f"{GL}/api/v4")
        self.assertEqual(cfg["clone_dir"], ".migrate-clones")

    def test_tokens_default_to_https_protocols(self):
        ini = self.BASE_INI.replace("clone_protocol = https\n", "") \
                           .replace("push_protocol = https\n", "")
        cfg = m.load_config(self._write(ini))
        self.assertEqual(cfg["gh_clone_protocol"], "https")
        self.assertEqual(cfg["gl_push_protocol"], "https")

    def test_env_tokens_used_when_ini_empty(self):
        env = {"GHE_TOKEN": "env-gtk", "GITLAB_TOKEN": "env-ltk"}
        with mock.patch.dict(os.environ, env):
            cfg = m.load_config(self._write(self.NO_TOKEN_INI))
        self.assertEqual(cfg["gh_token"], "env-gtk")
        self.assertEqual(cfg["gl_token"], "env-ltk")

    def test_env_token_missing_is_reported_as_ini_name(self):
        env = dict(os.environ)
        env["GHE_TOKEN"] = "env-gtk"
        env.pop("GITLAB_TOKEN", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_config(self._write(self.NO_TOKEN_INI))
        self.assertIn("gitlab:token", str(ctx.exception))

    def test_ini_token_wins_over_env(self):
        with mock.patch.dict(os.environ, {"GHE_TOKEN": "env-gtk"}):
            cfg = m.load_config(self._write(self.BASE_INI))
        self.assertEqual(cfg["gh_token"], "ini-gtk")

    def test_invalid_clone_protocol_rejected(self):
        ini = self.BASE_INI.replace("clone_protocol = https", "clone_protocol = ftp")
        with self.assertRaises(m.MigrateError) as ctx:
            m.load_config(self._write(ini))
        self.assertIn("clone_protocol", str(ctx.exception))

    def test_invalid_push_protocol_rejected(self):
        ini = self.BASE_INI.replace("push_protocol = https", "push_protocol = file")
        with self.assertRaises(m.MigrateError) as ctx:
            m.load_config(self._write(ini))
        self.assertIn("push_protocol", str(ctx.exception))


def ghe_repo(name, private=True, description="", size=None):
    repo = {"name": name, "private": private, "description": description}
    if size is not None:
        repo["size"] = size
    return repo


class TestGithubLogin(unittest.TestCase):
    def test_login_from_get_user(self):
        fake = FakeApi([(200, {"login": "octo"})])
        with mock.patch.object(m, "http_json", fake):
            self.assertEqual(m.github_login(make_cfg()), "octo")
        self.assertEqual(fake.calls[0][1], f"{GHE}/api/v3/user")
        self.assertEqual(fake.calls[0][2]["Authorization"], "Bearer gtk")

    def test_bad_credentials_fail_fast(self):
        fake = FakeApi([(401, {"message": "Bad credentials for gtk"})])
        with mock.patch.object(m, "http_json", fake):
            with self.assertRaises(m.MigrateError) as ctx:
                m.github_login(make_cfg())
        msg = str(ctx.exception)
        self.assertIn("HTTP 401", msg)
        self.assertIn("***", msg)
        self.assertNotIn("gtk", msg)

    def test_missing_login_field_rejected(self):
        with mock.patch.object(m, "http_json", FakeApi([(200, {})])):
            with self.assertRaises(m.MigrateError):
                m.github_login(make_cfg())


class TestGitlabNamespace(unittest.TestCase):
    def test_username_from_get_user(self):
        fake = FakeApi([(200, {"username": "me", "name": "Me"})])
        with mock.patch.object(m, "http_json", fake):
            self.assertEqual(m.gitlab_namespace(make_cfg()), "me")
        self.assertEqual(fake.calls[0][1], f"{GL}/api/v4/user")
        self.assertEqual(fake.calls[0][2]["PRIVATE-TOKEN"], "ltk")

    def test_missing_username_rejected(self):
        with mock.patch.object(m, "http_json", FakeApi([(200, {"id": 1})])):
            with self.assertRaises(m.MigrateError):
                m.gitlab_namespace(make_cfg())


class TestDeriveProjectBaseline(unittest.TestCase):
    def test_name_lowercased_under_namespace(self):
        self.assertEqual(m.derive_project_baseline("codeup-Demo-App", "me"),
                         "me/codeup-demo-app")

    def test_lowercase_name_unchanged(self):
        self.assertEqual(m.derive_project_baseline("mydemo", "ns"), "ns/mydemo")


class TestListSourceRepos(unittest.TestCase):
    def _list(self, responses):
        fake = FakeApi(responses)
        with mock.patch.object(m, "http_json", fake):
            repos, failures = m.list_source_repos(make_cfg())
        return repos, failures, fake

    def test_paginates_until_short_page(self):
        page1 = [ghe_repo(f"repo-{i:03d}") for i in range(m.PAGE_SIZE)]
        repos, failures, fake = self._list([(200, page1), (200, [ghe_repo("last")])])
        self.assertEqual(len(repos), m.PAGE_SIZE + 1)
        self.assertEqual(failures, [])
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("affiliation=owner", fake.calls[0][1])
        self.assertIn(f"per_page={m.PAGE_SIZE}", fake.calls[0][1])
        self.assertIn("page=1", fake.calls[0][1])
        self.assertIn("page=2", fake.calls[1][1])

    def test_empty_first_page_stops(self):
        repos, failures, fake = self._list([(200, [])])
        self.assertEqual((repos, failures), ([], []))
        self.assertEqual(len(fake.calls), 1)

    def test_parses_name_private_description_size(self):
        repos, _, _ = self._list([(200, [ghe_repo("codeup-a", private=False,
                                                  description=" demo ", size=2048)])])
        self.assertEqual(repos[0]["name"], "codeup-a")
        self.assertFalse(repos[0]["private"])
        self.assertEqual(repos[0]["description"], "demo")
        self.assertAlmostEqual(repos[0]["size_mb"], 2.0)

    def test_size_absent_is_none(self):
        repos, _, _ = self._list([(200, [ghe_repo("a")])])
        self.assertIsNone(repos[0]["size_mb"])

    def test_401_fails_fast(self):
        with mock.patch.object(m, "http_json",
                               FakeApi([(401, {"message": "Bad credentials gtk"})])):
            with self.assertRaises(m.MigrateError) as ctx:
                m.list_source_repos(make_cfg())
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_403_fails_fast(self):
        with mock.patch.object(m, "http_json", FakeApi([(403, {"message": "forbidden"})])):
            with self.assertRaises(m.MigrateError) as ctx:
                m.list_source_repos(make_cfg())
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_301_records_failure_and_keeps_earlier_pages(self):
        page1 = [ghe_repo(f"repo-{i:03d}") for i in range(m.PAGE_SIZE)]
        repos, failures, _ = self._list([(200, page1), (301, {"message": "moved"})])
        self.assertEqual(len(repos), m.PAGE_SIZE)
        self.assertEqual(len(failures), 1)
        self.assertIn("page 2", failures[0][0])
        self.assertIn("301", failures[0][1])

    def test_unsafe_repo_name_aborts_listing(self):
        with self.assertRaises(m.MigrateError) as ctx:
            self._list([(200, [ghe_repo("../../etc/passwd")])])
        self.assertIn("unsafe repo name", str(ctx.exception))


class TestEnsureProjectPrivate(unittest.TestCase):
    BASELINE = "me/codeup-demo-app"

    def _ensure(self, responses, baseline=None, name="codeup-Demo-App"):
        fake = FakeApi(responses)
        with mock.patch.object(m, "http_json", fake):
            project = m.ensure_project_private(make_cfg(), baseline or self.BASELINE, name)
        return project, fake

    def test_creates_private_project_on_404(self):
        project, fake = self._ensure([
            (404, {"message": "404 Project Not Found"}),
            (201, {"path_with_namespace": self.BASELINE, "visibility": "private"})])
        self.assertEqual(project["visibility"], "private")
        # existence query resolves the derived baseline (URL-encoded)
        self.assertEqual(fake.calls[0][0], "GET")
        self.assertIn("/api/v4/projects/me%2Fcodeup-demo-app", fake.calls[0][1])
        method, url, headers, body = fake.calls[1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/v4/projects"))
        self.assertEqual(body, {"name": "codeup-Demo-App",
                                "visibility": "private",
                                "initialize_with_readme": False})
        self.assertEqual(headers["PRIVATE-TOKEN"], "ltk")

    def test_existing_private_project_not_recreated(self):
        _, fake = self._ensure([(200, {"path_with_namespace": self.BASELINE,
                                       "visibility": "private"})])
        self.assertEqual(len(fake.calls), 1)

    def test_existing_non_private_project_refuses_to_push(self):
        with self.assertRaises(m.MigrateError) as ctx:
            self._ensure([(200, {"path_with_namespace": self.BASELINE,
                                 "visibility": "public"})])
        msg = str(ctx.exception)
        self.assertIn("public", msg)
        self.assertIn(self.BASELINE, msg)

    def test_existing_non_private_project_is_not_written_first(self):
        fake = FakeApi([(200, {"path_with_namespace": self.BASELINE,
                               "visibility": "internal"})])
        with mock.patch.object(m, "http_json", fake):
            with self.assertRaises(m.MigrateError):
                m.ensure_project_private(make_cfg(), self.BASELINE, "codeup-Demo-App")
        self.assertEqual([c[0] for c in fake.calls], ["GET"])

    def test_created_path_readback_mismatch_recorded(self):
        with self.assertRaises(m.MigrateError) as ctx:
            self._ensure([(404, None),
                          (201, {"path_with_namespace": "me/codeup-demo-app-x"})])
        msg = str(ctx.exception)
        self.assertIn("me/codeup-demo-app-x", msg)
        self.assertIn(self.BASELINE, msg)

    def test_missing_readback_field_recorded(self):
        with self.assertRaises(m.MigrateError):
            self._ensure([(404, None), (201, {"visibility": "private"})])

    def test_create_failure_raises_with_masked_body(self):
        with self.assertRaises(m.MigrateError) as ctx:
            self._ensure([(404, None), (403, {"message": "insufficient scope ltk"})])
        msg = str(ctx.exception)
        self.assertIn("HTTP 403", msg)
        self.assertIn("***", msg)
        self.assertNotIn("ltk", msg)

    def test_check_failure_raises(self):
        with self.assertRaises(m.MigrateError) as ctx:
            self._ensure([(500, "boom")])
        self.assertIn("HTTP 500", str(ctx.exception))


class TtyIO(io.StringIO):
    """StringIO that reports itself as a TTY (for prompt-gate tests)."""

    def isatty(self):
        return True


class TestCloneBare(unittest.TestCase):
    REPO = {"name": "codeup-Demo-App", "private": True, "description": "",
            "size_mb": None}

    def test_clone_command_and_target_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            with mock.patch.object(m.subprocess, "run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                clone_dir = m.clone_bare(cfg, self.REPO)
            cmd = run_mock.call_args[0][0]
            self.assertEqual(cmd[:3], ["git", "clone", "--bare"])
            self.assertEqual(cmd[3], f"https://me:gtk@ghe.example.com/me/codeup-Demo-App.git")
            self.assertEqual(cmd[4], os.path.join(tmp, "codeup-Demo-App.git"))
            self.assertEqual(clone_dir, os.path.join(tmp, "codeup-Demo-App.git"))
            self.assertEqual(run_mock.call_args.kwargs["timeout"],
                             m.CLONE_TIMEOUT_SECONDS)

    def test_ssh_clone_protocol_has_no_credentials_in_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp, gh_clone_protocol="ssh")
            with mock.patch.object(m.subprocess, "run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                m.clone_bare(cfg, self.REPO)
            self.assertEqual(run_mock.call_args[0][0][3],
                             "git@ghe.example.com:me/codeup-Demo-App.git")

    def test_clone_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            with mock.patch.object(m.subprocess, "run") as run_mock, \
                    mock.patch.object(m.time, "sleep"):
                run_mock.return_value = mock.Mock(returncode=128,
                                                  stderr="fatal: auth failed")
                with self.assertRaises(m.MigrateError) as ctx:
                    m.clone_bare(cfg, self.REPO)
            self.assertIn("auth failed", str(ctx.exception))

    def test_clone_retries_on_transient_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            results = [mock.Mock(returncode=128, stderr="fatal: fetch-pack: boom"),
                       mock.Mock(returncode=0, stderr="")]
            with mock.patch.object(m.subprocess, "run", side_effect=results) as run_mock, \
                    mock.patch.object(m.time, "sleep") as sleep_mock:
                clone_dir = m.clone_bare(cfg, self.REPO)
            self.assertEqual(run_mock.call_count, 2)
            self.assertEqual(clone_dir, os.path.join(tmp, "codeup-Demo-App.git"))
            sleep_mock.assert_called_once_with(m.CLONE_BACKOFF_SECONDS)

    def test_clone_gives_up_after_max_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            fail = mock.Mock(returncode=128, stderr="fatal: boom")
            with mock.patch.object(m.subprocess, "run",
                                   side_effect=[fail] * m.CLONE_RETRIES) as run_mock, \
                    mock.patch.object(m.time, "sleep"):
                with self.assertRaises(m.MigrateError):
                    m.clone_bare(cfg, self.REPO)
            self.assertEqual(run_mock.call_count, m.CLONE_RETRIES)

    def test_clone_timeout_retries_then_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            with mock.patch.object(m.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("git", 1)), \
                    mock.patch.object(m.time, "sleep"):
                with self.assertRaises(m.MigrateError) as ctx:
                    m.clone_bare(cfg, self.REPO)
            self.assertIn("timed out", str(ctx.exception))

    def test_clone_removes_partial_dir_between_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            target = os.path.join(tmp, "codeup-Demo-App.git")
            stale_seen = []
            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd)
                if len(calls) == 1:
                    os.makedirs(os.path.join(target, "objects"), exist_ok=True)
                    return mock.Mock(returncode=128, stderr="fatal: partial")
                stale_seen.append(os.path.exists(os.path.join(target, "objects")))
                return mock.Mock(returncode=0)

            with mock.patch.object(m.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(m.time, "sleep"):
                m.clone_bare(cfg, self.REPO)
            self.assertEqual(stale_seen, [False])


class TestPushMirror(unittest.TestCase):
    def test_push_url_uses_baseline_and_mirror(self):
        with mock.patch.object(m, "run_git") as git_mock:
            m.push_mirror(make_cfg(), "me/codeup-demo-app", "/clones/x.git")
        args, kwargs = git_mock.call_args
        self.assertEqual(args[0], ["git", "push", "--mirror",
                                   "https://oauth2:ltk@gitlab.example.com/"
                                   "me/codeup-demo-app.git"])
        self.assertEqual(kwargs["cwd"], "/clones/x.git")

    def test_ssh_push_protocol(self):
        with mock.patch.object(m, "run_git") as git_mock:
            m.push_mirror(make_cfg(gl_push_protocol="ssh"), "me/x", "/clones/x.git")
        self.assertEqual(git_mock.call_args[0][0],
                         ["git", "push", "--mirror",
                          "git@gitlab.example.com:me/x.git"])


class TestConfirmMigration(unittest.TestCase):
    def _args(self, yes=False):
        return argparse.Namespace(yes=yes)

    def test_yes_skips_prompt(self):
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self.assertTrue(m.confirm_migration(self._args(yes=True), 2,
                                                make_cfg(), "me"))

    def test_non_tty_proceeds_without_prompt(self):
        with mock.patch.object(sys, "stdin", io.StringIO()), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self.assertTrue(m.confirm_migration(self._args(), 2, make_cfg(), "me"))

    def test_tty_accept(self):
        with mock.patch.object(sys, "stdin", TtyIO()), \
                mock.patch.object(sys, "stdout", TtyIO()), \
                mock.patch("builtins.input", return_value=" y ") as prompt:
            self.assertTrue(m.confirm_migration(self._args(), 2, make_cfg(), "me"))
        self.assertIn("gitlab.example.com/me", prompt.call_args[0][0])

    def test_tty_decline(self):
        with mock.patch.object(sys, "stdin", TtyIO()), \
                mock.patch.object(sys, "stdout", TtyIO()), \
                mock.patch("builtins.input", return_value="n"):
            self.assertFalse(m.confirm_migration(self._args(), 2, make_cfg(), "me"))


class TestAcquireLock(unittest.TestCase):
    def test_lock_prevents_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = m.acquire_lock(tmp)
            self.assertTrue(os.path.exists(lock))
            with self.assertRaises(m.MigrateError) as ctx:
                m.acquire_lock(tmp)
            self.assertIn("another migration", str(ctx.exception))

    def test_stale_lock_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, ".migrate.lock")
            with open(lock, "w") as f:
                f.write("999999999")  # no such pid
            m.acquire_lock(tmp)
            with open(lock, encoding="utf-8") as f:
                self.assertEqual(f.read(), str(os.getpid()))


class TestWriteManifest(unittest.TestCase):
    def test_one_line_per_repo_with_source_private_flag_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = m.write_manifest(tmp, [
                ("codeup-Demo-App", "me/codeup-demo-app", True, "ok"),
                ("demo", "me/demo", False, "failed")])
            self.assertEqual(path, os.path.join(tmp, m.MANIFEST_NAME))
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines()
                         if ln and not ln.startswith("#")]
        self.assertEqual(lines, [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tfailed"])


class TestMain(unittest.TestCase):
    """Shared main() invocation with scripted API responses."""

    @staticmethod
    def _catch_exit(fn):
        try:
            fn()
            return 0
        except SystemExit as e:
            return e.code

    def run_main(self, responses, argv=None, cfg_over=None, ensure=None, clone=None,
                 push=None, stdin_tty=False, stdout_tty=False, input_text=None,
                 setup=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = make_cfg(clone_dir=tmp)
        cfg.update(cfg_over or {})
        out = TtyIO() if stdout_tty else io.StringIO()
        err = io.StringIO()
        fake = FakeApi(responses)
        events = []

        def default_clone(c, r):
            events.append(("clone", r["name"]))
            path = os.path.join(tmp, r["name"] + ".git")
            os.makedirs(path, exist_ok=True)  # clone_bare leaves a real dir behind
            return path

        def default_ensure(c, baseline, name):
            events.append(("ensure", name))
            return {"path_with_namespace": baseline, "visibility": "private"}

        def default_push(c, baseline, clone_dir):
            events.append(("push", baseline))

        if setup:
            setup(tmp)
        with mock.patch.object(m, "http_json", fake), \
                mock.patch.object(m, "run_git") as git_mock, \
                mock.patch.object(m, "load_config", return_value=cfg), \
                mock.patch.object(m, "acquire_lock", return_value=os.path.join(tmp, ".lock")), \
                mock.patch.object(m, "clone_bare", side_effect=clone or default_clone), \
                mock.patch.object(m, "push_mirror",
                                  side_effect=push or default_push) as push_mock, \
                mock.patch.object(m, "ensure_project_private",
                                  side_effect=ensure or default_ensure) as ensure_mock, \
                mock.patch.object(sys, "argv", argv or ["github2gitlab.py", "--yes"]), \
                mock.patch.object(sys, "stdin") as stdin, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            stdin.isatty.return_value = stdin_tty
            if input_text is not None:
                with mock.patch("builtins.input", return_value=input_text):
                    code = self._catch_exit(m.main)
            else:
                code = self._catch_exit(m.main)
        return types.SimpleNamespace(
            code=code, out=out.getvalue(), err=err.getvalue(), fake=fake,
            git_mock=git_mock, push_mock=push_mock, ensure_mock=ensure_mock,
            events=events, clone_dir=tmp, manifest=os.path.join(tmp, m.MANIFEST_NAME))

    def read_manifest(self, path):
        with open(path, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln and not ln.startswith("#")]


class TestMainPreRepoErrors(TestMain):
    """Errors raised before any repo work: exit 2 with an error line, no traceback.

    The real code path runs here (real load_config, real http_json), so this is
    the end-to-end counterpart of the unit tests for those helpers.
    """

    def _tmp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    def _run_real(self, argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            return self._catch_exit(m.main), err.getvalue()

    def test_unreachable_source_exits_2(self):
        tmp = self._tmp()
        cfg_path = os.path.join(tmp, "unreachable.ini")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(f"[github]\nbase_url = 127.0.0.1:{closed_port()}\ntoken = gtk\n\n"
                    f"[gitlab]\nbase_url = gitlab.example.com\ntoken = ltk\n\n"
                    f"[run]\nclone_dir = {tmp}\n")
        code, err = self._run_real(["github2gitlab.py", "--yes", "-c", cfg_path])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_malformed_ini_exits_2(self):
        tmp = self._tmp()
        cfg_path = os.path.join(tmp, "malformed.ini")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("base_url = ghe.example.com\n")  # no [section] header
        code, err = self._run_real(["github2gitlab.py", "-c", cfg_path])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        # one actionable line, not a traceback and not a multi-line parser dump
        self.assertEqual(len(err.splitlines()), 1)

    def test_malformed_ini_does_not_echo_the_source_line(self):
        # configparser embeds the offending line verbatim and a token can sit there
        tmp = self._tmp()
        cfg_path = os.path.join(tmp, "secret.ini")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("token = glpat-SECRETVALUE\n[github]\n")
        code, err = self._run_real(["github2gitlab.py", "-c", cfg_path])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("SECRETVALUE", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_non_utf8_config_exits_2(self):
        # a config file that is not UTF-8 is a config error, not a traceback
        tmp = self._tmp()
        cfg_path = os.path.join(tmp, "binary.ini")
        with open(cfg_path, "wb") as f:
            f.write(b"[github]\nbase_url = ghe.example.com\ntoken = \xff\xfe\n")
        code, err = self._run_real(["github2gitlab.py", "-c", cfg_path])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_missing_git_binary_exits_2(self):
        # real condition probe: git cannot be launched at all, and the config is
        # valid, so only the pre-repo git check can fail here
        tmp = self._tmp()
        empty_path = os.path.join(tmp, "empty-path")
        os.makedirs(empty_path)
        cfg_path = os.path.join(tmp, "valid.ini")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(f"[github]\nbase_url = ghe.example.com\ntoken = gtk\n\n"
                    f"[gitlab]\nbase_url = gitlab.example.com\ntoken = ltk\n\n"
                    f"[run]\nclone_dir = {tmp}\n")
        with mock.patch.dict(os.environ, {"PATH": empty_path}):
            code, err = self._run_real(["github2gitlab.py", "-c", cfg_path])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertIn("git", err)
        self.assertNotIn("Traceback", err)
        # one actionable line: the widened handler, not a traceback
        self.assertEqual(len(err.splitlines()), 1)


class TestMainDryRun(TestMain):
    def test_lists_target_paths_and_writes_nothing(self):
        res = self.run_main(
            [(200, {"login": "me"}), (200, {"username": "me"}),
             (200, [ghe_repo("codeup-Demo-App"), ghe_repo("demo", private=False)]),
             (200, [])],
            argv=["github2gitlab.py", "--dry-run"])
        self.assertEqual(res.code, 0)
        self.assertIn("found 2 repo(s)", res.out)
        self.assertIn("-> me/codeup-demo-app", res.out)
        self.assertIn("-> me/demo [public source]", res.out)
        # dry-run performs no remote writes and no git clone/push
        self.assertEqual({c[0] for c in res.fake.calls}, {"GET"})
        res.git_mock.assert_called_once_with(["git", "--version"])
        res.push_mock.assert_not_called()
        self.assertFalse(os.path.exists(res.manifest))

    def test_dry_run_exits_1_when_listing_had_failures(self):
        full_page = [ghe_repo(f"repo-{i:03d}") for i in range(m.PAGE_SIZE)]
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"}),
                             (200, full_page), (301, {"message": "moved"})],
                            argv=["github2gitlab.py", "--dry-run"])
        self.assertEqual(res.code, 1)
        self.assertIn("301", res.err)


class TestMainMigrate(TestMain):
    REPOS = [(200, [ghe_repo("codeup-Demo-App"), ghe_repo("demo", private=False)]),
             (200, [])]

    def test_phases_and_manifest_rows(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS)
        self.assertEqual(res.code, 0)
        self.assertIn("phase 1/2: cloning 2 repo(s)", res.err)
        self.assertIn("phase 2/2: pushing 2 repo(s)", res.err)
        # every clone happens before the first push (two-phase)
        self.assertEqual(res.events, [
            ("clone", "codeup-Demo-App"), ("clone", "demo"),
            ("ensure", "codeup-Demo-App"), ("push", "me/codeup-demo-app"),
            ("ensure", "demo"), ("push", "me/demo")])
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tok"])

    def test_only_filter_limits_run_and_manifest(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            argv=["github2gitlab.py", "--yes", "--only", "demo"])
        self.assertEqual(res.code, 0)
        self.assertEqual([e[1] for e in res.events if e[0] == "clone"], ["demo"])
        self.assertEqual(self.read_manifest(res.manifest), ["demo\tme/demo\tpublic\tok"])

    def test_only_accepts_several_names(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            argv=["github2gitlab.py", "--yes",
                                  "--only", "demo", "--only", "codeup-Demo-App"])
        self.assertEqual(res.code, 0)
        self.assertEqual(len(self.read_manifest(res.manifest)), 2)

    def test_unknown_only_name_is_usage_error(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            argv=["github2gitlab.py", "--yes", "--only", "nope"])
        self.assertEqual(res.code, 2)
        self.assertIn("nope", res.err)
        self.assertFalse(os.path.exists(res.manifest))

    def test_clone_failure_skips_push_and_exits_1(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        def failing_clone(cfg, r):
            if r["name"] == "demo":
                raise m.MigrateError(f"git clone failed for {r['name']} ltk")
            path = os.path.join(tmp, r["name"] + ".git")
            os.makedirs(path, exist_ok=True)
            return path

        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            clone=failing_clone)
        self.assertEqual(res.code, 1)
        self.assertIn("1 success, 1 failed", res.err)
        # tokens never reach the failure output
        self.assertIn("***", res.err)
        self.assertNotIn("ltk", res.err)
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tfailed"])

    def test_non_private_target_project_fails_before_push(self):
        def ensure(cfg, baseline, name):
            if name == "demo":
                raise m.MigrateError(f"existing project {baseline} is not private "
                                     "(visibility=public); refusing to push")
            return {"path_with_namespace": baseline, "visibility": "private"}
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            ensure=ensure)
        self.assertEqual(res.code, 1)
        pushed = [e[1] for e in res.events if e[0] == "push"]
        self.assertEqual(pushed, ["me/codeup-demo-app"])
        self.assertIn("not private", res.err)
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tfailed"])

    def test_confirm_declined_exits_130(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            argv=["github2gitlab.py"],
                            stdin_tty=True, stdout_tty=True, input_text="n")
        self.assertEqual(res.code, 130)
        self.assertIn("cancelled", res.err)
        self.assertEqual(res.events, [])

    def test_confirm_accepted_proceeds(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            argv=["github2gitlab.py"],
                            stdin_tty=True, stdout_tty=True, input_text="y")
        self.assertEqual(res.code, 0)
        self.assertIn("done:", res.err)

    def test_non_tty_runs_without_prompt(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            argv=["github2gitlab.py"])
        self.assertEqual(res.code, 0)

    def test_empty_repo_set_is_not_an_error(self):
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"}), (200, [])])
        self.assertEqual(res.code, 0)
        self.assertIn("nothing to migrate", res.err)
        self.assertFalse(os.path.exists(res.manifest))

    def test_enumeration_failure_with_no_repos_exits_1(self):
        # a 301 on the first page yields zero repos plus a recorded failure:
        # an incomplete enumeration must not look like a successful empty run
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"}),
                             (301, {"message": "moved"})])
        self.assertEqual(res.code, 1)
        self.assertIn("nothing to migrate", res.err)
        self.assertIn("301", res.err)
        self.assertFalse(os.path.exists(res.manifest))

    def test_config_error_exits_2(self):
        with mock.patch.object(m, "run_git"), \
                mock.patch.object(m, "load_config",
                                  side_effect=m.MigrateError("missing config values: []")), \
                mock.patch.object(sys, "argv", ["github2gitlab.py"]), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self._catch_exit(m.main)
        self.assertEqual(code, 2)
        self.assertIn("error:", err.getvalue())

    def test_enumeration_auth_failure_exits_2_with_masked_token(self):
        res = self.run_main([(401, {"message": "Bad credentials gtk"})])
        self.assertEqual(res.code, 2)
        self.assertIn("HTTP 401", res.err)
        self.assertIn("***", res.err)
        self.assertNotIn("gtk", res.err)

    def test_push_failure_recorded_and_exits_1(self):
        # risk R2: a rejected mirror push (protected branch) must be a recorded
        # failure, never a silent success
        def failing_push(cfg, baseline, clone_dir):
            if baseline == "me/demo":
                raise m.MigrateError("git push failed: pre-receive hook declined ltk")

        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})] + self.REPOS,
                            push=failing_push)
        self.assertEqual(res.code, 1)
        self.assertIn("1 success, 1 failed", res.err)
        self.assertIn("***", res.err)
        self.assertNotIn("ltk", res.err)
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tfailed"])


class TestCredentialGuards(unittest.TestCase):
    """G-CRED minimum checks: the ignore entry is effective and the shipped
    example config carries no token (proposal.md, G-CRED verification route)."""

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def test_gitignore_entry_is_effective(self):
        with open(os.path.join(self.ROOT, ".gitignore"), encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
        self.assertIn("github2gitlab.ini", lines)
        ret = subprocess.run(["git", "check-ignore", "-q", "github2gitlab.ini"],
                             cwd=self.ROOT)
        self.assertEqual(ret.returncode, 0,
                         "github2gitlab.ini is not ignored by the shipped .gitignore")

    def test_example_config_has_the_required_keys_without_tokens(self):
        cp = configparser.ConfigParser()
        cp.read(os.path.join(self.ROOT, "github2gitlab.example.ini"), encoding="utf-8")
        for section, key in (("github", "base_url"), ("github", "token"),
                             ("gitlab", "base_url"), ("gitlab", "token"),
                             ("run", "clone_dir")):
            self.assertTrue(cp.has_option(section, key), f"[{section}] {key} missing")
        for section in ("github", "gitlab"):
            self.assertEqual(cp.get(section, "token").strip(), "",
                             f"[{section}] token is not empty")


class TestPushMirrorFailure(unittest.TestCase):
    """The real push path: a rejected mirror push raises instead of passing."""

    def test_rejected_mirror_push_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = os.path.join(tmp, "demo.git")
            work = os.path.join(tmp, "work")
            subprocess.run(["git", "init", "-q", "--bare", clone],
                           check=True, capture_output=True)
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            subprocess.run(["git", "clone", "-q", clone, work],
                           check=True, capture_output=True)
            for args in (["commit", "-q", "--allow-empty", "-m", "init"],
                         ["branch", "-M", "main"],
                         ["push", "-q", "origin", "main"]):
                subprocess.run(["git", "-C", work] + args,
                               check=True, capture_output=True, env=env)

            target = os.path.join(tmp, "push-target", "me", "demo.git")
            os.makedirs(os.path.dirname(target))
            subprocess.run(["git", "init", "-q", "--bare", target],
                           check=True, capture_output=True)
            hook = os.path.join(target, "hooks", "pre-receive")
            with open(hook, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\necho 'rejected by pre-receive policy' >&2\nexit 1\n")
            os.chmod(hook, 0o755)

            # no scheme in gl_base_url: target_push_url then yields a local path
            cfg = make_cfg(gl_base_url=os.path.join(tmp, "push-target"))
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(m.MigrateError) as ctx:
                    m.push_mirror(cfg, "me/demo", clone)
            self.assertIn("git push failed", str(ctx.exception))
            self.assertIn("rejected by pre-receive policy", str(ctx.exception))


class TestParseArgsHalves(unittest.TestCase):
    def test_clone_only_and_push_only_are_mutually_exclusive(self):
        with mock.patch.object(sys, "argv",
                               ["github2gitlab.py", "--clone-only", "--push-only"]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                m.parse_args()
        self.assertEqual(ctx.exception.code, 2)


class TestLocalMatchesSource(unittest.TestCase):
    """The skip judgment reuses the verifier's refs equality."""

    CFG = make_cfg()

    @staticmethod
    def _bare(tmp):
        bare = os.path.join(tmp, "demo.git")
        subprocess.run(["git", "init", "-q", "--bare", bare],
                       check=True, capture_output=True)
        return bare

    def test_missing_local_clone_is_not_current_and_makes_no_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeApi([])  # any call is an unexpected call
            with mock.patch.object(m, "http_json", fake):
                self.assertFalse(m.local_matches_source(
                    self.CFG, os.path.join(tmp, "demo.git"), "demo"))

    def test_empty_local_clone_against_empty_source_is_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeApi([(200, []), (200, [])])
            with mock.patch.object(m, "http_json", fake):
                self.assertTrue(m.local_matches_source(self.CFG, self._bare(tmp), "demo"))

    def test_source_branch_missing_locally_is_not_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeApi([(200, [{"name": "main", "commit": {"sha": "a" * 40}}]),
                            (200, [])])
            with mock.patch.object(m, "http_json", fake):
                self.assertFalse(m.local_matches_source(self.CFG, self._bare(tmp), "demo"))

    def test_source_refs_error_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeApi([(500, "boom")])
            with mock.patch.object(m, "http_json", fake):
                with self.assertRaises(m.MigrateError) as ctx:
                    m.local_matches_source(self.CFG, self._bare(tmp), "demo")
            self.assertIn("source refs unavailable", str(ctx.exception))


class TestClonePhase(unittest.TestCase):
    REPO = {"name": "demo", "private": True, "description": "", "size_mb": None}

    def test_current_clone_is_skipped_without_a_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(m, "local_matches_source", return_value=True), \
                    mock.patch.object(m, "clone_bare") as clone_mock:
                ready, skipped, failed = m.clone_phase(make_cfg(clone_dir=tmp), [self.REPO])
            clone_mock.assert_not_called()
            self.assertEqual(skipped, ["demo"])
            self.assertEqual(ready, [(self.REPO, os.path.join(tmp, "demo.git"))])
            self.assertEqual(failed, [])

    def test_refs_check_error_falls_back_to_cloning(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(m, "local_matches_source",
                                   side_effect=m.MigrateError("source refs unavailable")), \
                    mock.patch.object(m, "clone_bare",
                                      return_value="/clones/demo.git") as clone_mock:
                ready, skipped, failed = m.clone_phase(make_cfg(clone_dir=tmp), [self.REPO])
            clone_mock.assert_called_once()
            self.assertEqual((skipped, failed), ([], []))
            self.assertEqual(ready, [(self.REPO, "/clones/demo.git")])

    def test_clone_failure_is_recorded_with_the_token_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(m, "local_matches_source", return_value=False), \
                    mock.patch.object(m, "clone_bare",
                                      side_effect=m.MigrateError("clone failed gtk")):
                ready, skipped, failed = m.clone_phase(make_cfg(clone_dir=tmp), [self.REPO])
            self.assertEqual(ready, [])
            self.assertEqual(failed, [("demo", "clone failed ***")])


class TestRepoList(unittest.TestCase):
    def test_round_trip_records_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = [{"name": "demo", "private": True, "size_mb": 1.5}]
            path = m.write_repo_list(tmp, repos, {"demo": True})
            self.assertEqual(path, os.path.join(tmp, m.REPO_LIST_NAME))
            self.assertEqual(m.read_repo_list(tmp),
                             [{"name": "demo", "private": True, "size_mb": 1.5,
                               "clone_verified": True}])

    def test_untouched_repo_keeps_its_previous_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.write_repo_list(tmp, [{"name": "a"}, {"name": "b"}],
                              {"a": True, "b": True})
            m.write_repo_list(tmp, [{"name": "a"}, {"name": "b"}, {"name": "c"}],
                              {"a": True})
            flags = {r["name"]: r["clone_verified"] for r in m.read_repo_list(tmp)}
            self.assertEqual(flags, {"a": True, "b": True, "c": False})

    def test_failed_clone_clears_the_previous_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.write_repo_list(tmp, [{"name": "a"}], {"a": True})
            m.write_repo_list(tmp, [{"name": "a"}], {"a": False})
            self.assertFalse(m.read_repo_list(tmp)[0]["clone_verified"])

    def test_unreadable_previous_list_is_rebuilt_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, m.REPO_LIST_NAME), "w", encoding="utf-8") as f:
                f.write("{not json")
            m.write_repo_list(tmp, [{"name": "a"}], {})
            self.assertFalse(m.read_repo_list(tmp)[0]["clone_verified"])

    def test_non_boolean_flag_is_not_carried_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, m.REPO_LIST_NAME), "w", encoding="utf-8") as f:
                f.write('[{"name": "a", "clone_verified": "true"}]')
            m.write_repo_list(tmp, [{"name": "a"}], {})
            self.assertFalse(m.read_repo_list(tmp)[0]["clone_verified"])

    def test_update_repo_list_merges_states_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.write_repo_list(tmp, [{"name": "a", "private": True},
                                    {"name": "b", "private": False}],
                              {"a": True, "b": True})
            m.update_repo_list(tmp, {"a": False})
            entries = m.read_repo_list(tmp)
            self.assertEqual([(e["name"], e["clone_verified"]) for e in entries],
                             [("a", False), ("b", True)])

    def test_update_repo_list_without_a_list_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(m.update_repo_list(tmp, {"a": False}))
            self.assertFalse(os.path.exists(os.path.join(tmp, m.REPO_LIST_NAME)))

    def test_update_repo_list_leaves_an_unreadable_list_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, m.REPO_LIST_NAME)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            m.update_repo_list(tmp, {"a": False})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "{not json")

    def test_missing_list_names_the_clone_half(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(m.MigrateError) as ctx:
                m.read_repo_list(tmp)
            self.assertIn("--clone-only", str(ctx.exception))

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, m.REPO_LIST_NAME), "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(m.MigrateError) as ctx:
                m.read_repo_list(tmp)
            self.assertIn("malformed repo list", str(ctx.exception))

    def test_entry_without_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, m.REPO_LIST_NAME), "w", encoding="utf-8") as f:
                f.write('[{"private": true}]')
            with self.assertRaises(m.MigrateError) as ctx:
                m.read_repo_list(tmp)
            self.assertIn("without a name", str(ctx.exception))

    def test_non_string_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, m.REPO_LIST_NAME), "w", encoding="utf-8") as f:
                f.write('[{"name": 123}]')
            with self.assertRaises(m.MigrateError) as ctx:
                m.read_repo_list(tmp)
            self.assertIn("without a name", str(ctx.exception))

    def test_unsafe_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, m.REPO_LIST_NAME), "w", encoding="utf-8") as f:
                f.write('[{"name": "../../etc/passwd"}]')
            with self.assertRaises(m.MigrateError) as ctx:
                m.read_repo_list(tmp)
            self.assertIn("unsafe repo name", str(ctx.exception))


class TestManifestLedger(unittest.TestCase):
    @staticmethod
    def _rows(clone_dir):
        with open(os.path.join(clone_dir, m.MANIFEST_NAME), encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln and not ln.startswith("#")]

    def test_new_rows_are_appended_after_untouched_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.write_manifest(tmp, [("old-repo", "me/old-repo", True, "ok")])
            m.write_manifest_ledger(tmp, [("demo", "me/demo", False, "ok")])
            self.assertEqual(self._rows(tmp), [
                "old-repo\tme/old-repo\tprivate\tok",
                "demo\tme/demo\tpublic\tok"])

    def test_touched_rows_are_refreshed_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.write_manifest(tmp, [("old-repo", "me/old-repo", True, "failed"),
                                   ("demo", "me/demo", False, "failed")])
            m.write_manifest_ledger(tmp, [("demo", "me/demo", False, "ok")])
            self.assertEqual(self._rows(tmp), [
                "old-repo\tme/old-repo\tprivate\tfailed",
                "demo\tme/demo\tpublic\tok"])

    def test_malformed_existing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, m.MANIFEST_NAME), "w", encoding="utf-8") as f:
                f.write("just-one-field\n")
            with self.assertRaises(m.MigrateError):
                m.write_manifest_ledger(tmp, [])


class TestMainCloneOnly(TestMain):
    REPOS = [(200, [ghe_repo("codeup-Demo-App"), ghe_repo("demo", private=False)]),
             (200, [])]

    def test_only_the_source_is_contacted_and_verified_repos_are_recorded(self):
        res = self.run_main([(200, {"login": "me"})] + self.REPOS,
                            argv=["github2gitlab.py", "--clone-only"])
        self.assertEqual(res.code, 0)
        self.assertTrue(all(GHE in c[1] for c in res.fake.calls),
                        f"target contacted: {res.fake.calls}")
        self.assertEqual([e[1] for e in res.events if e[0] == "clone"],
                         ["codeup-Demo-App", "demo"])
        self.assertFalse(os.path.exists(res.manifest))
        flags = {r["name"]: r["clone_verified"] for r in m.read_repo_list(res.clone_dir)}
        self.assertEqual(flags, {"codeup-Demo-App": True, "demo": True})

    def test_current_clone_is_skipped_end_to_end(self):
        def seed(tmp):
            subprocess.run(["git", "init", "-q", "--bare",
                            os.path.join(tmp, "demo.git")], check=True, capture_output=True)
        res = self.run_main([(200, {"login": "me"}), (200, [ghe_repo("demo")]),
                             (200, []), (200, []), (200, [])],
                            argv=["github2gitlab.py", "--clone-only"], setup=seed)
        self.assertEqual(res.code, 0)
        self.assertEqual([e for e in res.events if e[0] == "clone"], [])
        self.assertIn("skip demo", res.err)
        self.assertIn("0 cloned, 1 skipped, 0 failed", res.err)
        self.assertEqual(m.read_repo_list(res.clone_dir),
                         [{"name": "demo", "private": True, "description": "",
                           "size_mb": None, "clone_verified": True}])

    def test_failed_clone_is_marked_unverified(self):
        # the leftover of a failed clone must never read as a usable clone
        def failing_clone(cfg, r):
            if r["name"] == "demo":
                raise m.MigrateError("clone failed")
            return os.path.join(cfg["clone_dir"], r["name"] + ".git")

        res = self.run_main([(200, {"login": "me"})] + self.REPOS,
                            argv=["github2gitlab.py", "--clone-only"],
                            clone=failing_clone)
        self.assertEqual(res.code, 1)
        flags = {r["name"]: r["clone_verified"] for r in m.read_repo_list(res.clone_dir)}
        self.assertEqual(flags, {"codeup-Demo-App": True, "demo": False})

    def test_repos_are_marked_unverified_before_any_clone_work(self):
        # a run killed mid-clone must not leave a stale verified flag behind
        seen = []

        def seed(tmp):
            m.write_repo_list(tmp, [{"name": "codeup-Demo-App"},
                                    {"name": "demo"}],
                              {"codeup-Demo-App": True, "demo": True})

        def exploding_clone(cfg, r):
            seen.extend(entry["clone_verified"]
                        for entry in m.read_repo_list(cfg["clone_dir"]))
            raise RuntimeError("killed mid-clone")

        with self.assertRaises(RuntimeError):
            self.run_main([(200, {"login": "me"})] + self.REPOS,
                          argv=["github2gitlab.py", "--clone-only"],
                          setup=seed, clone=exploding_clone)
        self.assertEqual(seen, [False, False])

    def test_repo_list_covers_the_full_enumeration_under_only(self):
        res = self.run_main([(200, {"login": "me"})] + self.REPOS,
                            argv=["github2gitlab.py", "--clone-only", "--only", "demo"])
        self.assertEqual(res.code, 0)
        self.assertEqual([e[1] for e in res.events if e[0] == "clone"], ["demo"])
        flags = {r["name"]: r["clone_verified"] for r in m.read_repo_list(res.clone_dir)}
        self.assertEqual(flags, {"codeup-Demo-App": False, "demo": True})

    def test_repo_list_write_failure_is_a_usage_error(self):
        with mock.patch.object(m, "write_repo_list", side_effect=OSError("disk full")):
            res = self.run_main([(200, {"login": "me"})] + self.REPOS,
                                argv=["github2gitlab.py", "--clone-only"])
        self.assertEqual(res.code, 2)
        self.assertIn("error:", res.err)

    def test_dry_run_writes_nothing(self):
        res = self.run_main([(200, {"login": "me"})] + self.REPOS,
                            argv=["github2gitlab.py", "--clone-only", "--dry-run"])
        self.assertEqual(res.code, 0)
        self.assertEqual(res.events, [])
        self.assertFalse(os.path.exists(os.path.join(res.clone_dir, m.REPO_LIST_NAME)))
        self.assertFalse(os.path.exists(res.manifest))

    def test_incomplete_enumeration_keeps_the_old_list_and_exits_1(self):
        # a partial page must not clobber a good repo list
        def seed(tmp):
            with open(os.path.join(tmp, m.REPO_LIST_NAME), "w", encoding="utf-8") as f:
                f.write('[{"name": "previous", "private": true}]')
        full_page = [ghe_repo(f"repo-{i:03d}") for i in range(m.PAGE_SIZE)]
        res = self.run_main([(200, {"login": "me"}), (200, full_page),
                             (301, {"message": "moved"})],
                            argv=["github2gitlab.py", "--clone-only"], setup=seed)
        self.assertEqual(res.code, 1)
        self.assertIn("repo list not written", res.err)
        with open(os.path.join(res.clone_dir, m.REPO_LIST_NAME), encoding="utf-8") as f:
            self.assertIn("previous", f.read())


class TestMainPushOnly(TestMain):
    LIST = [{"name": "codeup-Demo-App", "private": True, "size_mb": None},
            {"name": "demo", "private": False, "size_mb": 1.0}]

    def _setup(self, tmp, repos=None, clones=("codeup-Demo-App", "demo"),
               manifest=None):
        repos = self.LIST if repos is None else repos
        m.write_repo_list(tmp, repos,
                          {r["name"]: r.get("clone_verified", True) for r in repos})
        for name in clones:
            os.makedirs(os.path.join(tmp, name + ".git"), exist_ok=True)
        if manifest is not None:
            m.write_manifest(tmp, manifest)

    def test_only_the_target_is_contacted_and_the_manifest_is_written(self):
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only", "--yes"],
                            setup=self._setup)
        self.assertEqual(res.code, 0)
        self.assertTrue(all("ghe" not in c[1] for c in res.fake.calls),
                        f"source contacted: {res.fake.calls}")
        self.assertEqual([e for e in res.events if e[0] == "push"],
                         [("push", "me/codeup-demo-app"), ("push", "me/demo")])
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tok"])

    def test_missing_local_clone_is_a_recorded_failure(self):
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only", "--yes"],
                            setup=lambda tmp: self._setup(tmp, clones=("demo",)))
        self.assertEqual(res.code, 1)
        self.assertIn("no local clone", res.err)
        self.assertEqual([e[1] for e in res.events if e[0] == "push"], ["me/demo"])
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tfailed",
            "demo\tme/demo\tpublic\tok"])

    def test_unverified_clone_is_refused_with_a_failed_row(self):
        # a directory that exists is not enough: only a clone the clone half
        # verified may be mirrored (a leftover could delete target refs)
        list_with_flag = [dict(self.LIST[0], clone_verified=True),
                          dict(self.LIST[1], clone_verified=False)]
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only", "--yes"],
                            setup=lambda tmp: self._setup(tmp, repos=list_with_flag))
        self.assertEqual(res.code, 1)
        self.assertIn("local clone not verified", res.err)
        self.assertEqual(res.events, [("ensure", "codeup-Demo-App"),
                                      ("push", "me/codeup-demo-app")])
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tfailed"])

    def test_missing_repo_list_exits_2_without_any_call(self):
        res = self.run_main([], argv=["github2gitlab.py", "--push-only", "--yes"])
        self.assertEqual(res.code, 2)
        self.assertIn("error:", res.err)
        self.assertEqual(res.fake.calls, [])

    def test_unknown_only_name_is_a_usage_error(self):
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only", "--yes",
                                  "--only", "nope"],
                            setup=self._setup)
        self.assertEqual(res.code, 2)
        self.assertIn("nope", res.err)

    def test_dry_run_writes_nothing(self):
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only", "--dry-run"],
                            setup=self._setup)
        self.assertEqual(res.code, 0)
        self.assertEqual(res.events, [])
        self.assertFalse(os.path.exists(res.manifest))

    def test_ledger_keeps_the_rows_it_did_not_touch(self):
        seeded = [("codeup-Demo-App", "me/codeup-demo-app", True, "ok"),
                  ("demo", "me/demo", False, "ok")]
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only", "--yes",
                                  "--only", "demo"],
                            setup=lambda tmp: self._setup(tmp, clones=("demo",),
                                                          manifest=seeded))
        self.assertEqual(res.code, 0)
        self.assertEqual([e[1] for e in res.events if e[0] == "push"], ["me/demo"])
        self.assertEqual(self.read_manifest(res.manifest), [
            "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok",
            "demo\tme/demo\tpublic\tok"])

    def test_confirm_declined_exits_130(self):
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only"],
                            setup=self._setup, stdin_tty=True, stdout_tty=True,
                            input_text="n")
        self.assertEqual(res.code, 130)
        self.assertEqual(res.events, [])

    def test_non_boolean_verification_flag_is_refused(self):
        # only a real true counts: "true"/1 must not pass as verified
        list_bad = [{"name": "demo", "private": False, "clone_verified": "true"}]
        res = self.run_main([(200, {"username": "me"})],
                            argv=["github2gitlab.py", "--push-only", "--yes"],
                            setup=lambda tmp: self._setup(tmp, repos=list_bad,
                                                          clones=("demo",)))
        self.assertEqual(res.code, 1)
        self.assertIn("local clone not verified", res.err)
        self.assertEqual(res.events, [])


class TestMainLedger(TestMain):
    REPOS = [(200, [ghe_repo("demo", private=False)]), (200, [])]

    def test_default_rerun_skips_the_current_clone_and_keeps_other_rows(self):
        # the local clone already matches (empty bare repo, empty source refs):
        # nothing is transferred, and the manifest keeps the row it did not touch
        def seed(tmp):
            subprocess.run(["git", "init", "-q", "--bare",
                            os.path.join(tmp, "demo.git")], check=True, capture_output=True)
            m.write_manifest(tmp, [("old-repo", "me/old-repo", True, "ok")])
        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})]
                            + self.REPOS + [(200, []), (200, [])],
                            argv=["github2gitlab.py", "--yes"], setup=seed)
        self.assertEqual(res.code, 0)
        self.assertEqual(res.events, [("ensure", "demo"), ("push", "me/demo")])
        self.assertIn("skip demo", res.err)
        self.assertEqual(self.read_manifest(res.manifest), [
            "old-repo\tme/old-repo\tprivate\tok",
            "demo\tme/demo\tpublic\tok"])

    def test_default_flow_invalidates_the_flag_before_recloning(self):
        # the default run follows the same rule as the clone half: the flag is
        # withdrawn before the clone is wiped, so a killed run cannot go stale
        seen = []

        def seed(tmp):
            subprocess.run(["git", "init", "-q", "--bare",
                            os.path.join(tmp, "demo.git")], check=True, capture_output=True)
            m.write_repo_list(tmp, [{"name": "demo", "private": False}], {"demo": True})

        def exploding_clone(cfg, r):
            seen.extend(entry["clone_verified"]
                        for entry in m.read_repo_list(cfg["clone_dir"]))
            raise RuntimeError("killed mid-clone")

        with self.assertRaises(RuntimeError):
            self.run_main([(200, {"login": "me"}), (200, {"username": "me"}),
                           (200, [ghe_repo("demo", private=False)]), (200, []),
                           (200, [{"name": "main", "commit": {"sha": "a" * 40}}]),
                           (200, [])],
                          argv=["github2gitlab.py", "--yes"], setup=seed,
                          clone=exploding_clone)
        self.assertEqual(seen, [False])

    def test_default_flow_records_verified_clones(self):
        def seed(tmp):
            m.write_repo_list(tmp, [{"name": "demo", "private": False}], {})

        res = self.run_main([(200, {"login": "me"}), (200, {"username": "me"})]
                            + self.REPOS, argv=["github2gitlab.py", "--yes"], setup=seed)
        self.assertEqual(res.code, 0)
        self.assertEqual([(e["name"], e["clone_verified"])
                          for e in m.read_repo_list(res.clone_dir)],
                         [("demo", True)])


if __name__ == "__main__":
    unittest.main()
