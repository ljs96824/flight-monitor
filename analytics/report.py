def _load_run_cli():
    if __package__:
        from .report_lib import run_cli
    else:
        from report_lib import run_cli

    return run_cli


def main(argv=None) -> int:
    return _load_run_cli()(argv)


if __name__ == "__main__":
    raise SystemExit(main())
