"""Load a project-local ``.env`` into ``os.environ``. Standard library only.

README and SECURITY.md both tell the user to ``cp .env.example .env``, but
nothing in the project ever read that file: there is no ``python-dotenv``
dependency and no ``load_dotenv()`` call anywhere. Anyone who followed the
documented setup ended up with empty credentials.

Implemented here rather than by adding a dependency, so the supply-chain
surface does not grow for ~40 lines of parsing.

Precedence: a variable already present in the real environment always wins, so
``export BINANCE_API_KEY=...`` overrides the file and CI stays in control.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / '.env'

_loaded = False


def parse_env(text):
    """Parse ``KEY=VALUE`` lines. Supports ``export`` prefixes, ``#`` comments
    and single/double quoted values. Returns a dict."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        else:
            value = value.split(' #')[0].strip()
        values[key] = value
    return values


def load_env(path=None, override=False):
    """Load ``path`` (default: project ``.env``) into ``os.environ``.

    Returns the number of variables applied. Missing file is not an error --
    exporting the variables directly is an equally valid setup.
    """
    global _loaded
    env_path = Path(path) if path else DEFAULT_ENV_FILE
    try:
        text = env_path.read_text(encoding='utf-8')
    except (FileNotFoundError, NotADirectoryError):
        _loaded = True
        return 0
    except OSError:
        _loaded = True
        return 0
    applied = 0
    for key, value in parse_env(text).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied += 1
    _loaded = True
    return applied


def ensure_loaded():
    """Load once per process. Safe to call from every config module."""
    if not _loaded:
        return load_env()
    return 0
