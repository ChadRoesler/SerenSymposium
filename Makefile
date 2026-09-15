# Mirrors the CI matrix locally.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test          - base install only (.[dev])          - matches CI extras=dev
#   make test-desktop  - with the native window (.[dev,desktop])
#   make test-corp     - with CORP TLS extras (.[dev,corp])
#   make test-all      - all three in sequence
#   make shapes        - what CI's install-shapes job proves: construct the app
#                        from OUTSIDE the repo, on a bare install, so the source
#                        tree cannot mask a missing runtime dependency
#   make clean         - remove any leftover venvs
#
# Each target creates a fresh isolated venv, runs tests, then removes it. A venv
# is the point: this repo's most expensive past bug was a satisfied-but-stale
# dependency floor that only a clean resolve reveals.
# Venvs are also gitignored as a belt-and-suspenders safety net.

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR      := SerenSymposium
VENV_BASE    := .venv-base
VENV_DESKTOP := .venv-desktop
VENV_CORP    := .venv-corp
VENV_SHAPES  := .venv-shapes

.PHONY: test test-desktop test-corp test-all shapes clean

test:
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_BASE); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-base\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev]"; \
	.\.venv-base\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	exit $$status

test-desktop:
	Remove-Item -Recurse -Force $(VENV_DESKTOP) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_DESKTOP); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-desktop\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev,desktop]"; \
	.\.venv-desktop\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_DESKTOP) -ErrorAction SilentlyContinue; \
	exit $$status

test-corp:
	Remove-Item -Recurse -Force $(VENV_CORP) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_CORP); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-corp\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev,corp]"; \
	.\.venv-corp\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_CORP) -ErrorAction SilentlyContinue; \
	exit $$status

# NOT an editable install, and deliberately run from a temp directory: this is
# the leg that catches an unconditional import of an optional dependency.
shapes:
	Remove-Item -Recurse -Force $(VENV_SHAPES) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_SHAPES); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-shapes\Scripts\pip.exe install "$(PKG_DIR)/."; \
	.\.venv-shapes\Scripts\pip.exe install httpx; \
	$$venv=(Resolve-Path .\.venv-shapes\Scripts\python.exe).Path; \
	Push-Location $$env:TEMP; \
	& $$venv -c "from seren_symposium.app import create_app; from fastapi.testclient import TestClient; app=create_app(); c=TestClient(app); c.__enter__(); r=c.get('/api/config'); assert r.status_code==200, r.text; u=r.json()['updates']; assert u['status'] in {'ok','disabled','error'}, u; print('bare install OK, updates status=' + u['status'])"; \
	$$status=$$LASTEXITCODE; \
	Pop-Location; \
	Remove-Item -Recurse -Force $(VENV_SHAPES) -ErrorAction SilentlyContinue; \
	exit $$status

test-all: test test-desktop test-corp

clean:
	Remove-Item -Recurse -Force $(VENV_BASE), $(VENV_DESKTOP), $(VENV_CORP), $(VENV_SHAPES) -ErrorAction SilentlyContinue
