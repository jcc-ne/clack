"""Entry point for `python -m clack` and the `clack` CLI command."""

from clack.app import ClackApp


def main():
    app = ClackApp()
    app.run()


if __name__ == "__main__":
    main()
