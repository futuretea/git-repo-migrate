"""Unit tests for verify_migration (no network; local git fixtures only).

Run: python3 -m unittest -q test_verify_migration
"""
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

import verify_migration as m  # noqa: E402
import github2gitlab as g2g  # noqa: E402  (patched alongside: verify_migration
# imports its helpers, and those resolve http_json in their own module globals)

GHE = "https://ghe.example.com"
GL = "https://gitlab.example.com"

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def closed_port():
    """A just-released loopback port: connecting to it is refused."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


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


def make_bare_repo(tmp, name="demo", bare_dir=None, branch="main", extra_branch=None):
    """Bare repo with one branch, an annotated tag and a lightweight tag;
    returns (path, main_sha)."""
    bare_dir = bare_dir or tmp
    os.makedirs(bare_dir, exist_ok=True)
    bare = os.path.join(bare_dir, name + ".git")
    work = os.path.join(tmp, name + "-work")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True, capture_output=True)

    def wgit(*args):
        subprocess.run(["git", "-C", work] + list(args), check=True,
                       capture_output=True, env=GIT_ENV)

    subprocess.run(["git", "clone", "-q", bare, work], check=True, capture_output=True)
    wgit("commit", "-q", "--allow-empty", "-m", "init")
    wgit("branch", "-M", branch)
    wgit("tag", "-a", "v1", "-m", "rel")
    wgit("tag", "v2")  # lightweight: no tag object, the ref holds the commit
    wgit("push", "-q", "origin", branch, "--tags")
    if extra_branch:
        wgit("checkout", "-q", "-b", extra_branch)
        wgit("commit", "-q", "--allow-empty", "-m", extra_branch)
        wgit("push", "-q", "origin", extra_branch)
    sha = subprocess.run(["git", "-C", work, "rev-parse", branch],
                         capture_output=True, text=True).stdout.strip()
    return bare, sha


def make_empty_bare_repo(tmp, name="empty", bare_dir=None):
    bare_dir = bare_dir or tmp
    os.makedirs(bare_dir, exist_ok=True)
    bare = os.path.join(bare_dir, name + ".git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True, capture_output=True)
    return bare


def write_ini(tmp, clone_dir, sections=("github", "gitlab"), verify=None,
              manifest="auto", name="cfg.ini"):
    """Write a verifier config; returns its path."""
    body = []
    if "github" in sections:
        body += ["[github]", "base_url = ghe.example.com", "token = gtk", ""]
    if "gitlab" in sections:
        body += ["[gitlab]", "base_url = gitlab.example.com", "token = ltk", ""]
    body += ["[run]", f"clone_dir = {clone_dir}", ""]
    body += ["[verify]"]
    if verify is None:
        verify = {"source": "local-bare", "target": "gitlab", "manifest": manifest}
    body += [f"{k} = {v}" for k, v in verify.items()]
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    return path


class TestLoadVerifyConfig(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("GHE_TOKEN", "GITLAB_TOKEN", "GITHUB_TOKEN", "CODEUP_ACCESS_TOKEN"):
            os.environ.pop(name, None)

    def test_missing_file(self):
        with self.assertRaises(m.MigrateError):
            m.load_verify_config("no-such-file.ini")

    def test_defaults_when_verify_section_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.ini")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[github]\nbase_url = ghe.example.com\ntoken = gtk\n"
                        "[gitlab]\nbase_url = gitlab.example.com\ntoken = ltk\n"
                        "[run]\nclone_dir = clones\n")
            cfg = m.load_verify_config(path)
        self.assertEqual(cfg["source"], "local-bare")
        self.assertEqual(cfg["target"], "gitlab")
        self.assertEqual(cfg["manifest"], os.path.join("clones", m.MANIFEST_NAME))

    def test_explicit_values_and_manifest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ini(tmp, "clones", verify={"source": "api", "target": "github",
                                                    "manifest": "run.tsv"})
            cfg = m.load_verify_config(path)
        self.assertEqual((cfg["source"], cfg["target"], cfg["manifest"]),
                         ("api", "github", "run.tsv"))
        self.assertEqual(cfg["gh_api_base"], f"{GHE}/api/v3")
        self.assertEqual(cfg["gl_api_base"], f"{GL}/api/v4")

    def test_invalid_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ini(tmp, "clones", verify={"source": "svn", "target": "gitlab"})
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_verify_config(path)
        self.assertIn("source", str(ctx.exception))

    def test_invalid_target_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ini(tmp, "clones", verify={"source": "local-bare", "target": "cvs"})
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_verify_config(path)
        self.assertIn("target", str(ctx.exception))

    def test_missing_clone_dir_reported_by_ini_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nocd.ini")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[github]\nbase_url = ghe.example.com\ntoken = gtk\n"
                        "[gitlab]\nbase_url = gitlab.example.com\ntoken = ltk\n[run]\n")
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_verify_config(path)
        self.assertIn("run:clone_dir", str(ctx.exception))

    def test_local_bare_source_does_not_need_the_github_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ini(tmp, "clones", sections=("gitlab",))
            cfg = m.load_verify_config(path)
        self.assertEqual(cfg["source"], "local-bare")
        self.assertEqual(cfg["gh_base_url"], "")

    def test_api_source_requires_github_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ini(tmp, "clones", sections=("gitlab",),
                             verify={"source": "api", "target": "gitlab"})
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_verify_config(path)
        msg = str(ctx.exception)
        self.assertIn("github:base_url", msg)
        self.assertIn("github:token", msg)

    def test_github_target_does_not_need_the_gitlab_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ini(tmp, "clones", sections=("github",),
                             verify={"source": "local-bare", "target": "github"})
            cfg = m.load_verify_config(path)
        self.assertEqual(cfg["target"], "github")

    def test_gitlab_target_requires_gitlab_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ini(tmp, "clones", sections=("github",),
                             verify={"source": "local-bare", "target": "gitlab"})
            with self.assertRaises(m.MigrateError) as ctx:
                m.load_verify_config(path)
        self.assertIn("gitlab:token", str(ctx.exception))

    def test_env_tokens_used_when_ini_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env.ini")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[github]\nbase_url = ghe.example.com\n"
                        "[gitlab]\nbase_url = gitlab.example.com\n"
                        "[run]\nclone_dir = clones\n")
            with mock.patch.dict(os.environ, {"GHE_TOKEN": "env-gtk",
                                              "GITLAB_TOKEN": "env-ltk"}):
                cfg = m.load_verify_config(path)
        self.assertEqual(cfg["gh_token"], "env-gtk")
        self.assertEqual(cfg["gl_token"], "env-ltk")


class TestReadManifest(unittest.TestCase):
    def _write(self, tmp, content):
        path = os.path.join(tmp, "migrate-manifest.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_reads_name_and_target_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "# name\ttarget_path\tsource_private\tresult\n"
                                     "codeup-Demo-App\tme/codeup-demo-app\tprivate\tok\n"
                                     "\ndemo\tme/demo\tpublic\tfailed\n")
            self.assertEqual(m.read_manifest(path), [
                ("codeup-Demo-App", "me/codeup-demo-app"),
                ("demo", "me/demo")])

    def test_malformed_line_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "just-a-name\n")
            with self.assertRaises(m.MigrateError):
                m.read_manifest(path)

    def test_missing_manifest_reported(self):
        with self.assertRaises(OSError):
            m.read_manifest("/no/such/manifest.txt")


class TestRefsLocal(unittest.TestCase):
    def test_branches_and_annotated_tags_dereferenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare, sha = make_bare_repo(tmp, extra_branch="side")
            refs = m.refs_local(bare)
        self.assertEqual(refs["refs/heads/main"], sha)
        self.assertIn("refs/heads/side", refs)
        # the annotated tag must resolve to the commit, not the tag object
        self.assertEqual(refs["refs/tags/v1"], sha)
        # the lightweight tag has no tag object: the ref itself is the commit
        self.assertEqual(refs["refs/tags/v2"], sha)

    def test_empty_bare_repo_has_zero_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = make_empty_bare_repo(tmp)
            self.assertEqual(m.refs_local(bare), {})

    def test_missing_dir_raises(self):
        with self.assertRaises(m.MigrateError):
            m.refs_local("/no/such/repo.git")

    def test_not_a_git_dir_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(m.MigrateError):
                m.refs_local(tmp)

    def test_git_unavailable_raises_migrate_error(self):
        # the fixture is built first: only git itself is made unavailable
        with tempfile.TemporaryDirectory() as tmp:
            bare, _ = make_bare_repo(tmp)
            empty_path = os.path.join(tmp, "empty-path")
            os.makedirs(empty_path)
            with mock.patch.dict(os.environ, {"PATH": empty_path}):
                with self.assertRaises(m.MigrateError) as ctx:
                    m.refs_local(bare)
            self.assertIn("git", str(ctx.exception))


class TestRefsGitlab(unittest.TestCase):
    CFG = {"gl_api_base": f"{GL}/api/v4", "gl_token": "ltk"}

    def test_branches_and_tags_use_commit_id(self):
        fake = FakeApi([(200, [{"name": "main", "commit": {"id": "a" * 40},
                                "target": "t" * 40}]),
                        (200, [{"name": "v1", "commit": {"id": "b" * 40},
                                "target": "object-id"}])])
        with mock.patch.object(m, "http_json", fake):
            refs, err = m.gitlab_refs(self.CFG, "me/codeup-demo-app")
        self.assertIsNone(err)
        self.assertEqual(refs, {"refs/heads/main": "a" * 40, "refs/tags/v1": "b" * 40})
        self.assertIn("/projects/me%2Fcodeup-demo-app/repository/branches",
                      fake.calls[0][1])
        self.assertIn("per_page=100", fake.calls[0][1])
        self.assertEqual(fake.calls[0][2]["PRIVATE-TOKEN"], "ltk")

    def test_paginates_until_short_page(self):
        full = [{"name": f"b{i}", "commit": {"id": "a" * 40}} for i in range(m.PAGE_SIZE)]
        fake = FakeApi([(200, full), (200, [{"name": "last", "commit": {"id": "c" * 40}}]),
                        (200, [])])
        with mock.patch.object(m, "http_json", fake):
            refs, err = m.gitlab_refs(self.CFG, "me/x")
        self.assertIsNone(err)
        self.assertEqual(len(refs), m.PAGE_SIZE + 1)
        self.assertIn("page=2", fake.calls[1][1])

    def test_http_error_unreachable(self):
        with mock.patch.object(m, "http_json", FakeApi([(404, {"message": "404"})])):
            refs, err = m.gitlab_refs(self.CFG, "me/x")
        self.assertIsNone(refs)
        self.assertIn("404", err)

    def test_empty_project_has_zero_refs(self):
        fake = FakeApi([(200, []), (200, [])])
        with mock.patch.object(m, "http_json", fake):
            refs, err = m.gitlab_refs(self.CFG, "me/x")
        self.assertEqual((refs, err), ({}, None))


class TestRefsGithub(unittest.TestCase):
    """github_refs is defined in github2gitlab, so its http_json is patched there."""
    CFG = {"gh_api_base": f"{GHE}/api/v3", "gh_token": "gtk"}

    def test_branches_and_tags_use_commit_sha(self):
        fake = FakeApi([(200, [{"name": "main", "commit": {"sha": "a" * 40}}]),
                        (200, [{"name": "v1", "commit": {"sha": "b" * 40}}])])
        with mock.patch.object(g2g, "http_json", fake):
            refs, err = m.github_refs(self.CFG, "me/demo")
        self.assertIsNone(err)
        self.assertEqual(refs, {"refs/heads/main": "a" * 40, "refs/tags/v1": "b" * 40})
        self.assertIn("/repos/me/demo/branches", fake.calls[0][1])
        self.assertEqual(fake.calls[0][2]["Authorization"], "Bearer gtk")

    def test_http_error_unreachable(self):
        with mock.patch.object(g2g, "http_json", FakeApi([(500, "boom")])):
            refs, err = m.github_refs(self.CFG, "me/demo")
        self.assertIsNone(refs)
        self.assertIn("500", err)


class TestClassify(unittest.TestCase):
    def test_identical_is_ok(self):
        refs = {"refs/heads/main": "a" * 40, "refs/tags/v1": "b" * 40}
        self.assertEqual(m.classify(refs, dict(refs)), [])

    def test_both_sides_empty_is_ok(self):
        self.assertEqual(m.classify({}, {}), [])

    def test_reports_missing_extra_and_sha_shift(self):
        src = {"refs/heads/main": "a" * 40, "refs/tags/v1": "b" * 40}
        dst = {"refs/heads/main": "c" * 40, "refs/tags/v2": "d" * 40}
        diffs = m.classify(src, dst)
        self.assertIn("-missing refs/tags/v1", diffs)
        self.assertIn(f"+extra refs/tags/v2", diffs)
        self.assertEqual(len(diffs), 3)
        self.assertTrue(any(d.startswith("~sha refs/heads/main:") for d in diffs))


class TestSelftest(unittest.TestCase):
    def test_selftest_passes_offline(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.selftest()
        self.assertIn("selftest OK", out.getvalue())


class TestMain(unittest.TestCase):
    """End-to-end verifier runs against local fixtures and a scripted API."""

    def _run(self, responses, tmp, argv=None, source="local-bare", target="gitlab",
             sections=("github", "gitlab"), manifest="auto", manifest_body=None):
        clone_dir = os.path.join(tmp, "clones")
        os.makedirs(clone_dir, exist_ok=True)
        if manifest_body is not None:
            if manifest == "auto":
                manifest_path = os.path.join(clone_dir, m.MANIFEST_NAME)
            else:
                manifest_path = os.path.join(tmp, manifest)
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write(manifest_body)
        cfg_path = write_ini(tmp, clone_dir, sections=sections, manifest=manifest,
                             verify={"source": source, "target": target,
                                     "manifest": manifest})
        fake = FakeApi(responses)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(m, "http_json", fake), \
                mock.patch.object(g2g, "http_json", fake), \
                mock.patch.object(sys, "argv", argv or ["verify_migration.py", "-c", cfg_path]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self._catch_exit(m.main)
        return code, out.getvalue(), err.getvalue(), fake

    def _run_live(self, tmp, manifest_body=None, manifest="auto", source="local-bare",
                  target="gitlab", gl_base_url="gitlab.example.com"):
        """Like _run, but with the real http_json: the transport stack runs.

        Only sys.argv and stdio are patched, so an endpoint that cannot be
        reached fails inside urllib exactly as it does in production.
        """
        clone_dir = os.path.join(tmp, "clones")
        os.makedirs(clone_dir, exist_ok=True)
        if manifest_body is not None:
            manifest_path = (os.path.join(clone_dir, m.MANIFEST_NAME)
                             if manifest == "auto" else os.path.join(tmp, manifest))
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write(manifest_body)
        cfg_path = os.path.join(tmp, "live.ini")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(f"[github]\nbase_url = ghe.example.com\ntoken = gtk\n\n"
                    f"[gitlab]\nbase_url = {gl_base_url}\ntoken = ltk\n\n"
                    f"[run]\nclone_dir = {clone_dir}\n\n"
                    f"[verify]\nsource = {source}\ntarget = {target}\n"
                    f"manifest = {manifest}\n")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["verify_migration.py", "-c", cfg_path]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self._catch_exit(m.main)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _catch_exit(fn):
        try:
            fn()
            return 0
        except SystemExit as e:
            return e.code

    @staticmethod
    def clones(tmp):
        return os.path.join(tmp, "clones")

    def test_ok_when_both_sides_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sha = make_bare_repo(tmp, "demo", bare_dir=self.clones(tmp))
            # the fixture carries both an annotated (v1) and a lightweight (v2)
            # tag: an annotated-only implementation would report a false mismatch
            code, out, err, fake = self._run(
                [(200, [{"name": "main", "commit": {"id": sha}}]),
                 (200, [{"name": "v1", "commit": {"id": sha}},
                        {"name": "v2", "commit": {"id": sha}}])],
                tmp, manifest_body="demo\tme/demo\tprivate\tok\n")
            self.assertIn("OK (refs identical): 1", out)
            self.assertIn("MISMATCH: 0", out)
            self.assertIn("UNREACHABLE: 0", out)
            self.assertEqual([c[0] for c in fake.calls], ["GET", "GET"])
        self.assertEqual(code, 0)

    def test_mismatch_exits_1_with_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bare_repo(tmp, "demo", bare_dir=self.clones(tmp))
            code, out, _, _ = self._run(
                [(200, [{"name": "main", "commit": {"id": "0" * 40}}]), (200, [])],
                tmp, manifest_body="demo\tme/demo\tprivate\tok\n")
            self.assertIn("MISMATCH: 1", out)
            self.assertIn("~sha refs/heads/main", out)
            self.assertIn("-missing refs/tags/v1", out)
        self.assertEqual(code, 1)

    def test_missing_local_bare_dir_is_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _, fake = self._run(
                [], tmp, manifest_body="vanished\tme/vanished\tprivate\tok\n")
            self.assertIn("UNREACHABLE: 1", out)
            self.assertIn("vanished", out)
            # no target query for a source that cannot be read
            self.assertEqual(fake.calls, [])
        self.assertEqual(code, 1)

    def test_manifest_drives_the_denominator_not_the_clone_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bare_repo(tmp, "demo", bare_dir=self.clones(tmp))
            make_bare_repo(tmp, "leftover", bare_dir=self.clones(tmp))
            code, out, _, _ = self._run(
                [(200, [{"name": "main", "commit": {"id": "a" * 40}}]), (200, [])],
                tmp, manifest_body="demo\tme/demo\tprivate\tok\n")
            self.assertNotIn("leftover", out)
        self.assertEqual(code, 1)

    def test_target_api_error_is_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bare_repo(tmp, "demo", bare_dir=self.clones(tmp))
            code, out, _, _ = self._run([(404, {"message": "404"})], tmp,
                                        manifest_body="demo\tme/demo\tprivate\tok\n")
            self.assertIn("UNREACHABLE: 1", out)
            self.assertIn("404", out)
        self.assertEqual(code, 1)

    def test_empty_sets_on_both_sides_are_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_empty_bare_repo(tmp, "empty", bare_dir=self.clones(tmp))
            code, out, _, _ = self._run([(200, []), (200, [])], tmp,
                                        manifest_body="empty\tme/empty\tprivate\tok\n")
            self.assertIn("OK (refs identical): 1", out)
        self.assertEqual(code, 0)

    def test_api_source_uses_source_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _, fake = self._run(
                [(200, {"login": "srcuser"}),
                 (200, [{"name": "main", "commit": {"sha": "a" * 40}}]),
                 (200, []),
                 (200, [{"name": "main", "commit": {"id": "a" * 40}}]),
                 (200, [])],
                tmp, source="api", manifest_body="demo\tme/demo\tprivate\tok\n")
            self.assertIn("/user", fake.calls[0][1])
            self.assertIn("/repos/srcuser/demo/branches", fake.calls[1][1])
            self.assertIn("OK (refs identical): 1", out)
        self.assertEqual(code, 0)

    def test_github_target_uses_the_github_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bare_repo(tmp, "demo", bare_dir=self.clones(tmp))
            code, out, _, fake = self._run(
                [(200, [{"name": "main", "commit": {"sha": "a" * 40}}]), (200, [])],
                tmp, target="github", sections=("github",),
                manifest_body="demo\tme/demo\tprivate\tok\n")
            self.assertIn("/api/v3/repos/me/demo/branches", fake.calls[0][1])
            self.assertIn("MISMATCH: 1", out)
        self.assertEqual(code, 1)

    def test_missing_manifest_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err, _ = self._run([], tmp)
            self.assertIn("error:", err)
        self.assertEqual(code, 2)

    def test_malformed_ini_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "malformed.ini")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("clone_dir = clones\n")  # no [section] header
            err = io.StringIO()
            with mock.patch.object(sys, "argv",
                                   ["verify_migration.py", "-c", cfg_path]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = self._catch_exit(m.main)
            self.assertEqual(code, 2)
            self.assertIn("error:", err.getvalue())

    def test_malformed_ini_does_not_echo_the_source_line(self):
        # configparser embeds the offending line verbatim and a token can sit there
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "secret.ini")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("token = glpat-SECRETVALUE\n")  # no [section] header
            err = io.StringIO()
            with mock.patch.object(sys, "argv",
                                   ["verify_migration.py", "-c", cfg_path]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = self._catch_exit(m.main)
            self.assertEqual(code, 2)
            self.assertIn("error:", err.getvalue())
            self.assertNotIn("SECRETVALUE", err.getvalue())
            self.assertEqual(len(err.getvalue().splitlines()), 1)

    def test_non_utf8_config_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "binary.ini")
            with open(cfg_path, "wb") as f:
                f.write(b"[gitlab]\nbase_url = gitlab.example.com\ntoken = \xff\xfe\n")
            err = io.StringIO()
            with mock.patch.object(sys, "argv",
                                   ["verify_migration.py", "-c", cfg_path]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = self._catch_exit(m.main)
            self.assertEqual(code, 2)
            self.assertIn("error:", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            self.assertEqual(len(err.getvalue().splitlines()), 1)

    def test_non_utf8_manifest_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = os.path.join(tmp, "clones")
            os.makedirs(clone_dir, exist_ok=True)
            with open(os.path.join(clone_dir, m.MANIFEST_NAME), "wb") as f:
                f.write(b"demo\tme/demo\tprivate\tok\n\xff\xfe\n")
            cfg_path = write_ini(tmp, clone_dir)
            err = io.StringIO()
            with mock.patch.object(sys, "argv",
                                   ["verify_migration.py", "-c", cfg_path]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = self._catch_exit(m.main)
            self.assertEqual(code, 2)
            self.assertIn("error:", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_unreachable_target_is_unreachable_not_an_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bare_repo(tmp, "demo", bare_dir=self.clones(tmp))
            code, out, err = self._run_live(
                tmp, manifest_body="demo\tme/demo\tprivate\tok\n",
                gl_base_url=f"127.0.0.1:{closed_port()}")
            self.assertIn("OK (refs identical): 0", out)
            self.assertIn("MISMATCH: 0", out)
            self.assertIn("UNREACHABLE: 1", out)
            self.assertIn("demo", out)
            self.assertNotIn("Traceback", err)
        self.assertEqual(code, 1)

    def test_git_unavailable_is_unreachable_not_an_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bare_repo(tmp, "demo", bare_dir=self.clones(tmp))
            empty_path = os.path.join(tmp, "empty-path")
            os.makedirs(empty_path)
            with mock.patch.dict(os.environ, {"PATH": empty_path}):
                code, out, err = self._run_live(
                    tmp, manifest_body="demo\tme/demo\tprivate\tok\n")
            self.assertIn("OK (refs identical): 0", out)
            self.assertIn("MISMATCH: 0", out)
            self.assertIn("UNREACHABLE: 1", out)
            self.assertIn("demo", out)
            self.assertNotIn("Traceback", err)
        self.assertEqual(code, 1)

    def test_selftest_flag_runs_without_config(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["verify_migration.py", "--selftest"]), \
                contextlib.redirect_stdout(out):
            code = self._catch_exit(m.main)
        self.assertEqual(code, 0)
        self.assertIn("selftest OK", out.getvalue())

    def test_selftest_with_git_unavailable_exits_2(self):
        # the selftest builds fixtures with git: a git that cannot be launched
        # must end as one error line, not as a FileNotFoundError traceback
        with tempfile.TemporaryDirectory() as tmp:
            empty_path = os.path.join(tmp, "empty-path")
            os.makedirs(empty_path)
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"PATH": empty_path}), \
                    mock.patch.object(sys, "argv", ["verify_migration.py", "--selftest"]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = self._catch_exit(m.main)
            self.assertEqual(code, 2)
            self.assertIn("error:", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            self.assertEqual(len(err.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
