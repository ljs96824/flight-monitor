"""Run the minimal subscription form service."""

from log_utils import configure_stdio_utf8

configure_stdio_utf8()

from web_form import app


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

