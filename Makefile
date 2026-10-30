.PHONY: build check test doctor system-package
build:
	cargo build --workspace --locked
check:
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings
test:
	cargo test --workspace --locked
	npm run test:api
doctor:
	cargo run --locked -p kane-supervisor -- doctor --json
system-package:
	cargo build --release --locked -p kane-supervisor
	python3 scripts/build-system-package.py
