"""Find compiled (DLL) packages that Windows Application Control blocks.

Prints one line per package: OK / BLOCKED / MISSING. Exit code 1 if anything is blocked.
"""
import importlib
import sys

# packages with native .pyd files that this project loads
CANDIDATES = ["pydantic_core", "orjson", "ormsgpack", "xxhash", "zstandard", "uuid_utils", "regex",
              "yaml", "charset_normalizer", "jiter", "tiktoken", "websockets", "httptools", "watchfiles"]

blocked = 0
for name in CANDIDATES:
    try:
        importlib.import_module(name)
        mod = sys.modules[name]
        print(f"OK {name} {getattr(mod, '__version__', '')}")
    except ModuleNotFoundError:
        print(f"MISSING {name}")
    except Exception as e:  # DLL load failed / Application Control
        blocked += 1
        print(f"BLOCKED {name}: {e}")

# the imports the app actually needs
for label, stmt in [("fastapi", "import fastapi"), ("langgraph", "from langgraph.checkpoint.memory import InMemorySaver"),
                    ("langchain", "from langchain.agents import create_agent"), ("app", "import incident_agent.api")]:
    try:
        exec(stmt)
        print(f"OK import:{label}")
    except Exception as e:
        blocked += 1
        print(f"FAIL import:{label}: {type(e).__name__}: {e}")
sys.exit(1 if blocked else 0)
