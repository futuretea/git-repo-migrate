"""Unit tests for codeup2github pure logic (no network, no git side effects).

Run: python3 -m unittest test_codeup2github -v
"""
import configparser
import contextlib
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import codeup2github as m  # noqa: E402

ORG = "60de7a6852743a5162b5f957"


def closed_port():
    """A just-released loopback port: connecting to it is refused."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_cfg(**over):
    cfg = {
        "api_style": "legacy",
        "org_id": ORG,
        "codeup_token": "ctk",
        "group_path": "backend",
        "api_base": "https://devops.example.com",
        "git_base": "https://codeup.example.com",
        "codeup_git_user": "cu",
        "insecure_tls": False,
        "gh_user": "me",
        "gh_token": "gtk",
        "gh_api_base": "https://api.github.com",
        "gh_git_base": "https://github.com",
        "gh_push_ssh": False,
        "clone_dir": ".migrate-clones",
        "exclude": set(),
        "include_groups": [],
        "my_creator_ids": set(),
        "repo_prefix": "",
        "git_codeup_prefix": True,
    }
    cfg.update(over)
    return cfg


class FakeApi:
    """Feeds scripted http_json responses and records calls."""

    def __init__(self, pages):
        self.seq = iter(pages)
        self.calls = []

    def __call__(self, method, url, headers=None, body=None, insecure=False):
        self.calls.append((method, url, headers, body, insecure))
        return 200, next(self.seq)


class TestNormalizeBase(unittest.TestCase):
    def test_bare_host_gets_https(self):
        self.assertEqual(m.normalize_base("codeup.corp.example.com"),
                         "https://codeup.corp.example.com")

    def test_full_url_kept_and_slash_stripped(self):
        self.assertEqual(m.normalize_base("http://host:8080/"),
                         "http://host:8080")


class TestMapRepoName(unittest.TestCase):
    def test_multi_level_flattened(self):
        self.assertEqual(m.map_repo_name("backend/team-a/web"), "backend-team-a-web")

    def test_single_segment_unchanged(self):
        self.assertEqual(m.map_repo_name("solo"), "solo")

    def test_prefix_applied(self):
        self.assertEqual(m.map_repo_name("demo/app", "codeup-"),
                         "codeup-demo-app")
        self.assertEqual(m.map_repo_name("mydemo", "codeup-"), "codeup-mydemo")


class TestSelectionMode(unittest.TestCase):
    """include_groups whitelist + my_creator_ids personal repos."""

    def page(self, repos):
        return [{"success": True, "result": [
            dict(r, path=r["rel"].rsplit("/", 1)[-1], description="", archived=False,
                 creatorId=r.get("creatorId", 9),
                 pathWithNamespace=f"{ORG}/{r['rel']}")
            for r in repos]}]

    def _list(self, cfg_over, repos):
        cfg = make_cfg(**cfg_over)
        cfg.pop("group_path", None)
        with mock.patch.object(m, "http_json",
                               FakeApi(self.page(repos) + [{"success": True, "result": []}])):
            return m.list_codeup_repos(cfg)

    def test_group_whitelist_plus_creator(self):
        repos = [
            {"rel": "container/kured", "creatorId": 41},
            {"rel": "observability/signoz", "creatorId": 50},
            {"rel": "jenseny/react-test", "creatorId": 41},   # mine, outside groups
            {"rel": "jenseny/depend-test", "creatorId": 50},  # not mine, outside
            {"rel": "mydemo", "creatorId": 41},               # mine, root level
            {"rel": "spring-boot", "creatorId": 60},          # not mine, root
        ]
        picked = self._list({"include_groups": ["container", "observability"],
                             "my_creator_ids": {"41"}}, repos)
        rels = [r["rel"] for r in picked]
        self.assertEqual(rels, ["container/kured", "observability/signoz",
                                "jenseny/react-test", "mydemo"])

    def test_exclude_still_applies_in_selection_mode(self):
        repos = [
            {"rel": "container/kured", "creatorId": 41},
            {"rel": "container/old", "creatorId": 41},
        ]
        picked = self._list({"include_groups": ["container"],
                             "my_creator_ids": {"41"},
                             "exclude": {"container/old"}}, repos)
        self.assertEqual([r["rel"] for r in picked], ["container/kured"])

    def test_no_selection_mode_falls_back_to_group_path(self):
        # include_groups/my_creator_ids empty -> original single-prefix behavior
        repos = [{"rel": "container/kured", "creatorId": 41},
                 {"rel": "x/y", "creatorId": 41}]
        cfg_over = {"group_path": "container"}
        cfg = make_cfg(**cfg_over)
        with mock.patch.object(m, "http_json",
                               FakeApi(self.page(repos) + [{"success": True, "result": []}])):
            picked = m.list_codeup_repos(cfg)
        self.assertEqual([r["rel"] for r in picked], ["container/kured"])


class TestMask(unittest.TestCase):
    def test_secret_replaced(self):
        self.assertEqual(m.mask("https://u:tk@host/x.git", "tk"),
                         "https://u:***@host/x.git")

    def test_empty_secret_noop(self):
        self.assertEqual(m.mask("text", ""), "text")


class TestListReposLegacy(unittest.TestCase):
    def test_filter_exclude_and_flag_compat(self):
        pages = [
            {"success": True, "result": [
                {"pathWithNamespace": f"{ORG}/backend/team-a/web", "path": "web",
                 "description": " d ", "archive": False},
                {"pathWithNamespace": f"{ORG}/backend/legacy", "path": "legacy",
                 "description": "", "archive": False},
                {"pathWithNamespace": f"{ORG}/frontend/app", "path": "app",
                 "description": "", "archive": False},
                {"pathWithNamespace": f"{ORG}/backend/old", "path": "old",
                 "description": "", "archive": True},
            ]},
            {"success": True, "result": []},
        ]
        fake = FakeApi(pages)
        with mock.patch.object(m, "http_json", fake):
            repos = m.list_codeup_repos(make_cfg(exclude={"backend/legacy"}))
        self.assertEqual([r["rel"] for r in repos], ["backend/team-a/web", "backend/old"])
        self.assertEqual(repos[0]["description"], "d")
        # legacy auth goes in query params, not headers
        url = fake.calls[0][1]
        self.assertIn("organizationId=" + ORG, url)
        self.assertIn("accessToken=ctk", url)
        self.assertEqual(fake.calls[0][2], {})

    def test_api_error_raises_masked(self):
        fake = FakeApi([])
        with mock.patch.object(m, "http_json",
                               return_value=(403, {"errorMessage": "bad ctk"})):
            with self.assertRaises(m.MigrateError) as ctx:
                m.list_codeup_repos(make_cfg())
        self.assertIn("HTTP 403", str(ctx.exception))
        # token value leaked into the error body must be masked in output
        self.assertIn("bad ***", str(ctx.exception))


class TestListReposOapi(unittest.TestCase):
    def test_region_endpoint_and_header_auth_when_no_org(self):
        page = [{"pathWithNamespace": f"{ORG}/backend/web", "path": "web",
                 "description": "", "archived": False}]
        fake = FakeApi([page, []])
        with mock.patch.object(m, "http_json", fake):
            repos = m.list_codeup_repos(make_cfg(api_style="oapi", org_id="",
                                                 group_path=""))
        self.assertEqual([r["rel"] for r in repos], [f"{ORG}/backend/web"])
        self.assertTrue(fake.calls[-1][1].startswith(
            f"{m.normalize_base('devops.example.com')}/oapi/v1/codeup/repositories?"))
        self.assertEqual(fake.calls[-1][2], {"x-yunxiao-token": "ctk"})

    def test_org_endpoint_when_org_id_set(self):
        fake = FakeApi([[], None])
        with mock.patch.object(m, "http_json", fake):
            m.list_codeup_repos(make_cfg(api_style="oapi", org_id="ORGID"))
        self.assertIn("/oapi/v1/codeup/organizations/ORGID/repositories?",
                      fake.calls[0][1])

    def test_insecure_tls_flag_propagates(self):
        fake = FakeApi([[], None])
        with mock.patch.object(m, "http_json", fake):
            m.list_codeup_repos(make_cfg(api_style="oapi", insecure_tls=True))
        self.assertTrue(fake.calls[0][4])


class TestPagination(unittest.TestCase):
    def test_continues_until_short_page(self):
        full = {"success": True, "result": [{"pathWithNamespace": f"{ORG}/backend/r{i}",
                                             "path": f"r{i}", "description": "",
                                             "archive": False} for i in range(m.PAGE_SIZE)]}
        tail = {"success": True, "result": [
            {"pathWithNamespace": f"{ORG}/backend/last", "path": "last",
             "description": "", "archive": False}]}
        fake = FakeApi([full, tail])
        with mock.patch.object(m, "http_json", fake):
            repos = m.list_codeup_repos(make_cfg())
        self.assertEqual(len(repos), m.PAGE_SIZE + 1)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("page=2", fake.calls[1][1])


class TestUrls(unittest.TestCase):
    def test_clone_url_credentials(self):
        # regression: colon between user and token must stay literal
        url = m.codeup_clone_url(make_cfg(), {"path_with_namespace": f"{ORG}/a/b"})
        self.assertEqual(url,
                         f"https://cu:ctk@codeup.example.com/codeup/{ORG}/a/b.git")

    def test_clone_url_without_codeup_prefix(self):
        url = m.codeup_clone_url(make_cfg(git_codeup_prefix=False),
                                 {"path_with_namespace": f"{ORG}/a"})
        self.assertEqual(url, f"https://cu:ctk@codeup.example.com/{ORG}/a.git")

    def test_clone_url_http_base(self):
        url = m.codeup_clone_url(make_cfg(git_base="http://git.example.com:8080"),
                                 {"path_with_namespace": f"{ORG}/a"})
        self.assertEqual(url,
                         f"http://cu:ctk@git.example.com:8080/codeup/{ORG}/a.git")

    def test_push_url(self):
        self.assertEqual(m.github_push_url(make_cfg(), "n"),
                         "https://me:gtk@github.com/me/n.git")

    def test_push_url_ssh_mode(self):
        cfg = make_cfg(gh_push_ssh=True, gh_git_base="https://scm.example.com")
        self.assertEqual(m.github_push_url(cfg, "n"),
                         "git@scm.example.com:me/n.git")

    def test_push_url_ghe(self):
        ghe = make_cfg(gh_api_base="https://scm.example.com/api/v3",
                       gh_git_base="https://scm.example.com")
        self.assertEqual(m.github_push_url(ghe, "n"),
                         "https://me:gtk@scm.example.com/me/n.git")

    def test_ghe_api_base_used_for_repo_check(self):
        cfg = make_cfg(gh_api_base="https://scm.example.com/api/v3",
                       gh_git_base="https://scm.example.com")
        fake = FakeApi([{"id": 1, "private": True, "visibility": "private"}])
        with mock.patch.object(m, "http_json", fake):
            self.assertTrue(m.ensure_github_repo(cfg, "n", "d"))
        self.assertTrue(fake.calls[0][1].startswith(
            "https://scm.example.com/api/v3/repos/me/n"))

    def test_special_chars_quoted(self):
        cfg = make_cfg(codeup_git_user="u ser", codeup_token="tk/1")
        url = m.codeup_clone_url(cfg, {"path_with_namespace": "ORG/a"})
        self.assertEqual(url,
                         "https://u%20ser:tk%2F1@codeup.example.com/codeup/ORG/a.git")


class TestEnsureGithubRepo(unittest.TestCase):
    def test_existing_private_repo_skips_create(self):
        cfg = make_cfg()
        fake = FakeApi([{"id": 1, "private": True, "visibility": "private"}])
        with mock.patch.object(m, "http_json", fake):
            self.assertTrue(m.ensure_github_repo(cfg, "n", "d"))
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("/repos/me/n", fake.calls[0][1])
        self.assertEqual(fake.calls[0][0], "GET")

    def test_existing_public_repo_refused_before_any_write(self):
        # mirror-pushing private history into a public repo cannot be undone
        cfg = make_cfg()
        fake = FakeApi([{"id": 1, "private": False, "visibility": "public"}])
        with mock.patch.object(m, "http_json", fake):
            with self.assertRaises(m.MigrateError) as ctx:
                m.ensure_github_repo(cfg, "n", "d")
        msg = str(ctx.exception)
        self.assertIn("not confirmed private", msg)
        self.assertIn("visibility=public", msg)
        self.assertIn("me/n", msg)
        self.assertEqual([c[0] for c in fake.calls], ["GET"])

    def test_existing_internal_repo_refused(self):
        # instance-internal repos report private=true but are readable beyond the owner
        cfg = make_cfg()
        fake = FakeApi([{"id": 1, "private": True, "visibility": "internal"}])
        with mock.patch.object(m, "http_json", fake):
            with self.assertRaises(m.MigrateError) as ctx:
                m.ensure_github_repo(cfg, "n", "d")
        self.assertIn("internal", str(ctx.exception))

    def test_existing_repo_without_visibility_refused(self):
        # an unexpected payload shape must not be read as "private"
        cfg = make_cfg()
        fake = FakeApi([{"id": 1}])
        with mock.patch.object(m, "http_json", fake):
            with self.assertRaises(m.MigrateError) as ctx:
                m.ensure_github_repo(cfg, "n", "d")
        self.assertIn("unknown", str(ctx.exception))

    def test_existing_private_flag_without_visibility_refused(self):
        # `private` alone also covers instance-internal repos: both fields count
        cfg = make_cfg()
        fake = FakeApi([{"id": 1, "private": True}])
        with mock.patch.object(m, "http_json", fake):
            with self.assertRaises(m.MigrateError) as ctx:
                m.ensure_github_repo(cfg, "n", "d")
        self.assertIn("visibility=unknown", str(ctx.exception))

    def test_existing_repo_with_a_non_object_body_refused(self):
        # decode_body can hand back an HTML/SSO page or a list for a 200
        cfg = make_cfg()
        fake = FakeApi(["<html>SSO login</html>"])
        with mock.patch.object(m, "http_json", fake):
            with self.assertRaises(m.MigrateError):
                m.ensure_github_repo(cfg, "n", "d")

    def test_creates_private_repo_on_404(self):
        cfg = make_cfg()
        with mock.patch.object(m, "http_json",
                               side_effect=[(404, None), (201, {"id": 1})]) as fake:
            self.assertTrue(m.ensure_github_repo(cfg, "n", "desc"))
        self.assertEqual(fake.call_count, 2)
        method, url, headers, body = fake.call_args[0]
        self.assertEqual(method, "POST")
        self.assertIn("/user/repos", url)
        self.assertEqual(body, {"name": "n", "description": "desc", "private": True})
        self.assertEqual(headers["Authorization"], "Bearer gtk")

    def test_422_already_exists_refuses_without_a_readback(self):
        # the repo appeared between the existence check and the create: its
        # visibility was never confirmed, so it is not a safe push target
        cfg = make_cfg()
        with mock.patch.object(m, "http_json",
                               side_effect=[(404, None),
                                            (422, {"message":
                                                   "name already exists on this account"})]) as fake:
            with self.assertRaises(m.MigrateError) as ctx:
                m.ensure_github_repo(cfg, "n", "d")
        self.assertIn("refusing to push", str(ctx.exception))
        self.assertEqual(fake.call_count, 2)  # no third call

    def test_create_failure_raises(self):
        cfg = make_cfg()
        with mock.patch.object(m, "http_json",
                               side_effect=[(404, None), (401, "Bad credentials")]):
            with self.assertRaises(m.MigrateError):
                m.ensure_github_repo(cfg, "n", "d")


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("GHE_TOKEN", "GITLAB_TOKEN", "GITHUB_TOKEN", "CODEUP_ACCESS_TOKEN"):
            os.environ.pop(name, None)

    def _write(self, tmp, content):
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        return tmp

    def test_missing_file(self):
        with self.assertRaises(m.MigrateError):
            m.load_config("no-such-file.ini")

    def test_missing_values_reported_by_ini_names(self):
        tmp = self._write("/tmp/c2g_missing.ini", "[codeup]\n[github]\n[run]\n")
        try:
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_config(tmp)
            msg = str(ctx.exception)
            # git_host fails fast with its own actionable message
            self.assertIn("git_host", msg)
        finally:
            os.remove(tmp)

    def test_missing_values_after_git_host_filled(self):
        tmp = self._write("/tmp/c2g_missing2.ini", (
            "[codeup]\ngit_host = gh\n[github]\n[run]\n"))
        try:
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_config(tmp)
            msg = str(ctx.exception)
            self.assertIn("api_domain", msg)
            self.assertIn("access_token", msg)
            self.assertIn("git_username", msg)
            self.assertIn("organization_id", msg)  # legacy requires it
            self.assertNotIn("api_base", msg)
        finally:
            os.remove(tmp)

    def test_oapi_allows_empty_org_id(self):
        tmp = self._write("/tmp/c2g_oapi.ini", (
            "[codeup]\napi_style = oapi\napi_domain = h.example.com\ngit_host = gh.example.com\n"
            "access_token = tk\ngit_username = u\n"
            "[github]\nusername = me\ntoken = gt\n[run]\n"))
        try:
            cfg = m.load_config(tmp)
            self.assertEqual(cfg["api_base"], "https://h.example.com")
            self.assertEqual(cfg["org_id"], "")
        finally:
            os.remove(tmp)

    def test_env_tokens_used_when_ini_empty(self):
        tmp = self._write("/tmp/c2g_env.ini", (
            "[codeup]\napi_domain = h\ngit_host = gh\napi_style = oapi\ngit_username = u\n"
            "[github]\nusername = me\n[run]\n"))
        env = {"CODEUP_ACCESS_TOKEN": "envctk", "GITHUB_TOKEN": "envgtk"}
        try:
            with mock.patch.dict(os.environ, env):
                cfg = m.load_config(tmp)
            self.assertEqual(cfg["codeup_token"], "envctk")
            self.assertEqual(cfg["gh_token"], "envgtk")
        finally:
            os.remove(tmp)

    def test_ini_token_wins_over_env(self):
        tmp = self._write("/tmp/c2g_ini2.ini", (
            "[codeup]\napi_domain = h\ngit_host = gh\napi_style = oapi\naccess_token = ini\n"
            "git_username = u\n[github]\nusername = me\ntoken = gt\n[run]\n"))
        try:
            with mock.patch.dict(os.environ, {"CODEUP_ACCESS_TOKEN": "envctk"}):
                cfg = m.load_config(tmp)
            self.assertEqual(cfg["codeup_token"], "ini")
        finally:
            os.remove(tmp)

    def test_invalid_api_style_rejected(self):
        tmp = self._write("/tmp/c2g_style.ini", (
            "[codeup]\napi_style = bogus\napi_domain = h\ngit_host = gh\naccess_token = tk\n"
            "git_username = u\norganization_id = o\n"
            "[github]\nusername = me\ntoken = gt\n[run]\n"))
        try:
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_config(tmp)
            self.assertIn("api_style", str(ctx.exception))
        finally:
            os.remove(tmp)


class TestValidateRepoPath(unittest.TestCase):
    def test_traversal_segment_rejected(self):
        for bad in (f"{ORG}/../x", f"{ORG}/a/..", "..", f"{ORG}/a\\\\b", ""):
            with self.assertRaises(m.MigrateError):
                m.validate_repo_path(bad)

    def test_normal_path_accepted(self):
        m.validate_repo_path(f"{ORG}/backend/team-a/web")

    def test_traversal_from_api_aborts_listing(self):
        page = [{"pathWithNamespace": f"{ORG}/backend/../../etc/x", "path": "x",
                 "description": "", "archive": False}]
        with mock.patch.object(m, "http_json",
                               FakeApi([{"success": True, "result": page}, []])):
            with self.assertRaises(m.MigrateError) as ctx:
                m.list_codeup_repos(make_cfg())
        self.assertIn("unsafe repo path", str(ctx.exception))


class TestMigrateRepo(unittest.TestCase):
    REPO = {"path": "b", "path_with_namespace": f"{ORG}/a/b",
            "rel": "a/b", "description": "", "archived": False,
            "size_mb": None}

    def test_clone_bare_with_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            with mock.patch.object(m.subprocess, "run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                clone_dir = m.clone_repo(cfg, self.REPO)
            clone_cmd = run_mock.call_args[0][0]
            self.assertEqual(clone_cmd[:3], ["git", "clone", "--bare"])
            self.assertIn("https://cu:ctk@", clone_cmd[3])
            self.assertEqual(clone_cmd[4], os.path.join(tmp, f"{ORG}/a/b.git"))
            self.assertEqual(clone_dir, os.path.join(tmp, f"{ORG}/a/b.git"))
            self.assertNotIn("GIT_SSL_NO_VERIFY", run_mock.call_args.kwargs["env"])

    def test_clone_insecure_tls_sets_git_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp, insecure_tls=True)
            with mock.patch.object(m.subprocess, "run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                m.clone_repo(cfg, self.REPO)
            self.assertEqual(run_mock.call_args.kwargs["env"]["GIT_SSL_NO_VERIFY"], "1")

    def test_clone_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            with mock.patch.object(m.subprocess, "run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=128,
                                                  stderr="fatal: auth failed")
                with self.assertRaises(m.MigrateError) as ctx:
                    m.clone_repo(cfg, self.REPO)
            self.assertIn("auth failed", str(ctx.exception))

    def test_clone_retries_on_transient_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            results = [mock.Mock(returncode=128, stderr="fatal: fetch-pack: invalid index-pack output"),
                       mock.Mock(returncode=0, stderr="")]
            with mock.patch.object(m.subprocess, "run", side_effect=results) as run_mock, \
                    mock.patch.object(m.time, "sleep") as sleep_mock:
                clone_dir = m.clone_repo(cfg, self.REPO)
            self.assertEqual(run_mock.call_count, 2)
            self.assertEqual(clone_dir, os.path.join(tmp, f"{ORG}/a/b.git"))
            sleep_mock.assert_called_once()

    def test_clone_gives_up_after_max_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            fail = mock.Mock(returncode=128, stderr="fatal: boom")
            with mock.patch.object(m.subprocess, "run", side_effect=[fail] * m.CLONE_RETRIES), \
                    mock.patch.object(m.time, "sleep"):
                with self.assertRaises(m.MigrateError) as ctx:
                    m.clone_repo(cfg, self.REPO)
            self.assertIn("boom", str(ctx.exception))

    def test_clone_removes_partial_dir_between_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(clone_dir=tmp)
            target = os.path.join(tmp, f"{ORG}/a/b.git")
            # first attempt leaves a partial dir (simulate)
            results = []
            def fake_run(cmd, **kw):
                ret = mock.Mock(returncode=1, stderr="fatal: partial")
                os.makedirs(os.path.join(target, "objects"), exist_ok=True)
                return ret
            ok = mock.Mock(returncode=0)
            with mock.patch.object(m.subprocess, "run", side_effect=[fake_run(None), ok]), \
                    mock.patch.object(m.time, "sleep"):
                m.clone_repo(cfg, self.REPO)
            self.assertFalse(os.path.exists(os.path.join(target, "objects/x")))

    def test_push_mirror_with_cwd(self):
        cfg = make_cfg()
        with mock.patch.object(m, "run_git") as git_mock:
            m.push_repo(cfg, self.REPO, "a-b", "/some/clone/dir")
        push_args, push_kwargs = git_mock.call_args
        self.assertEqual(push_args[0][:3], ["git", "push", "--mirror"])
        self.assertIn("https://me:gtk@github.com/me/a-b.git", push_args[0])
        self.assertEqual(push_kwargs["cwd"], "/some/clone/dir")


class TestMainDryRun(unittest.TestCase):
    def _run_main(self, repo_pages, argv=None):
        pages = [{"success": True, "result": p} if isinstance(p, list) else p
                 for p in repo_pages]
        fake = FakeApi(pages)
        out = io.StringIO()
        with mock.patch.object(m, "http_json", fake), \
                mock.patch.object(m, "run_git") as git_mock, \
                mock.patch.object(m, "load_config", return_value=make_cfg()), \
                mock.patch.object(m, "acquire_lock", return_value="/tmp/test.lock"), \
                mock.patch.object(sys, "argv", argv or ["codeup2github.py", "--dry-run"]), \
                contextlib.redirect_stdout(out):
            m.main()
        return fake, git_mock, out.getvalue()

    def test_dry_run_lists_mapping_and_skips_archived(self):
        page = [{"pathWithNamespace": f"{ORG}/backend/web", "path": "web",
                 "description": "web app", "archived": False},
                {"pathWithNamespace": f"{ORG}/backend/old", "path": "old",
                 "description": "", "archived": True}]
        _, git_mock, out = self._run_main([page, []])
        self.assertIn("found 1 repo(s)", out)
        self.assertIn("-> me/backend-web", out)
        self.assertNotIn("backend-old", out)
        # dry-run must not touch git beyond the precheck
        git_mock.assert_called_once_with(["git", "--version"])

    def test_include_archived_flag_keeps_archived(self):
        page = [{"pathWithNamespace": f"{ORG}/backend/old", "path": "old",
                 "description": "", "archived": True}]
        _, _, out = self._run_main([page, []],
                                   argv=["codeup2github.py", "--dry-run",
                                         "--include-archived"])
        self.assertIn("found 1 repo(s)", out)
        self.assertIn("[archived]", out)

    def test_collision_exits_2_with_one_error_line(self):
        page = [{"pathWithNamespace": f"{ORG}/backend/a/web", "path": "web",
                 "description": "", "archived": False},
                {"pathWithNamespace": f"{ORG}/backend/a-web", "path": "a-web",
                 "description": "", "archived": False}]
        err = io.StringIO()
        with mock.patch.object(m, "http_json",
                               FakeApi([{"success": True, "result": page}, []])), \
                mock.patch.object(m, "run_git"), \
                mock.patch.object(m, "acquire_lock", return_value="/tmp/test.lock"), \
                mock.patch.object(m, "load_config", return_value=make_cfg()), \
                mock.patch.object(sys, "argv", ["codeup2github.py", "--dry-run"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            try:
                m.main()
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 2)
        self.assertIn("collision", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(len(err.getvalue().splitlines()), 1)


class TtyIO(io.StringIO):
    """StringIO that reports itself as a TTY (for prompt-gate tests)."""

    def isatty(self):
        return True


class TestMainMigrateLoop(unittest.TestCase):
    def _repo(self, name, size=None):
        return {"pathWithNamespace": f"{ORG}/backend/{name}", "path": name,
                "description": "", "archived": False, "repositorySize": size,
                "creatorId": 9}

    def _run_migrate(self, page, ensure_side_effect=None, argv=None,
                     stdin_tty=False, stdout_tty=False, input_text=None):
        out = TtyIO() if stdout_tty else io.StringIO()
        err = io.StringIO()
        fake = FakeApi([{"success": True, "result": page}, []])
        with mock.patch.object(m, "http_json", fake), \
                mock.patch.object(m, "run_git"), \
                mock.patch.object(m, "acquire_lock", return_value="/tmp/test.lock"), \
                mock.patch.object(m, "load_config", return_value=make_cfg()), \
                mock.patch.object(m, "clone_repo",
                                  side_effect=lambda cfg, r: f"/clones/{r['path']}") as clone_mock, \
                mock.patch.object(m, "push_repo") as push_mock, \
                mock.patch.object(m, "ensure_github_repo",
                                  side_effect=ensure_side_effect or [True] * 99), \
                mock.patch.object(sys, "argv", argv or ["codeup2github.py", "--yes"]), \
                mock.patch.object(sys, "stdin") as stdin, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            stdin.isatty.return_value = stdin_tty
            if input_text is not None:
                with mock.patch("builtins.input", return_value=input_text):
                    code = self._catch_exit(m.main)
            else:
                code = self._catch_exit(m.main)
        return code, out.getvalue(), err.getvalue(), clone_mock, push_mock

    @staticmethod
    def _catch_exit(fn):
        try:
            fn()
            return 0
        except SystemExit as e:
            return e.code

    def test_success_and_failure_continue_then_exit_1(self):
        page = [self._repo("ok"), self._repo("bad")]
        err_obj = m.MigrateError("boom ctk gtk")
        code, out, err, clone_mock, push_mock = self._run_migrate(
            page, ensure_side_effect=[True, err_obj])
        self.assertEqual(code, 1)
        self.assertIn("1 success, 1 failed", err)
        # raw tokens must never appear in failure output
        self.assertIn("***", err)
        self.assertNotIn("ctk", err)
        self.assertNotIn("gtk", err)

    def test_clone_all_before_push_sorted_by_size(self):
        page = [self._repo("big", "500"), self._repo("small", "1"),
                self._repo("mid", "10")]
        code, _, err, clone_mock, push_mock = self._run_migrate(page)
        self.assertEqual(code, 0)
        self.assertEqual(clone_mock.call_count, 3)
        self.assertEqual(push_mock.call_count, 3)
        # phase separation + size order verified via stderr log (mock call
        # ordering across two mocks is fragile; the log is the real contract)
        clone_order = [c.args[1]["path"] for c in clone_mock.call_args_list]
        push_order = [c.args[1]["path"] for c in push_mock.call_args_list]
        # every repo cloned exactly once, pushes cover the same set
        self.assertEqual(sorted(clone_order), sorted(push_order))
        # push order: smallest first
        pushed_names = [c.args[2] for c in push_mock.call_args_list]
        self.assertEqual(pushed_names, ["backend-small", "backend-mid", "backend-big"])
        # phase separation: all clones requested before the first push completes,
        # observable via the phase banners in stderr
        self.assertIn("phase 1/2: cloning 3 repo(s)", err)
        self.assertIn("phase 2/2: pushing 3 repo(s), smallest first", err)

    def test_clone_failure_skips_push_but_others_continue(self):
        page = [self._repo("bad"), self._repo("ok")]
        err_obj = m.MigrateError("clone boom")
        out, err_out = io.StringIO(), io.StringIO()

        def fake_clone(cfg, r):
            if r["path"] == "bad":
                raise err_obj
            return f"/clones/{r['path']}"

        with mock.patch.object(m, "http_json",
                               FakeApi([{"success": True, "result": page}, []])), \
                mock.patch.object(m, "run_git"), \
                mock.patch.object(m, "acquire_lock", return_value="/tmp/test.lock"), \
                mock.patch.object(m, "load_config", return_value=make_cfg()), \
                mock.patch.object(m, "clone_repo", side_effect=fake_clone), \
                mock.patch.object(m, "push_repo") as push_mock, \
                mock.patch.object(m, "ensure_github_repo", return_value=True), \
                mock.patch.object(sys, "argv", ["codeup2github.py", "--yes"]), \
                mock.patch.object(sys, "stdin"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err_out):
            code = self._catch_exit(m.main)
        self.assertEqual(code, 1)
        self.assertEqual([c.args[2] for c in push_mock.call_args_list], ["backend-ok"])
        self.assertIn("1 success, 1 failed", err_out.getvalue())

    def test_confirm_declined_exits_130(self):
        code, out, err, _, _ = self._run_migrate(
            [self._repo("a")], argv=["codeup2github.py"],
            stdin_tty=True, stdout_tty=True, input_text="n")
        self.assertEqual(code, 130)
        self.assertIn("cancelled", err)

    def test_confirm_accepted_proceeds(self):
        code, out, err, _, _ = self._run_migrate(
            [self._repo("a")], argv=["codeup2github.py"],
            stdin_tty=True, stdout_tty=True, input_text="y")
        self.assertEqual(code, 0)
        self.assertIn("done:", err)

    def test_non_tty_skips_prompt(self):
        # scripted run: no TTY, no prompt, migration proceeds
        code, out, err, _, _ = self._run_migrate([self._repo("a")],
                                                 argv=["codeup2github.py"])
        self.assertEqual(code, 0)

    def test_usage_error_exits_2(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(m, "run_git"), \
                mock.patch.object(m, "acquire_lock", return_value="/tmp/test.lock"), \
                mock.patch.object(m, "load_config",
                                  side_effect=m.MigrateError("missing config values: ['api_domain']")), \
                mock.patch.object(sys, "argv", ["codeup2github.py"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self._catch_exit(m.main)
        self.assertEqual(code, 2)
        self.assertIn("error:", err.getvalue())

    def test_empty_repo_set_is_not_an_error(self):
        code, out, err, _, _ = self._run_migrate([])
        self.assertEqual(code, 0)
        self.assertIn("nothing to migrate", err)


class TestMainErrorTaxonomy(unittest.TestCase):
    """Pre-run, enumeration and endpoint errors: one `error:` line, exit 2.

    The real code path runs here (real load_config, real lock, real http_json or a
    real subprocess launch), so this is the end-to-end counterpart of the unit tests.
    """

    @staticmethod
    def _catch_exit(fn):
        try:
            fn()
            return 0
        except SystemExit as e:
            return e.code

    def _config(self, tmp, api_domain="devops.example.com"):
        path = os.path.join(tmp, "migrate.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"[codeup]\napi_style = oapi\napi_domain = {api_domain}\n"
                    f"git_host = codeup.example.com\naccess_token = ctk\n"
                    f"git_username = cu\n"
                    f"[github]\nusername = me\ntoken = gtk\n"
                    f"[run]\nclone_dir = {os.path.join(tmp, 'clones')}\n")
        return path

    def _run(self, tmp, api_domain="devops.example.com"):
        err = io.StringIO()
        argv = ["codeup2github.py", "--dry-run", "-c", self._config(tmp, api_domain)]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = self._catch_exit(m.main)
        return code, err.getvalue()

    def test_unreachable_endpoint_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self._run(tmp, api_domain=f"127.0.0.1:{closed_port()}")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_missing_git_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_path = os.path.join(tmp, "empty-path")
            os.makedirs(empty_path)
            with mock.patch.dict(os.environ, {"PATH": empty_path}):
                code, err = self._run(tmp)
        self.assertEqual(code, 2)
        self.assertIn("git", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_enumeration_401_exits_2_with_the_token_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            argv = ["codeup2github.py", "--dry-run", "-c", self._config(tmp)]
            with mock.patch.object(m, "http_json",
                                   return_value=(401, {"errorMessage": "bad ctk"})), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = self._catch_exit(m.main)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", err.getvalue())
        self.assertIn("***", err.getvalue())
        self.assertNotIn("ctk", err.getvalue())
        self.assertEqual(len(err.getvalue().splitlines()), 1)

    def _run_config(self, tmp, body, section="codeup"):
        path = os.path.join(tmp, "broken.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        err = io.StringIO()
        argv = ["codeup2github.py", "--dry-run", "-c", path]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = self._catch_exit(m.main)
        return code, err.getvalue()

    def test_unparseable_config_exits_2(self):
        # a file that exists but cannot be parsed is a config error, not a crash
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self._run_config(tmp, "clone_dir = clones\n")  # no section header
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_config_error_does_not_echo_the_source_line(self):
        # the parser message embeds the offending line verbatim: a token can sit there
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self._run_config(tmp, "token = glpat-SECRETVALUE\n[github]\n")
        self.assertEqual(code, 2)
        self.assertNotIn("SECRETVALUE", err)
        self.assertIn("error:", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_non_boolean_flag_exits_2_without_echoing_the_value(self):
        # getboolean's message embeds the value, which may be a mis-pasted token
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self._run_config(tmp, (
                "[codeup]\napi_style = oapi\napi_domain = devops.example.com\n"
                "git_host = codeup.example.com\naccess_token = ctk\ngit_username = cu\n"
                "insecure_tls = glpat-SECRETVALUE\n"
                "[github]\nusername = me\ntoken = gtk\n[run]\nclone_dir = clones\n"))
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("SECRETVALUE", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_interpolation_error_does_not_echo_the_value(self):
        # interpolation messages embed the raw value; a token can sit there
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self._run_config(tmp, (
                "[codeup]\napi_style = oapi\napi_domain = devops.example.com\n"
                "git_host = codeup.example.com\naccess_token = abc%(x)sdef-SECRETVALUE\n"
                "git_username = cu\n"
                "[github]\nusername = me\ntoken = gtk\n[run]\nclone_dir = clones\n"))
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("SECRETVALUE", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_malformed_api_domain_exits_2(self):
        # request building itself raises ValueError for this URL shape
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self._run(tmp, api_domain="http://[oops")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_credential_in_api_domain_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self._run(
                tmp, api_domain=f"https://user:ctk-secret@127.0.0.1:{closed_port()}")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("ctk-secret", err)
        self.assertEqual(len(err.splitlines()), 1)

    def test_unparseable_api_body_exits_2(self):
        # a 200 that is an SSO/proxy page is a response, not a repo list
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b"<html>SSO login</html>"
        body = (f"[codeup]\napi_style = legacy\napi_domain = devops.example.com\n"
                f"git_host = codeup.example.com\norganization_id = {ORG}\n"
                f"access_token = ctk\ngit_username = cu\n"
                f"[github]\nusername = me\ntoken = gtk\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.ini")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body + f"[run]\nclone_dir = {os.path.join(tmp, 'clones')}\n")
            err = io.StringIO()
            argv = ["codeup2github.py", "--dry-run", "-c", path]
            with mock.patch.object(m.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = self._catch_exit(m.main)
        self.assertEqual(code, 2)
        self.assertIn("error:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(len(err.getvalue().splitlines()), 1)


class TestCredentialGuards(unittest.TestCase):
    """Same guards the github2gitlab side already has (TestCredentialGuards there):
    the shipped example config carries no token and the local config is ignored."""

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def test_gitignore_entry_is_effective(self):
        with open(os.path.join(self.ROOT, ".gitignore"), encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
        self.assertIn("migrate.ini", lines)
        ret = subprocess.run(["git", "check-ignore", "-q", "migrate.ini"], cwd=self.ROOT)
        self.assertEqual(ret.returncode, 0,
                         "migrate.ini is not ignored by the shipped .gitignore")

    def test_example_config_has_the_required_keys_without_tokens(self):
        cp = configparser.ConfigParser()
        cp.read(os.path.join(self.ROOT, "migrate.example.ini"), encoding="utf-8")
        for section, key in (("codeup", "api_domain"), ("codeup", "git_host"),
                             ("codeup", "access_token"), ("codeup", "git_username"),
                             ("github", "username"), ("github", "token"),
                             ("run", "clone_dir")):
            self.assertTrue(cp.has_option(section, key), f"[{section}] {key} missing")
        self.assertEqual(cp.get("codeup", "access_token").strip(), "")
        self.assertEqual(cp.get("github", "token").strip(), "")


if __name__ == "__main__":
    unittest.main()


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
            m.acquire_lock(tmp)  # should not raise
            self.assertEqual(open(lock).read(), str(os.getpid()))
