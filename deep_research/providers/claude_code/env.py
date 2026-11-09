"""Child-process environment and binary lookup.

Credentials: nothing is read, stored or logged. The unmodified ``claude`` binary signs in by itself
(its own files under HOME). Children get an ALLOWLISTED environment, never the caller's full one, so
provider keys in plain variables never reach them. Pattern vendored from swarm-framework
(``src/swarmfw/supervisor.py``, ``agent_env``)."""
import os
import re
import shutil
from typing import Optional

ENV_EXACT = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ", "LANG", "PWD",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
             "NODE_USE_ENV_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
             "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO", "AWS_CA_BUNDLE"}
ENV_PREFIX = ("LC_", "CLAUDE_CODE_", "DEEPDOG_CC_")
SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
OPT_IN_VAR = "DEEPDOG_ENV_PASS"
BIN_VAR = "DEEPDOG_CLAUDE_BIN"


def agent_env(environ: Optional[dict] = None, extra: Optional[dict] = None) -> dict:
    """Allowlisted environment for ``claude`` child processes.

    Names that look secret never pass by pattern. Only the owner can opt names in, through their own
    environment: ``DEEPDOG_ENV_PASS="NAME1,NAME2"`` (accepting that the child can then see them).
    ``extra`` is applied last and is for values this package sets itself (e.g. an output-token limit)."""
    env = os.environ if environ is None else environ
    opted = {n.strip() for n in env.get(OPT_IN_VAR, "").split(",") if n.strip()}
    out = {}
    for k, v in env.items():
        if k == OPT_IN_VAR:
            continue
        if k in opted:
            out[k] = v
        elif (k in ENV_EXACT or k.startswith(ENV_PREFIX)) and not SECRETISH.search(k):
            out[k] = v
    if extra:
        out.update({k: str(v) for k, v in extra.items()})
    return out


def resolve_binary(environ: Optional[dict] = None) -> Optional[str]:
    """Path of the ``claude`` binary: ``DEEPDOG_CLAUDE_BIN`` if set, else ``claude`` on PATH."""
    env = os.environ if environ is None else environ
    explicit = (env.get(BIN_VAR) or "").strip()
    if explicit:
        return explicit
    return shutil.which("claude", path=env.get("PATH"))
