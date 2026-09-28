#!/usr/bin/env python3
"""Config-driven post-migration verification: compare source refs with target refs.

Source: local bare clones ({clone_dir}/{name}.git) or the GitHub-family API.
Target: the GitLab API (v4) or the GitHub-family API (v3).
The repo list comes from the run manifest written by github2gitlab.py, never
from a directory scan; each repo is classified OK / MISMATCH / UNREACHABLE.

Config, HTTP and refs helpers are shared with github2gitlab.py instead of
being copied: extracting a third module would touch the tested Codeup chain
(see plan.md, key decision 1).

--selftest: verify the comparison logic itself against local fixtures (no
network), catching verifier bugs before they produce false reports.
"""
import argparse
import configparser
import os
import sys
import tempfile
import urllib.parse

from github2gitlab import (GHE_API_PATH, GITLAB_API_PATH, MANIFEST_NAME, PAGE_SIZE,
                           MigrateError, config_error_message, github_login, github_refs,
                           gl_headers, http_json, normalize_base, refs_local,
                           run_git_capture)

SOURCES = ("local-bare", "api")
TARGETS = ("gitlab", "github")


def load_verify_config(path):
    """Read [verify] plus the [github]/[gitlab]/[run] values the selection needs."""
    if not os.path.exists(path):
        raise MigrateError(f"config file not found: {path}")
    cp = configparser.ConfigParser()
    try:
        # cp.read itself raises on a malformed ini (no section header, duplicate key)
        cp.read(path, encoding="utf-8")
        cfg = {
            "source": cp.get("verify", "source", fallback="local-bare").strip().lower(),
            "target": cp.get("verify", "target", fallback="gitlab").strip().lower(),
            "manifest": cp.get("verify", "manifest", fallback="auto").strip(),
            "clone_dir": cp.get("run", "clone_dir", fallback="").strip(),
            "gh_base_url": cp.get("github", "base_url", fallback="").strip(),
            "gh_token": (cp.get("github", "token", fallback="").strip()
                         or os.environ.get("GHE_TOKEN", "").strip()),
            "gl_base_url": cp.get("gitlab", "base_url", fallback="").strip(),
            "gl_token": (cp.get("gitlab", "token", fallback="").strip()
                         or os.environ.get("GITLAB_TOKEN", "").strip()),
        }
    except (configparser.Error, ValueError) as e:
        # keep the single `error:` line, and never the quoted source line or value
        # (ValueError: a non-UTF8 config file)
        raise MigrateError(f"invalid config: {config_error_message(e)}")
    if cfg["source"] not in SOURCES:
        raise MigrateError(f"invalid source: {cfg['source']} (choose from {SOURCES})")
    if cfg["target"] not in TARGETS:
        raise MigrateError(f"invalid target: {cfg['target']} (choose from {TARGETS})")
    needed = {"run:clone_dir": cfg["clone_dir"]}
    if cfg["source"] == "api" or cfg["target"] == "github":
        needed["github:base_url"] = cfg["gh_base_url"]
        needed["github:token"] = cfg["gh_token"]
    if cfg["target"] == "gitlab":
        needed["gitlab:base_url"] = cfg["gl_base_url"]
        needed["gitlab:token"] = cfg["gl_token"]
    missing = [name for name, value in needed.items() if not value]
    if missing:
        raise MigrateError(f"missing config values: {missing} "
                           "(tokens also accept env GHE_TOKEN / GITLAB_TOKEN)")
    cfg["gh_api_base"] = (normalize_base(cfg["gh_base_url"]) + GHE_API_PATH
                          if cfg["gh_base_url"] else "")
    cfg["gl_api_base"] = (normalize_base(cfg["gl_base_url"]) + GITLAB_API_PATH
                          if cfg["gl_base_url"] else "")
    if cfg["manifest"] == "auto":
        cfg["manifest"] = os.path.join(cfg["clone_dir"], MANIFEST_NAME)
    return cfg


def read_manifest(path):
    """[(name, target_path)] from the run manifest; it is the only repo source."""
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) < 4 or not fields[0] or not fields[1]:
                    raise MigrateError(f"malformed manifest line: {line}")
                entries.append((fields[0], fields[1]))
    except UnicodeDecodeError as e:
        # a non-UTF8 manifest is a config error, not a crash
        raise MigrateError(f"cannot read manifest {path}: {config_error_message(e)}")
    return entries


def gitlab_refs(cfg, project_path):
    """{ref_name: sha} via the GitLab branches/tags API (commit.id, not target)."""
    refs = {}
    quoted = urllib.parse.quote(project_path, safe="")
    for kind, namespace in (("branches", "heads"), ("tags", "tags")):
        page = 1
        while True:
            # documented route; some instances 404 on /projects/:id/{kind}
            url = (f"{cfg['gl_api_base']}/projects/{quoted}/repository/{kind}"
                   f"?per_page={PAGE_SIZE}&page={page}")
            try:
                status, body = http_json("GET", url, gl_headers(cfg))
            except MigrateError as e:
                # endpoint unreachable: this repo is UNREACHABLE, not the whole run
                return None, str(e)
            if status != 200 or not isinstance(body, list):
                return None, f"HTTP {status} listing {kind} of {project_path}"
            for item in body:
                refs[f"refs/{namespace}/{item.get('name')}"] = \
                    (item.get("commit") or {}).get("id")
            if len(body) < PAGE_SIZE:
                break
            page += 1
    return refs, None


def classify(src, dst):
    """Diff lines between two {ref_name: sha} maps; an empty list means OK.

    Two empty sets are OK only because both sides were really read: a side that
    could not be read becomes UNREACHABLE before classification.
    """
    diffs = []
    for ref in sorted(set(src) - set(dst)):
        diffs.append(f"-missing {ref}")
    for ref in sorted(set(dst) - set(src)):
        diffs.append(f"+extra {ref}")
    for ref in sorted(set(src) & set(dst)):
        if src[ref] != dst[ref]:
            diffs.append(f"~sha {ref}: {str(src[ref])[:8]} != {str(dst[ref])[:8]}")
    return diffs


def selftest():
    """Prove the comparison logic on local fixtures: build a bare repo with a
    branch and an annotated tag, compare it against itself (must be identical),
    then against a namespace-mangled and a sha-shifted copy (both must differ)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        run_git_capture(["git", "init", "-q", "--bare", src], check=True)
        work = os.path.join(tmp, "work")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def wgit(*args):
            run_git_capture(["git", "-C", work] + list(args), env=env, check=True)

        run_git_capture(["git", "clone", "-q", src, work], check=True)
        wgit("commit", "-q", "--allow-empty", "-m", "init")
        wgit("branch", "-M", "main")
        wgit("tag", "-a", "v1", "-m", "rel")
        wgit("push", "-q", "origin", "main", "--tags")

        refs = refs_local(src)
        assert "refs/heads/main" in refs and "refs/tags/v1" in refs, f"refs missing: {refs}"
        commit_sha = run_git_capture(["git", "-C", work, "rev-parse", "main"]).stdout.strip()
        assert refs["refs/tags/v1"] == commit_sha, "annotated tag not dereferenced"

        # identical source must compare equal
        assert classify(refs, dict(refs)) == [], "identical refs reported as different"
        # a namespace bug must be detectable
        wrong_ns = {r.replace("refs/heads/", "refs/branches/"): s for r, s in refs.items()}
        assert classify(refs, wrong_ns), "namespace bug not detected by comparison"
        # a sha shift must be detectable
        shifted = dict(refs, **{"refs/heads/main": "0" * 40})
        assert classify(refs, shifted), "sha mismatch not detected"
        print("selftest OK: comparison logic detects ns and sha differences, "
              "dereferences annotated tags")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Verify that source refs and target refs match, repo by repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s -c github2gitlab.ini       verify every repo listed in the run manifest
  %(prog)s --selftest                 prove the comparison logic offline

exit codes:
  0 all OK   1 mismatch or unreachable   2 usage or config error

config: ./github2gitlab.ini by default (see github2gitlab.example.ini);
[verify] selects source = local-bare|api, target = gitlab|github and the
manifest path (auto = <clone_dir>/migrate-manifest.txt).""")
    parser.add_argument("-c", "--config", default="github2gitlab.ini",
                        help="config file path")
    parser.add_argument("--selftest", action="store_true",
                        help="run the offline comparison self-test and exit")
    return parser.parse_args()


def verify_entries(cfg, entries, login=""):
    """Classify every manifest entry as OK, MISMATCH or UNREACHABLE.

    A side that cannot be read is UNREACHABLE, never an empty ref set: an empty
    set is only OK when both sides were really read and both have zero refs.
    """
    ok, mismatch, unreachable = 0, [], []
    for name, target_path in entries:
        if cfg["source"] == "local-bare":
            try:
                src = refs_local(os.path.join(cfg["clone_dir"], name + ".git"))
            except MigrateError as e:
                unreachable.append((name, str(e)))
                continue
        else:
            src, err = github_refs(cfg, f"{login}/{name}")
            if err:
                unreachable.append((name, err))
                continue
        if cfg["target"] == "gitlab":
            dst, err = gitlab_refs(cfg, target_path)
        else:
            dst, err = github_refs(cfg, target_path)
        if err:
            unreachable.append((name, err))
            continue
        diffs = classify(src, dst)
        if diffs:
            mismatch.append((name, target_path, diffs))
        else:
            ok += 1
    return ok, mismatch, unreachable


def report(ok, mismatch, unreachable):
    print(f"OK (refs identical): {ok}")
    print(f"MISMATCH: {len(mismatch)}")
    for name, target_path, diffs in mismatch:
        print(f"  {name} -> {target_path}")
        for d in diffs[:8]:
            print(f"    {d}")
    print(f"UNREACHABLE: {len(unreachable)}")
    for name, err in unreachable:
        print(f"  {name}: {err}")


def main():
    args = parse_args()
    if args.selftest:
        try:
            selftest()
        except (MigrateError, OSError) as e:
            # fixture or environment problem: same taxonomy as the pre-run errors
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)
        return

    try:
        cfg = load_verify_config(args.config)
        entries = read_manifest(cfg["manifest"])
    except (MigrateError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"verifying {len(entries)} repo(s) from {cfg['manifest']} "
          f"(source={cfg['source']}, target={cfg['target']}) ...\n")
    login = ""
    if cfg["source"] == "api":
        try:
            login = github_login(cfg)
        except MigrateError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)

    ok, mismatch, unreachable = verify_entries(cfg, entries, login)
    report(ok, mismatch, unreachable)
    if mismatch or unreachable:
        sys.exit(1)


if __name__ == "__main__":
    main()
