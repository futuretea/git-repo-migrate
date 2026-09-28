#!/usr/bin/env python3
"""Migrate Codeup repos under a group (incl. subgroups) to GitHub private repos.

Flow: list repos via Codeup OpenAPI -> filter by group path -> clone --bare
-> create GitHub private repo if missing -> push --mirror.

Stdlib only (Python >= 3.9). Requires git on PATH.
"""
import argparse
import configparser
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API_DEFAULT = "https://api.github.com"
GITHUB_GIT_DEFAULT = "https://github.com"
PAGE_SIZE = 100
API_STYLES = ("legacy", "oapi")
CLONE_RETRIES = 3
CLONE_BACKOFF_SECONDS = 5
CLONE_TIMEOUT_SECONDS = 1800  # 30min per repo; bare clones of huge repos included


class MigrateError(Exception):
    pass


def decode_body(text):
    """JSON when the body parses, else the text itself.

    A response that arrived is never an error: an HTML proxy/SSO page comes back
    as data so the caller can classify it.
    """
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return text


def http_json(method, url, headers=None, body=None, insecure=False):
    """HTTP status and parsed body; a response that arrived never raises.

    Only a request that cannot be built or carried (bad base_url, connection, TLS,
    broken proxy reply) raises: it carries no HTTP status for a caller to classify,
    so it ends the run through the caller's error path instead.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        ctx = ssl._create_unverified_context() if insecure else None
        with urllib.request.urlopen(req, context=ctx) as r:
            return r.status, decode_body(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, decode_body(e.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, http.client.HTTPException) as e:
        # a credential pasted into the URL must not survive in the message
        raise MigrateError(redact(f"{method} {url} failed: {e}", url))


def mask(text, secret):
    return text.replace(secret, "***") if secret else text


def redact(text, url):
    """*text* with any credential pasted into *url* removed.

    A userinfo in api_domain/api_base must not reach stderr: urllib quotes the
    password back in its own error text.
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
    if not os.path.exists(path):
        raise MigrateError(f"config file not found: {path}")
    cp = configparser.ConfigParser()
    try:
        # cp.read raises on a malformed ini (no section header, duplicate key) and
        # getboolean raises on a non-boolean value: both are config errors
        cp.read(path, encoding="utf-8")
        api_domain = cp.get("codeup", "api_domain", fallback="").strip()
        git_host = cp.get("codeup", "git_host", fallback="").strip()
        if not git_host:
            # no silent default: a wrong clone host wastes a full run
            raise MigrateError("missing config values: ['git_host'] "
                               "(set to your Codeup git server, e.g. yunxiao.example.com)")
        cfg = {
            "api_style": cp.get("codeup", "api_style", fallback="legacy").strip().lower(),
            "org_id": cp.get("codeup", "organization_id", fallback="").strip(),
            # ini value wins; env vars are the automation alternative for secrets
            "codeup_token": (cp.get("codeup", "access_token", fallback="").strip()
                             or os.environ.get("CODEUP_ACCESS_TOKEN", "").strip()),
            "group_path": cp.get("codeup", "group_path", fallback="").strip("/"),
            "api_base": normalize_base(api_domain) if api_domain else "",
            "git_base": normalize_base(git_host),
            "codeup_git_user": cp.get("codeup", "git_username", fallback="").strip(),
            "git_codeup_prefix": cp.getboolean("codeup", "git_codeup_prefix",
                                               fallback=True),
            "insecure_tls": cp.getboolean("codeup", "insecure_tls", fallback=False),
            "gh_user": cp.get("github", "username", fallback="").strip(),
            "gh_token": (cp.get("github", "token", fallback="").strip()
                         or os.environ.get("GITHUB_TOKEN", "").strip()),
            # GitHub.com defaults; for GitHub Enterprise set both to your host
            "gh_api_base": normalize_base(cp.get("github", "api_base",
                                                 fallback=GITHUB_API_DEFAULT)),
            "gh_git_base": normalize_base(cp.get("github", "git_base",
                                                 fallback=GITHUB_GIT_DEFAULT)),
            # push via SSH (git@host:owner/repo.git) instead of HTTPS+token;
            # requires an SSH key registered on the GitHub account
            "gh_push_ssh": cp.getboolean("github", "push_ssh", fallback=False),
            "clone_dir": cp.get("run", "clone_dir", fallback=".migrate-clones").strip(),
            "exclude": {s.strip() for s in cp.get("run", "exclude", fallback="").split(",") if s.strip()},
            # group whitelist: only migrate repos under these group prefixes (rel paths)
            "include_groups": [s.strip().strip("/") for s in
                               cp.get("run", "include_groups", fallback="").split(",")
                               if s.strip()],
            # numeric Codeup creator IDs whose personal (non-whitelisted) repos are migrated
            "my_creator_ids": {s.strip() for s in
                               cp.get("run", "my_creator_ids", fallback="").split(",")
                               if s.strip()},
            # prefix for GitHub repo names, e.g. "codeup-" -> codeup-demo-app
            "repo_prefix": cp.get("run", "repo_prefix", fallback="").strip(),
        }
    except (configparser.Error, ValueError) as e:
        raise MigrateError(f"invalid config: {config_error_message(e)}")
    if cfg["api_style"] not in API_STYLES:
        raise MigrateError(f"invalid api_style: {cfg['api_style']} (choose from {API_STYLES})")
    # report missing values by their ini-facing names
    required = {
        "api_base": "api_domain",
        "git_base": "git_host",
        "codeup_token": "access_token",
        "codeup_git_user": "git_username",
        "gh_user": "username",
        "gh_token": "token",
    }
    missing = [name for key, name in required.items() if not cfg[key]]
    if cfg["api_style"] == "legacy" and not cfg["org_id"]:
        missing.append("organization_id")
    if missing:
        raise MigrateError(f"missing config values: {missing} "
                           f"(copy migrate.example.ini to migrate.ini and fill in; "
                           f"tokens also accept env CODEUP_ACCESS_TOKEN / GITHUB_TOKEN)")
    return cfg


def fetch_all_pages(cfg, url, params):
    """Fetch paginated list; returns raw repo dicts. Stops on short/empty page."""
    headers = {"x-yunxiao-token": cfg["codeup_token"]} if cfg["api_style"] == "oapi" else {}
    repos, page = [], 1
    while True:
        q = dict(params, page=page, perPage=PAGE_SIZE)
        status, body = http_json("GET", f"{url}?{urllib.parse.urlencode(q)}",
                                 headers=headers, insecure=cfg["insecure_tls"])
        if status != 200:
            hint = (" (check Codeup access_token validity/scopes, or wrong api_domain)"
                    if status in (401, 403) else "")
            raise MigrateError(f"list repos failed (HTTP {status}){hint}: "
                               f"{mask(str(body), cfg['codeup_token'])}")
        if cfg["api_style"] == "legacy":
            if not isinstance(body, dict) or not body.get("success", False):
                raise MigrateError(f"list repos failed: "
                                   f"{mask(str(body), cfg['codeup_token'])}")
            result = body.get("result") or []
        else:
            result = body if isinstance(body, list) else []
        if not result:
            break
        repos.extend(result)
        if len(result) < PAGE_SIZE:
            break
        page += 1
    return repos


def list_codeup_repos(cfg):
    """List repos visible to the token, then filter by group path.

    legacy: GET {base}/repository/list (organizationId + accessToken query).
    oapi:   GET {base}/oapi/v1/codeup/organizations/{orgId}/repositories
            or {base}/oapi/v1/codeup/repositories (x-yunxiao-token header).
    Returns [{path, path_with_namespace, rel, description, archived}].
    """
    if cfg["api_style"] == "legacy":
        url = f"{cfg['api_base']}/repository/list"
        params = {"organizationId": cfg["org_id"], "accessToken": cfg["codeup_token"],
                  "orderBy": "id", "sort": "asc"}
    else:
        base = f"{cfg['api_base']}/oapi/v1/codeup"
        url = (f"{base}/organizations/{cfg['org_id']}/repositories" if cfg["org_id"]
               else f"{base}/repositories")
        params = {"orderBy": "created_at", "sort": "asc"}
    repos = fetch_all_pages(cfg, url, params)

    include_groups = cfg.get("include_groups") or []
    creator_ids = cfg.get("my_creator_ids") or set()
    picked, creator_repos = [], {}
    for r in repos:
        pwn = (r.get("pathWithNamespace") or "").strip("/")
        rel = pwn
        if cfg["org_id"] and rel.startswith(cfg["org_id"] + "/"):
            rel = rel[len(cfg["org_id"]) + 1:]
        if rel in cfg["exclude"]:
            continue
        in_group = any(rel.startswith(g + "/") or rel == g for g in include_groups)
        created_by_me = str(r.get("creatorId", "")) in creator_ids
        if include_groups or creator_ids:
            # selection mode: group whitelist, plus my own repos anywhere else
            if not (in_group or created_by_me):
                continue
        elif cfg["group_path"] and not rel.startswith(cfg["group_path"] + "/"):
            # plain mode: single group prefix filter
            continue
        if created_by_me and not in_group:
            creator_repos.setdefault(str(r.get("creatorId")), []).append(rel)
        validate_repo_path(pwn)
        size = r.get("repositorySize")
        picked.append({
            "path": r.get("path") or rel.rsplit("/", 1)[-1],
            "path_with_namespace": pwn,
            "rel": rel,
            "description": (r.get("description") or "").strip(),
            "archived": bool(r.get("archived", r.get("archive", False))),
            "creator_id": r.get("creatorId"),
            # MB as reported by the API; None when the field is absent
            "size_mb": float(size) if size not in (None, "") else None,
        })
    return picked


def validate_repo_path(pwn):
    """Namespace paths come from the remote API; refuse anything that could
    escape the clone dir when joined into a local filesystem path."""
    if not pwn:
        raise MigrateError("empty repo path from API")
    for seg in pwn.split("/"):
        if seg in (".", "..") or "\\" in seg:
            raise MigrateError(f"unsafe repo path from API: {pwn}")


def map_repo_name(rel, prefix=""):
    """group/sub/repo -> [prefix]group-sub-repo (org segment already dropped)."""
    return prefix + "-".join(rel.split("/"))


GH_TOKEN_HINT = (" (check GitHub token: classic PAT needs repo scope; "
                 "fine-grained needs Administration + Contents write)")


def ensure_github_repo(cfg, name, desc):
    """Create private repo under the PAT user if missing. Returns True on success.

    An existing repo that is not private raises before any push: --mirror would
    publish the private source history into that repo, and a public disclosure
    cannot be undone. Never write first and check later.
    """
    headers = {
        "Authorization": f"Bearer {cfg['gh_token']}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "codeup2github",
    }
    status, body = http_json("GET", f"{cfg['gh_api_base']}/repos/{cfg['gh_user']}/{name}", headers)
    if status == 200:
        # accept only an affirmative private report: `private` alone also covers
        # instance-internal repos, and a body missing either field is not proof
        repo = body if isinstance(body, dict) else {}
        private, visibility = repo.get("private"), repo.get("visibility") or "unknown"
        if private is not True or visibility != "private":
            raise MigrateError(f"existing repo {cfg['gh_user']}/{name} is not confirmed private "
                               f"(private={private!r}, visibility={visibility}); refusing to push "
                               "(make it private or rename it, then rerun)")
        return True
    if status != 404:
        hint = GH_TOKEN_HINT if status == 401 else ""
        raise MigrateError(f"check repo {name} failed (HTTP {status}){hint}")
    status, body = http_json("POST", f"{cfg['gh_api_base']}/user/repos", headers,
                             {"name": name, "description": desc or "", "private": True})
    if status == 201:
        return True
    if status == 422 and "already exists" in str(body):
        # the repo appeared between the existence check and the create, so its
        # visibility was never confirmed; rerunning re-checks it above
        raise MigrateError(f"repo {name} already exists but was not visible to the "
                           "existence check; refusing to push (rerun to re-check it)")
    hint = GH_TOKEN_HINT if status == 401 else ""
    raise MigrateError(f"create repo {name} failed (HTTP {status}){hint}: {body}")


def run_git(args, cwd=None):
    ret = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding="utf-8", cwd=cwd)
    if ret.returncode != 0:
        raise MigrateError(f"git {args[1]} failed: {ret.stderr.strip()}")
    return ret


def with_basic_auth(base, user, token):
    """Embed user:token credentials into a base URL (colon kept literal)."""
    cred = (f"{urllib.parse.quote(user, safe='')}:"
            f"{urllib.parse.quote(token, safe='')}")
    return base.replace("://", f"://{cred}@", 1)


def codeup_clone_url(cfg, repo):
    base = with_basic_auth(cfg["git_base"], cfg["codeup_git_user"], cfg["codeup_token"])
    # codeup git clone path is {base}/codeup/{orgId}/{group}/{repo}.git
    pwn = repo["path_with_namespace"]
    path = f"codeup/{pwn}" if cfg.get("git_codeup_prefix", True) else pwn
    return f"{base}/{path}.git"


def github_push_url(cfg, name):
    if cfg.get("gh_push_ssh"):
        host = urllib.parse.urlparse(cfg["gh_git_base"]).netloc
        return f"git@{host}:{cfg['gh_user']}/{name}.git"
    base = with_basic_auth(cfg["gh_git_base"], cfg["gh_user"], cfg["gh_token"])
    return f"{base}/{cfg['gh_user']}/{name}.git"


def clone_repo(cfg, repo):
    """Bare-clone one repo under {clone_dir}/{path_with_namespace}.git; returns the dir.

    Retries on transient Codeup server drops (tmp_pack fetch failures) with
    backoff; a failed attempt leaves a partial dir which is removed first.
    """
    clone_dir = os.path.join(cfg["clone_dir"], repo["path_with_namespace"] + ".git")
    env = dict(os.environ)
    if cfg["insecure_tls"]:
        # self-signed cert on private Codeup deployment
        env["GIT_SSL_NO_VERIFY"] = "1"
    last_err = ""
    for attempt in range(1, CLONE_RETRIES + 1):
        if os.path.exists(clone_dir):
            shutil.rmtree(clone_dir, ignore_errors=True)
        print(f"  cloning {repo['path_with_namespace']}"
              + (f" (attempt {attempt}/{CLONE_RETRIES})" if attempt > 1 else ""),
              file=sys.stderr)
        try:
            ret = subprocess.run(["git", "clone", "--bare", codeup_clone_url(cfg, repo),
                                  clone_dir],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 encoding="utf-8", env=env,
                                 timeout=CLONE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            last_err = f"clone timed out after {CLONE_TIMEOUT_SECONDS}s"
            if attempt < CLONE_RETRIES:
                time.sleep(CLONE_BACKOFF_SECONDS * attempt)
                continue
            break
        if ret.returncode == 0:
            return clone_dir
        last_err = ret.stderr.strip()
        if "already exists" not in last_err and attempt < CLONE_RETRIES:
            time.sleep(CLONE_BACKOFF_SECONDS * attempt)
    raise MigrateError(f"git clone failed: {last_err}")


def push_repo(cfg, repo, name, clone_dir):
    """Mirror-push a cloned bare repo to its GitHub target."""
    print(f"  pushing {name}", file=sys.stderr)
    run_git(["git", "push", "--mirror", github_push_url(cfg, name)], cwd=clone_dir)


def confirm_migration(args, repos, cfg):
    """Prompt gate for the irreversible mirror push.

    Only prompts when both stdin and stdout are TTY and --yes is absent;
    scripted (non-TTY) runs proceed — the target set is explicit via config.
    """
    if args.yes:
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    answer = input(f"\nMigrate {len(repos)} repo(s) to "
                   f"{urllib.parse.urlparse(cfg['gh_git_base']).netloc}/{cfg['gh_user']} "
                   "(private repos, mirror push overwrites targets)? [y/N] ")
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


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Codeup group repos (incl. subgroups) to GitHub private repos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --dry-run                  list repos and name mapping, no changes
  %(prog)s --dry-run --include-archived
  %(prog)s --yes                      migrate without the confirmation prompt

exit codes:
  0 success   1 migration failures   2 usage error   130 cancelled

config: ./migrate.ini by default (see migrate.example.ini);
token values may come from env CODEUP_ACCESS_TOKEN / GITHUB_TOKEN.""")
    parser.add_argument("-c", "--config", default="migrate.ini", help="config file path")
    parser.add_argument("--dry-run", action="store_true",
                        help="only list repos and show name mapping")
    parser.add_argument("--include-archived", action="store_true",
                        help="also migrate archived repos")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt (non-interactive runs never prompt)")
    args = parser.parse_args()

    try:
        run_git(["git", "--version"])
        cfg = load_config(args.config)
        acquire_lock(cfg["clone_dir"])
    except (MigrateError, OSError) as e:
        # usage/config/environment problems: actionable message on stderr, stable
        # exit 2 (OSError: no git binary, unreadable config path)
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        repos = list_codeup_repos(cfg)
        if not args.include_archived:
            repos = [r for r in repos if not r["archived"]]
        # name collision check: a flattened name must map to exactly one repo
        seen = {}
        for r in repos:
            name = map_repo_name(r["rel"], cfg["repo_prefix"])
            if name in seen:
                raise MigrateError(f"name collision after flatten: {name} <- "
                                   f"{seen[name]} and {r['path_with_namespace']}")
            seen[name] = r["path_with_namespace"]
    except (MigrateError, OSError) as e:
        # enumeration/endpoint problems and flatten collisions stop the run before
        # any repo is touched: one actionable line, stable exit 2
        print(f"error: {mask(mask(str(e), cfg['codeup_token']), cfg['gh_token'])}",
              file=sys.stderr)
        sys.exit(2)
    print(f"endpoints: api={cfg['api_base']} clone={cfg['git_base']} "
          f"push={'ssh:' + urllib.parse.urlparse(cfg['gh_git_base']).netloc if cfg['gh_push_ssh'] else cfg['gh_git_base']}",
          file=sys.stderr)
    print(f"found {len(repos)} repo(s) under "
          f"{cfg['group_path'] or '<all visible>'}\n")

    for r in repos:
        name = map_repo_name(r["rel"], cfg["repo_prefix"])
        flag = " [archived]" if r["archived"] else ""
        print(f"  {r['path_with_namespace']:<60} -> {cfg['gh_user']}/{name}{flag}")
    if args.dry_run:
        return

    if not repos:
        print("nothing to migrate", file=sys.stderr)
        return
    try:
        if not confirm_migration(args, repos, cfg):
            print("cancelled", file=sys.stderr)
            sys.exit(130)
    except EOFError:
        print("cancelled", file=sys.stderr)
        sys.exit(130)

    ok, failed = 0, []

    def record_failure(repo, exc):
        err = mask(mask(str(exc), cfg["codeup_token"]), cfg["gh_token"])
        failed.append((repo["path_with_namespace"], err))
        print(f"  FAILED: {err}", file=sys.stderr)

    # phase 1: clone everything first (disk-to-disk, no remote writes yet)
    print(f"\nphase 1/2: cloning {len(repos)} repo(s) into {cfg['clone_dir']}", file=sys.stderr)
    cloned = []
    for r in repos:
        try:
            clone_dir = clone_repo(cfg, r)
            cloned.append((r, clone_dir))
        except (MigrateError, OSError, subprocess.SubprocessError) as e:
            record_failure(r, e)

    # phase 2: push smallest first — quick wins land early, big repos run unattended
    cloned.sort(key=lambda rc: rc[0]["size_mb"] if rc[0]["size_mb"] is not None
                else float("inf"))
    print(f"\nphase 2/2: pushing {len(cloned)} repo(s), smallest first", file=sys.stderr)
    for r, clone_dir in cloned:
        name = map_repo_name(r["rel"], cfg["repo_prefix"])
        size = f" ({r['size_mb']:.0f}MB)" if r["size_mb"] is not None else ""
        print(f"pushing {r['path_with_namespace']} -> {name}{size}", file=sys.stderr)
        try:
            ensure_github_repo(cfg, name, r["description"])
            push_repo(cfg, r, name, clone_dir)
            ok += 1
            print(f"  done: {name}", file=sys.stderr)
        except (MigrateError, OSError, subprocess.SubprocessError) as e:
            record_failure(r, e)

    print(f"\nsummary: {ok} success, {len(failed)} failed", file=sys.stderr)
    for path, err in failed:
        print(f"  FAILED {path}: {err[:200]}", file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
