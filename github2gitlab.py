#!/usr/bin/env python3
"""Migrate the repos owned by the authenticated user to GitLab projects.

Flow: list repos via GET /user/repos?affiliation=owner -> clone --bare
-> create GitLab private project if missing -> push --mirror.
--clone-only and --push-only run one half each, for windows where only one
instance is reachable: the clone half skips repos whose local bare clone
already matches the source refs, and the push half works off the local repo
list the clone half wrote.

Stdlib only (Python >= 3.9). Requires git on PATH.
"""
import argparse
import configparser
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 100
CLONE_RETRIES = 3
CLONE_BACKOFF_SECONDS = 5
CLONE_TIMEOUT_SECONDS = 7200  # 2h per repo; 30min capped a clone at ~215MB on a ~0.1MB/s link
PROTOCOLS = ("https", "ssh")
GHE_API_PATH = "/api/v3"
GITLAB_API_PATH = "/api/v4"
MANIFEST_NAME = "migrate-manifest.txt"
REPO_LIST_NAME = "migrate-repos.json"


class MigrateError(Exception):
    pass


def decode_body(raw):
    """Response bytes as JSON when they parse, else the decoded text.

    A response that arrived is never an error: an HTML proxy/SSO page and a body
    that is not valid UTF-8 must both come back as data, not as a raise.
    """
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return text


def http_json(method, url, headers=None, body=None):
    """HTTP status and parsed body; a response that arrived never raises.

    A non-JSON body comes back as the raw text. Only a request that cannot be
    built or carried (bad base_url, connection, DNS, TLS, timeout, broken proxy
    reply) raises: it carries no HTTP status, so a caller cannot classify it from
    the return value. Auth travels in headers and any credential pasted into the
    URL is redacted, so nothing secret reaches the message.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, decode_body(r.read())
    except urllib.error.HTTPError as e:
        return e.code, decode_body(e.read())
    except (OSError, ValueError, http.client.HTTPException) as e:
        # HTTPError first (it is an OSError); the rest are the malformed-URL and
        # broken-reply families, which carry no status and so must become ours
        raise MigrateError(redact(f"{method} {url} failed: {e}", url))


def mask(text, secret):
    return text.replace(secret, "***") if secret else text


def redact(text, url):
    """*text* with any credential pasted into *url* removed.

    A userinfo in base_url (https://user:token@host) must not reach stderr: the
    message echoes the URL and urllib quotes the password back in its own error
    text (InvalidURL: nonnumeric port: 'token@host').
    """
    netloc = url.partition("://")[2].partition("/")[0]
    userinfo, at, _ = netloc.rpartition("@")
    if not at:
        return text
    return mask(text.replace(userinfo + "@", ""), userinfo.partition(":")[2])


def normalize_base(value, default_scheme="https"):
    """Host or URL -> base URL without trailing slash."""
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = f"{default_scheme}://{value}"
    return value


def config_error_message(exc):
    """One actionable line for a config error, without echoing file content.

    configparser embeds the offending source line and getboolean embeds the value;
    a token can sit in either, and the parse failed before any token was known, so
    neither is ever echoed.
    """
    if isinstance(exc, configparser.Error):
        if isinstance(exc, configparser.InterpolationError):
            # interpolation messages embed the raw value
            return (f"invalid interpolation in section {exc.section or '?'!r} "
                    f"option {exc.option or '?'!r} (value not shown)")
        kept = []
        for line in str(exc).splitlines():
            if line.lstrip().startswith("'"):
                continue  # the raw source line itself
            line = line.split("Raw value:", 1)[0]
            kept.append(line.split("'", 1)[0] if line.lstrip().startswith("[line")
                        else line)
        text = " ".join(" ".join(kept).split())
    else:
        text = str(exc).split(":", 1)[0].strip()
    return text or exc.__class__.__name__


def load_config(path):
    """Read [github] / [gitlab] / [run]; tokens fall back to env variables."""
    if not os.path.exists(path):
        raise MigrateError(f"config file not found: {path}")
    cp = configparser.ConfigParser()
    try:
        # cp.read itself raises on a malformed ini (no section header, duplicate key)
        cp.read(path, encoding="utf-8")
        raw = {
            "gh_base_url": cp.get("github", "base_url", fallback="").strip(),
            "gh_token": (cp.get("github", "token", fallback="").strip()
                         or os.environ.get("GHE_TOKEN", "").strip()),
            "gh_clone_protocol": cp.get("github", "clone_protocol",
                                        fallback="https").strip().lower(),
            "gl_base_url": cp.get("gitlab", "base_url", fallback="").strip(),
            "gl_token": (cp.get("gitlab", "token", fallback="").strip()
                         or os.environ.get("GITLAB_TOKEN", "").strip()),
            "gl_push_protocol": cp.get("gitlab", "push_protocol",
                                       fallback="https").strip().lower(),
            "clone_dir": cp.get("run", "clone_dir", fallback="").strip(),
        }
    except (configparser.Error, ValueError) as e:
        # keep the single `error:` line, and never the quoted source line or value
        # (ValueError: a non-UTF8 config file or a non-boolean flag)
        raise MigrateError(f"invalid config: {config_error_message(e)}")
    # report missing values by their ini-facing names
    missing = [ini_name for key, ini_name in (
        ("gh_base_url", "github:base_url"),
        ("gh_token", "github:token"),
        ("gl_base_url", "gitlab:base_url"),
        ("gl_token", "gitlab:token"),
        ("clone_dir", "run:clone_dir")) if not raw[key]]
    if missing:
        raise MigrateError(f"missing config values: {missing} "
                           "(copy github2gitlab.example.ini to github2gitlab.ini and fill in; "
                           "tokens also accept env GHE_TOKEN / GITLAB_TOKEN)")
    for key, ini_name in (("gh_clone_protocol", "clone_protocol"),
                          ("gl_push_protocol", "push_protocol")):
        if raw[key] not in PROTOCOLS:
            raise MigrateError(f"invalid {ini_name}: {raw[key]} (choose from {PROTOCOLS})")
    cfg = dict(raw)
    cfg["gh_base_url"] = normalize_base(raw["gh_base_url"])
    cfg["gh_api_base"] = cfg["gh_base_url"] + GHE_API_PATH
    cfg["gl_base_url"] = normalize_base(raw["gl_base_url"])
    cfg["gl_api_base"] = cfg["gl_base_url"] + GITLAB_API_PATH
    return cfg


def with_basic_auth(base, user, token):
    """Embed user:token credentials into a base URL (colon kept literal)."""
    cred = (f"{urllib.parse.quote(user, safe='')}:"
            f"{urllib.parse.quote(token, safe='')}")
    return base.replace("://", f"://{cred}@", 1)


def run_git(args, cwd=None):
    ret = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding="utf-8", cwd=cwd)
    if ret.returncode != 0:
        raise MigrateError(f"git {args[1]} failed: {ret.stderr.strip()}")
    return ret


GHE_TOKEN_HINT = (" (check the source token: classic PAT needs repo scope; "
                  "fine-grained needs Contents and Metadata read)")
GL_TOKEN_HINT = " (check the GitLab token: needs api scope; or wrong base_url)"


def gh_headers(cfg):
    return {"Authorization": f"Bearer {cfg['gh_token']}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "github2gitlab"}


def gl_headers(cfg):
    return {"PRIVATE-TOKEN": cfg["gl_token"]}


def github_login(cfg):
    """Login of the source token's user; used for clone URLs."""
    status, body = http_json("GET", f"{cfg['gh_api_base']}/user", gh_headers(cfg))
    if status != 200:
        hint = GHE_TOKEN_HINT if status in (401, 403) else ""
        raise MigrateError(f"GET /user failed (HTTP {status}){hint}: "
                           f"{mask(str(body), cfg['gh_token'])}")
    login = ((body if isinstance(body, dict) else {}).get("login") or "").strip()
    if not login:
        raise MigrateError("GET /user returned no login (check token and base_url)")
    return login


def gitlab_namespace(cfg):
    """Target personal namespace: username of the token's user, not a config key."""
    status, body = http_json("GET", f"{cfg['gl_api_base']}/user", gl_headers(cfg))
    if status != 200:
        hint = GL_TOKEN_HINT if status in (401, 403) else ""
        raise MigrateError(f"GET /user failed (HTTP {status}){hint}: "
                           f"{mask(str(body), cfg['gl_token'])}")
    namespace = ((body if isinstance(body, dict) else {}).get("username") or "").strip()
    if not namespace:
        raise MigrateError("GET /user returned no username (check token and base_url)")
    return namespace


def validate_repo_name(name):
    """Repo names come from the remote API; refuse anything that could escape
    the clone dir when joined into a local filesystem path."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise MigrateError(f"unsafe repo name from API: {name!r}")


def list_source_repos(cfg):
    """Repos owned by the source token's user as (repos, failures).

    401/403 fail fast (whole-run blocker); 301 (renamed repo) is recorded in the
    failure list instead of being taken for a missing repo.
    """
    repos, failures, page = [], [], 1
    while True:
        q = urllib.parse.urlencode({"affiliation": "owner", "per_page": PAGE_SIZE,
                                    "page": page})
        status, body = http_json("GET", f"{cfg['gh_api_base']}/user/repos?{q}",
                                 gh_headers(cfg))
        if status in (401, 403):
            raise MigrateError(f"list repos failed (HTTP {status}){GHE_TOKEN_HINT}: "
                               f"{mask(str(body), cfg['gh_token'])}")
        if status == 301:
            failures.append((f"page {page}", "HTTP 301: repository list moved; "
                                             "renamed repo not treated as missing: "
                                             + mask(str(body), cfg["gh_token"])[:200]))
            break
        if status != 200 or not isinstance(body, list):
            raise MigrateError(f"list repos failed (HTTP {status}): "
                               f"{mask(str(body), cfg['gh_token'])}")
        if not body:
            break
        for r in body:
            name = (r.get("name") or "").strip()
            validate_repo_name(name)
            size = r.get("size")
            repos.append({
                "name": name,
                "private": bool(r.get("private", False)),
                "description": (r.get("description") or "").strip(),
                # API reports KB; keep MB for the run listing
                "size_mb": float(size) / 1024 if size not in (None, "") else None,
            })
        if len(body) < PAGE_SIZE:
            break
        page += 1
    return repos, failures


def derive_project_baseline(name, namespace):
    """Target project path shared by the existence query, push URL, post-create
    readback and verifier resolution; slug is the repo name lowercased."""
    return f"{namespace}/{name.lower()}"


def ensure_project_private(cfg, baseline, name):
    """Create the target project private, or verify an existing one is private.

    Returns the project body. An existing project that is not private raises
    before any push (never write first and mark later); a created project is
    read back so its path must match the derived baseline.
    """
    quoted = urllib.parse.quote(baseline, safe="")
    status, body = http_json("GET", f"{cfg['gl_api_base']}/projects/{quoted}",
                             gl_headers(cfg))
    if status == 200:
        visibility = (body if isinstance(body, dict) else {}).get("visibility")
        if visibility != "private":
            raise MigrateError(f"existing project {baseline} is not private "
                               f"(visibility={visibility}); refusing to push "
                               "(fix visibility or delete the project, then rerun)")
        return body
    if status != 404:
        hint = GL_TOKEN_HINT if status in (401, 403) else ""
        raise MigrateError(f"check project {baseline} failed (HTTP {status}){hint}: "
                           f"{mask(str(body), cfg['gl_token'])}")
    status, body = http_json("POST", f"{cfg['gl_api_base']}/projects", gl_headers(cfg),
                             {"name": name, "visibility": "private",
                              "initialize_with_readme": False})
    if status != 201:
        hint = GL_TOKEN_HINT if status in (401, 403) else ""
        raise MigrateError(f"create project {baseline} failed (HTTP {status}){hint}: "
                           f"{mask(str(body), cfg['gl_token'])}")
    actual = (body if isinstance(body, dict) else {}).get("path_with_namespace")
    if actual != baseline:
        raise MigrateError(f"created project path_with_namespace={actual!r} does not "
                           f"match derived baseline {baseline!r}")
    return body


def source_repo_url(cfg, name):
    """Clone URL for a source repo owned by the token's user."""
    full_name = f"{cfg['gh_login']}/{name}"
    if cfg["gh_clone_protocol"] == "ssh":
        host = urllib.parse.urlparse(cfg["gh_base_url"]).netloc
        return f"git@{host}:{full_name}.git"
    base = with_basic_auth(cfg["gh_base_url"], cfg["gh_login"], cfg["gh_token"])
    return f"{base}/{full_name}.git"


def target_push_url(cfg, baseline):
    """Push URL for the target project path."""
    if cfg["gl_push_protocol"] == "ssh":
        host = urllib.parse.urlparse(cfg["gl_base_url"]).netloc
        return f"git@{host}:{baseline}.git"
    # GitLab accepts any user name with a PAT as password; oauth2 is the convention
    base = with_basic_auth(cfg["gl_base_url"], "oauth2", cfg["gl_token"])
    return f"{base}/{baseline}.git"


def clone_bare(cfg, repo):
    """Bare-clone one repo into {clone_dir}/{name}.git; returns the clone dir.

    Retries on transient source drops with backoff; a failed attempt leaves a
    partial dir which is removed before the next attempt.
    """
    clone_dir = os.path.join(cfg["clone_dir"], repo["name"] + ".git")
    last_err = ""
    for attempt in range(1, CLONE_RETRIES + 1):
        if os.path.exists(clone_dir):
            shutil.rmtree(clone_dir, ignore_errors=True)
        print(f"  cloning {repo['name']}"
              + (f" (attempt {attempt}/{CLONE_RETRIES})" if attempt > 1 else ""),
              file=sys.stderr)
        try:
            ret = subprocess.run(["git", "clone", "--bare",
                                  source_repo_url(cfg, repo["name"]), clone_dir],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 encoding="utf-8", timeout=CLONE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            last_err = f"clone timed out after {CLONE_TIMEOUT_SECONDS}s"
            if attempt < CLONE_RETRIES:
                time.sleep(CLONE_BACKOFF_SECONDS)
                continue
            break
        if ret.returncode == 0:
            return clone_dir
        last_err = ret.stderr.strip()
        if attempt < CLONE_RETRIES:
            time.sleep(CLONE_BACKOFF_SECONDS)
    raise MigrateError(f"git clone failed: {last_err}")


def push_mirror(cfg, baseline, clone_dir):
    """Mirror-push a cloned bare repo to its target project."""
    print(f"  pushing {baseline}", file=sys.stderr)
    run_git(["git", "push", "--mirror", target_push_url(cfg, baseline)], cwd=clone_dir)


def confirm_migration(args, count, cfg, namespace):
    """Prompt gate for the irreversible mirror push.

    Only prompts when both stdin and stdout are TTY and --yes is absent;
    scripted (non-TTY) runs proceed — the target set is explicit via config.
    """
    if args.yes:
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    netloc = urllib.parse.urlparse(cfg["gl_base_url"]).netloc
    answer = input(f"\nMigrate {count} repo(s) to {netloc}/{namespace} "
                   "(private projects, mirror push overwrites targets)? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def acquire_lock(clone_dir):
    """Single-instance lock under clone_dir; stale lock (dead pid) is reclaimed."""
    os.makedirs(clone_dir, exist_ok=True)
    lock_path = os.path.join(clone_dir, ".migrate.lock")
    if os.path.exists(lock_path):
        try:
            with open(lock_path, encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            raise MigrateError(f"another migration is running (pid {old_pid}); "
                               f"if not, remove {lock_path}")
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))
    return lock_path


def write_manifest(clone_dir, rows):
    """Run manifest read by the verifier as its repo denominator.

    One line per repo: name, target path, source private flag, result.
    """
    path = os.path.join(clone_dir, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# name\ttarget_path\tsource_private\tresult\n")
        for name, baseline, private, result in rows:
            f.write(f"{name}\t{baseline}\t{'private' if private else 'public'}\t"
                    f"{result}\n")
    return path


def select_repos(repos, only):
    """Apply --only filtering; unknown names are a usage error, not a silent no-op."""
    if not only:
        return repos
    wanted = set(only)
    known = {r["name"] for r in repos}
    unknown = sorted(wanted - known)
    if unknown:
        raise MigrateError(f"unknown --only repo name(s): {unknown}")
    return [r for r in repos if r["name"] in wanted]


def run_git_capture(args, env=None, check=False):
    """subprocess.run for git; failing to launch git at all is a MigrateError.

    A non-zero git exit is left to the caller (show-ref exits 1 on an empty
    repo) unless check=True, which raises as well; a git that cannot be executed
    (missing binary, permission error) always becomes the module's own error, so
    a repo is UNREACHABLE or the run ends with an error line, never a crash.
    """
    try:
        ret = subprocess.run(args, capture_output=True, text=True, env=env)
    except OSError as e:
        raise MigrateError(f"cannot run {args[0]}: {e}")
    if check and ret.returncode != 0:
        raise MigrateError(f"{' '.join(args)} failed: {ret.stderr.strip()}")
    return ret


def refs_local(bare):
    """{ref_name: commit_sha} for branches and tags from a local bare clone.

    Annotated tags are dereferenced to the commit ({}^{}) so they compare equal
    to the target API, which reports the commit, not the tag object. An empty
    repo has zero refs and is not an error.
    """
    if not os.path.isdir(bare):
        raise MigrateError(f"local bare repo not found: {bare}")
    ret = run_git_capture(["git", "-C", bare, "show-ref"])
    if ret.returncode != 0 and ret.stderr.strip():
        raise MigrateError(f"git show-ref failed in {bare}: {ret.stderr.strip()}")
    refs = {}
    for line in ret.stdout.splitlines():
        sha, _, ref = line.partition(" ")
        if ref.startswith("refs/heads/"):
            refs[ref] = sha
        elif ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            refs[ref] = None
    for ref in [r for r, sha in refs.items() if sha is None]:
        deref = run_git_capture(["git", "-C", bare, "rev-parse", ref + "^{}"])
        if deref.returncode != 0:
            raise MigrateError(f"cannot dereference {ref} in {bare}: "
                               f"{deref.stderr.strip()}")
        refs[ref] = deref.stdout.strip()
    return refs


def github_refs(cfg, full_name):
    """{ref_name: sha} via the GitHub-family branches/tags API (commit.sha)."""
    refs = {}
    for kind, namespace in (("branches", "heads"), ("tags", "tags")):
        page = 1
        while True:
            url = (f"{cfg['gh_api_base']}/repos/{full_name}/{kind}"
                   f"?per_page={PAGE_SIZE}&page={page}")
            try:
                status, body = http_json("GET", url, gh_headers(cfg))
            except MigrateError as e:
                # endpoint unreachable: this repo is UNREACHABLE, not the whole run
                return None, str(e)
            if status != 200 or not isinstance(body, list):
                return None, f"HTTP {status} listing {kind} of {full_name}"
            for item in body:
                refs[f"refs/{namespace}/{item.get('name')}"] = \
                    (item.get("commit") or {}).get("sha")
            if len(body) < PAGE_SIZE:
                break
            page += 1
    return refs, None


def local_matches_source(cfg, bare, name):
    """True when the local bare clone already has exactly the source's refs.

    The comparison is the verifier's: same refs readers, same equality, so
    "current" here means what the acceptance check means.
    """
    if not os.path.isdir(bare):
        return False
    local = refs_local(bare)
    remote, err = github_refs(cfg, f"{cfg['gh_login']}/{name}")
    if err:
        raise MigrateError(f"source refs unavailable for {name}: {err}")
    return local == remote


def clone_state(ready, failed):
    """Per-repo verification state after a clone pass (name -> verified)."""
    state = {r["name"]: True for r, _ in ready}
    state.update({name: False for name, _ in failed})
    return state


def clone_phase(cfg, repos):
    """Clone the repos, skipping those whose local clone already matches.

    Maintains the repo list's clone_verified flags around the destructive step:
    the repos are marked unverified before anything is wiped, and the ones with
    a usable clone are marked verified after the pass, so a list surviving a
    killed run never claims a clone that was deleted.

    Returns (ready, skipped, failed): ready is (repo, clone_dir) for every repo
    with a usable local clone (freshly cloned or already current), so the push
    half can run on it; skipped holds the names that needed no transfer.
    """
    update_repo_list(cfg["clone_dir"], {r["name"]: False for r in repos})
    ready, skipped, failed = [], [], []
    for repo in repos:
        name = repo["name"]
        bare = os.path.join(cfg["clone_dir"], name + ".git")
        try:
            if local_matches_source(cfg, bare, name):
                skipped.append(name)
                ready.append((repo, bare))
                print(f"  skip {name} (local refs already match the source)",
                      file=sys.stderr)
                continue
        except MigrateError as e:
            # cannot prove the clone is current: re-cloning is the safe fallback
            warn = mask(mask(str(e), cfg["gh_token"]), cfg["gl_token"])
            print(f"  WARN {name}: {warn}; cloning anyway", file=sys.stderr)
        try:
            ready.append((repo, clone_bare(cfg, repo)))
        except (MigrateError, OSError, subprocess.SubprocessError) as e:
            err = mask(mask(str(e), cfg["gh_token"]), cfg["gl_token"])
            failed.append((name, err))
            print(f"  FAILED {name}: {err[:160]}", file=sys.stderr)
    update_repo_list(cfg["clone_dir"], clone_state(ready, failed))
    return ready, skipped, failed


def push_phase(cfg, namespace, ready):
    """Create/verify each target project, then mirror-push its local clone.

    Returns (ok, failures, results) with results as {name: "ok" or "failed"}.
    """
    ok, failed, results = 0, [], {}
    for repo, clone_dir in ready:
        name = repo["name"]
        baseline = derive_project_baseline(name, namespace)
        if not os.path.isdir(clone_dir):
            results[name] = "failed"
            failed.append((name, "no local clone"))
            print(f"  SKIP {name}: no local clone", file=sys.stderr)
            continue
        print(f"pushing {name} -> {baseline}", file=sys.stderr)
        try:
            ensure_project_private(cfg, baseline, name)
            push_mirror(cfg, baseline, clone_dir)
        except (MigrateError, OSError, subprocess.SubprocessError) as e:
            err = mask(mask(str(e), cfg["gh_token"]), cfg["gl_token"])
            results[name] = "failed"
            failed.append((name, err))
            print(f"  FAILED: {err}", file=sys.stderr)
            continue
        ok += 1
        results[name] = "ok"
        print(f"  done: {baseline}", file=sys.stderr)
    return ok, failed, results


def load_repo_list_entries(path):
    """Entries from an existing repo list; [] for an empty one, None when the
    file is missing or unreadable (callers pick their own fallback)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(entries, list):
        return None
    return [e for e in entries if isinstance(e, dict)]


def write_repo_list(clone_dir, repos, state):
    """Source repo snapshot for a later --push-only run.

    *state* maps the names this run processed to whether their local bare clone
    is verified complete (freshly cloned, or refs match the source). Repos the
    run did not process keep the previous list's flag; an unreadable previous
    list is rebuilt with everything unverified, which is the safe default.
    """
    path = os.path.join(clone_dir, REPO_LIST_NAME)
    old = {}
    for entry in load_repo_list_entries(path) or []:
        if isinstance(entry.get("name"), str):
            old[entry["name"]] = entry.get("clone_verified") is True
    rows = []
    for repo in repos:
        entry = dict(repo)
        entry["clone_verified"] = state.get(repo["name"], old.get(repo["name"], False))
        rows.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
        f.write("\n")
    return path


def update_repo_list(clone_dir, state):
    """Merge per-repo verification states into the existing list.

    The clone machinery calls this around the destructive step; a repo the list
    does not know stays unknown. A missing or unreadable list is left for the
    clone half to rewrite (push-only then refuses everything, the safe default).
    """
    path = os.path.join(clone_dir, REPO_LIST_NAME)
    entries = load_repo_list_entries(path)
    if entries is None:
        return None
    rows = []
    for entry in entries:
        if isinstance(entry.get("name"), str) and entry["name"] in state:
            entry = dict(entry)
            entry["clone_verified"] = state[entry["name"]]
        rows.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
        f.write("\n")
    return path


def read_repo_list(clone_dir):
    """The repo list written by --clone-only; --push-only's only repo source."""
    path = os.path.join(clone_dir, REPO_LIST_NAME)
    if not os.path.exists(path):
        raise MigrateError(f"local repo list not found: {path} "
                           "(run the clone half first: --clone-only)")
    try:
        with open(path, encoding="utf-8") as f:
            repos = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise MigrateError(f"malformed repo list {path}: {e}")
    if not isinstance(repos, list):
        raise MigrateError(f"malformed repo list {path}: expected a list")
    for entry in repos:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not entry["name"]):
            raise MigrateError(f"malformed repo list {path}: entry without a name")
        # names join into local paths and URLs: same check as the API listing
        validate_repo_name(entry["name"])
    return repos


def read_manifest_rows(path):
    """[(name, target_path, private, result)] from an existing manifest, [] if absent."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 4:
                raise MigrateError(f"malformed manifest line: {line}")
            rows.append((fields[0], fields[1], fields[2] == "private", fields[3]))
    return rows


def write_manifest_ledger(clone_dir, run_rows):
    """Write the manifest as a full ledger.

    The verifier reads every row as its repo denominator, so a run that touches
    a subset (--only) must not drop the rows it did not touch: those keep their
    previous values, rows the run touched are refreshed in place, and newly
    seen repos are appended.
    """
    fresh = {row[0]: row for row in run_rows}
    merged = [fresh.pop(row[0], row)
              for row in read_manifest_rows(os.path.join(clone_dir, MANIFEST_NAME))]
    merged.extend(fresh.values())
    return write_manifest(clone_dir, merged)


def print_clone_plan(cfg, repos, failures):
    """Source-side listing for --clone-only; the target is never contacted."""
    print(f"endpoints: source={cfg['gh_base_url']} (target not contacted)",
          file=sys.stderr)
    print(f"found {len(repos)} repo(s) under {cfg['gh_login']}\n")
    for r in repos:
        size = f" ({r['size_mb']:.0f}MB)" if r["size_mb"] is not None else ""
        flag = "" if r["private"] else " [public source]"
        print(f"  {r['name']:<48}{size}{flag}")
    for label, err in failures:
        print(f"  FAILED {label}: {err}", file=sys.stderr)


def print_push_plan(cfg, namespace, repos, total):
    """Local-list listing for --push-only; the source is never contacted."""
    print(f"endpoints: target={cfg['gl_base_url']} (source not contacted)",
          file=sys.stderr)
    print(f"found {total} repo(s) in the local list, pushing {len(repos)}\n")
    for r in repos:
        size = f" ({r['size_mb']:.0f}MB)" if r.get("size_mb") is not None else ""
        baseline = derive_project_baseline(r["name"], namespace)
        print(f"  {r['name']:<48} -> {baseline}{size}")


def run_clone_only(args, cfg, repos, all_repos, enum_failures):
    """The clone half: the source is contacted, the target never is."""
    print_clone_plan(cfg, repos, enum_failures)
    if args.dry_run:
        return 1 if enum_failures else 0
    if not repos:
        print("nothing to clone", file=sys.stderr)
        return 1 if enum_failures else 0
    try:
        ready, skipped, failed = clone_phase(cfg, repos)
    except (MigrateError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if enum_failures:
        print("repo list not written: the enumeration was incomplete",
              file=sys.stderr)
    else:
        try:
            path = write_repo_list(cfg["clone_dir"], all_repos,
                                   clone_state(ready, failed))
        except (MigrateError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"repo list: {path} ({len(all_repos)} repo(s))", file=sys.stderr)
    print(f"summary: {len(ready) - len(skipped)} cloned, {len(skipped)} skipped, "
          f"{len(failed)} failed", file=sys.stderr)
    for name, err in failed:
        print(f"  FAILED {name}: {err[:200]}", file=sys.stderr)
    return 1 if (failed or enum_failures) else 0


def run_push_only(args, cfg):
    """The push half: repos come from the local list, the source is never contacted."""
    try:
        all_repos = read_repo_list(cfg["clone_dir"])
        namespace = gitlab_namespace(cfg)
        repos = select_repos(all_repos, args.only)
    except (MigrateError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_push_plan(cfg, namespace, repos, len(all_repos))
    if args.dry_run:
        return 0
    if not repos:
        print("nothing to push", file=sys.stderr)
        return 0
    try:
        if not confirm_migration(args, len(repos), cfg, namespace):
            print("cancelled", file=sys.stderr)
            return 130
    except EOFError:
        print("cancelled", file=sys.stderr)
        return 130
    # only a clone the clone half verified may be mirrored: pushing a partial or
    # empty leftover would delete target refs (mirror semantics) and read as ok
    ready, failed, results = [], [], {}
    for r in repos:
        name = r["name"]
        if r.get("clone_verified") is not True:
            failed.append((name, "local clone not verified (run --clone-only first)"))
            results[name] = "failed"
            print(f"  SKIP {name}: local clone not verified", file=sys.stderr)
            continue
        ready.append((r, os.path.join(cfg["clone_dir"], name + ".git")))
    ok, push_failed, push_results = push_phase(cfg, namespace, ready)
    failed.extend(push_failed)
    results.update(push_results)
    try:
        manifest = write_manifest_ledger(cfg["clone_dir"], [
            (r["name"], derive_project_baseline(r["name"], namespace),
             bool(r.get("private", False)), results.get(r["name"], "failed"))
            for r in repos])
    except (MigrateError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"summary: {ok} success, {len(failed)} failed", file=sys.stderr)
    print(f"manifest: {manifest}", file=sys.stderr)
    for name, err in failed:
        print(f"  FAILED {name}: {err[:200]}", file=sys.stderr)
    return 1 if failed else 0


def run_default(args, cfg, repos, namespace, enum_failures):
    """One-run migration: clone everything, then push everything."""
    print_plan(cfg, repos, namespace, enum_failures)
    if args.dry_run:
        return 1 if enum_failures else 0
    if not repos:
        print("nothing to migrate", file=sys.stderr)
        return 1 if enum_failures else 0
    try:
        if not confirm_migration(args, len(repos), cfg, namespace):
            print("cancelled", file=sys.stderr)
            return 130
    except EOFError:
        print("cancelled", file=sys.stderr)
        return 130
    try:
        ok, migration_failures = migrate_all(cfg, repos, namespace)
    except (MigrateError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    failed = enum_failures + migration_failures
    print(f"summary: {ok} success, {len(failed)} failed", file=sys.stderr)
    for name, err in failed:
        print(f"  FAILED {name}: {err[:200]}", file=sys.stderr)
    return 1 if failed else 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Migrate the repos owned by the source user to GitLab private projects.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --dry-run                  list repos and target projects, no changes
  %(prog)s --yes                      migrate without the confirmation prompt
  %(prog)s --only myrepo --yes        migrate a single repo first
  %(prog)s --clone-only               clone half only; the target is not contacted
  %(prog)s --push-only --yes          push half only, from the local repo list

exit codes:
  0 success   1 migration failures   2 usage or config error   130 cancelled

config: ./github2gitlab.ini by default (see github2gitlab.example.ini);
tokens may come from env GHE_TOKEN / GITLAB_TOKEN.
--clone-only records in <clone_dir>/migrate-repos.json which local clones it
verified; a later --push-only run (no source access needed) mirrors only those.
The run manifest keeps the rows a run did not touch.""")
    parser.add_argument("-c", "--config", default="github2gitlab.ini",
                        help="config file path")
    parser.add_argument("--dry-run", action="store_true",
                        help="only list repos and target project paths")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt (non-interactive runs never prompt)")
    parser.add_argument("--only", action="append", default=[], metavar="NAME",
                        help="limit the run to these repo names (repeatable)")
    halves = parser.add_mutually_exclusive_group()
    halves.add_argument("--clone-only", action="store_true",
                        help="run only the clone half; repos whose local clone "
                             "already matches the source are skipped")
    halves.add_argument("--push-only", action="store_true",
                        help="run only the push half against existing local "
                             "clones; the source is never contacted")
    return parser.parse_args()


def print_plan(cfg, repos, namespace, failures):
    """Endpoints, the repo -> target mapping and any enumeration failures."""
    print(f"endpoints: source={cfg['gh_base_url']} target={cfg['gl_base_url']}",
          file=sys.stderr)
    print(f"found {len(repos)} repo(s) under {cfg['gh_login']}\n")
    for r in repos:
        baseline = derive_project_baseline(r["name"], namespace)
        size = f" ({r['size_mb']:.0f}MB)" if r["size_mb"] is not None else ""
        flag = "" if r["private"] else " [public source]"
        print(f"  {r['name']:<48} -> {baseline}{size}{flag}")
    for label, err in failures:
        print(f"  FAILED {label}: {err}", file=sys.stderr)


def migrate_all(cfg, repos, namespace):
    """Clone everything, then create/verify and push everything.

    Returns (success count, failures) with failures as (name, masked message).
    """
    # phase 1: clone everything first (disk-to-disk, no target writes yet)
    print(f"\nphase 1/2: cloning {len(repos)} repo(s) into {cfg['clone_dir']}",
          file=sys.stderr)
    ready, skipped, clone_failed = clone_phase(cfg, repos)
    if skipped:
        print(f"  {len(skipped)} repo(s) skipped: local refs already match",
              file=sys.stderr)

    # phase 2: create/verify the target project, then mirror-push
    print(f"\nphase 2/2: pushing {len(ready)} repo(s)", file=sys.stderr)
    ok, push_failed, results = push_phase(cfg, namespace, ready)

    manifest = write_manifest_ledger(cfg["clone_dir"], [
        (r["name"], derive_project_baseline(r["name"], namespace), r["private"],
         results.get(r["name"], "failed")) for r in repos])
    print(f"\nmanifest: {manifest}", file=sys.stderr)
    return ok, clone_failed + push_failed


def main():
    args = parse_args()

    try:
        run_git(["git", "--version"])
        cfg = load_config(args.config)
        acquire_lock(cfg["clone_dir"])
    except (MigrateError, OSError) as e:
        # usage/config/environment problems: actionable message, stable exit 2
        # (OSError: no git binary, unreadable config path)
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.push_only:
        # the push half works off the local repo list: the source is never contacted
        sys.exit(run_push_only(args, cfg))

    try:
        cfg["gh_login"] = github_login(cfg)
        # the clone half never contacts the target; every other run needs it here
        namespace = "" if args.clone_only else gitlab_namespace(cfg)
        repos, failures = list_source_repos(cfg)
        all_repos = repos
        repos = select_repos(repos, args.only)
    except MigrateError as e:
        # credential/endpoint problems stop the run before any repo work
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.clone_only:
        sys.exit(run_clone_only(args, cfg, repos, all_repos, failures))
    sys.exit(run_default(args, cfg, repos, namespace, failures))


if __name__ == "__main__":
    main()
