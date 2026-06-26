# Contributing

Thanks for wanting to help.

## Setup

```bash
git clone https://github.com/m0rvayne/ghostmic.git
cd ghostmic
python3 -m venv .test-venv
.test-venv/bin/pip install -e ".[test]"
.test-venv/bin/pytest -v
```

## Running tests

```bash
.test-venv/bin/pytest -v          # Python tests (86 tests)
bash -n install.sh                # Shell syntax check
swiftc -O -framework CoreAudio -framework AudioToolbox -framework AppKit process-audio-tap.swift -o /tmp/test-tap  # Swift audio tap
swiftc -O -framework AppKit statusbar.swift -o /tmp/test-statusbar  # Swift menu bar
```

## Code style

- No linter enforced yet, just keep it readable
- Write tests for new functionality
- Run the full test suite before submitting a PR

## Pull requests

1. Fork the repo
2. Create a branch from `main`
3. Make your changes
4. Run tests
5. Open a PR with a clear description of what changed and why
