.PHONY: help build check publish-test smoke-test-testpypi release-create release-create-draft release-status release-view clean

PACKAGE := clack-tui
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
TEST_PYPI_SIMPLE := https://test.pypi.org/simple
PYPI_SIMPLE := https://pypi.org/simple

help:
	@printf "Targets:\n"
	@printf "  make build               Build wheel and sdist with uv\n"
	@printf "  make check               Run twine metadata checks on dist artifacts\n"
	@printf "  make publish-test        Upload current version artifacts to TestPyPI\n"
	@printf "  make smoke-test-testpypi Install and run from TestPyPI via uvx\n"
	@printf "  make release-create      Create and publish GitHub release for current version\n"
	@printf "  make release-create-draft Create GitHub draft release for current version\n"
	@printf "  make release-status      List recent GitHub publish workflow runs\n"
	@printf "  make release-view        View the latest GitHub release\n"
	@printf "  make clean               Remove build artifacts\n"

build:
	uv build

check:
	uvx twine check dist/*

publish-test: build check
	uvx twine upload -r testpypi dist/$(subst -,_,$(PACKAGE))-$(VERSION)*

smoke-test-testpypi:
	uvx --refresh-package $(PACKAGE) --index-url $(TEST_PYPI_SIMPLE) --extra-index-url $(PYPI_SIMPLE) $(PACKAGE)==$(VERSION)

release-create:
	gh release create v$(VERSION) --target main --title "v$(VERSION)" --generate-notes

release-create-draft:
	gh release create v$(VERSION) --target main --title "v$(VERSION)" --generate-notes --draft

release-status:
	gh run list --workflow publish.yml --limit 5

release-view:
	gh release view v$(VERSION)

clean:
	rm -rf build dist
