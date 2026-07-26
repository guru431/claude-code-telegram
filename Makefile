.PHONY: install dev hooks test lint format clean help run run-debug run-remote remote-attach remote-stop \
       bump-patch bump-minor bump-major release version

# Default target
help:
	@echo "Available commands:"
	@echo "  install       - Install production dependencies"
	@echo "  dev           - Install development dependencies + activate git hooks"
	@echo "  hooks         - Activate the .githooks secret-guard for this clone"
	@echo "  test          - Run tests"
	@echo "  lint          - Run linting checks"
	@echo "  format        - Format code"
	@echo "  clean         - Clean up generated files"
	@echo "  run           - Run the bot"
	@echo "  version       - Show current version"
	@echo "  bump-patch    - Bump patch version (1.2.0 -> 1.2.1), commit, and tag"
	@echo "  bump-minor    - Bump minor version (1.2.0 -> 1.3.0), commit, and tag"
	@echo "  bump-major    - Bump major version (1.2.0 -> 2.0.0), commit, and tag"
	@echo "  release       - Push current version tag to trigger release workflow"
	@echo "  run-remote    - Start bot in tmux on remote Mac (unlocks keychain)"
	@echo "  remote-attach - Attach to running bot tmux session"
	@echo "  remote-stop   - Stop the bot tmux session"

# --no-dev was removed from Poetry; current releases express the same thing as
# a dependency-group selector. Using the removed flag makes the documented
# production entry point fail outright on a modern Poetry.
install:
	poetry install --only main

dev: hooks
	poetry install

# Activate the in-repo secret-guard. This is NOT the pre-commit framework:
# the hook lives in .githooks/ and core.hooksPath overrides .git/hooks
# entirely, so the two mechanisms are mutually exclusive. Fail loudly --
# a clone that silently ends up without the leak scanner is the whole risk.
hooks:
	git config core.hooksPath .githooks
	@test -x .githooks/pre-commit || (echo "ERROR: .githooks/pre-commit missing or not executable" && exit 1)
	@echo "secret-guard active (core.hooksPath=.githooks)"

test:
	poetry run pytest

lint:
	poetry run black --check src tests
	poetry run isort --check-only src tests
	poetry run flake8 src tests
	poetry run mypy src

format:
	poetry run black src tests
	poetry run isort src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

run:
	poetry run claude-telegram-bot

# For debugging
run-debug:
	poetry run claude-telegram-bot --debug

# Remote Mac Mini (SSH session)
run-remote:  ## Start bot on remote Mac in tmux (persists after SSH disconnect)
	security unlock-keychain ~/Library/Keychains/login.keychain-db
	tmux new-session -d -s claude-bot 'poetry run claude-telegram-bot'
	@echo "Bot started in tmux session 'claude-bot'"
	@echo "  Attach: make remote-attach"
	@echo "  Stop:   make remote-stop"

remote-attach:  ## Attach to running bot tmux session
	tmux attach -t claude-bot

remote-stop:  ## Stop the bot tmux session
	tmux kill-session -t claude-bot

# --- Version Management ---

version:  ## Show current version
	@poetry version -s

bump-patch:  ## Bump patch version, commit, and tag
	poetry version patch && \
	NEW_VERSION=$$(poetry version -s) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

bump-minor:  ## Bump minor version, commit, and tag
	poetry version minor && \
	NEW_VERSION=$$(poetry version -s) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

bump-major:  ## Bump major version, commit, and tag
	poetry version major && \
	NEW_VERSION=$$(poetry version -s) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

release:  ## Push the current version tag to trigger the release workflow
	CURRENT_VERSION=$$(poetry version -s) && \
	git push && git push origin "v$$CURRENT_VERSION" && \
	echo "Pushed v$$CURRENT_VERSION. Release workflow will run on GitHub."
