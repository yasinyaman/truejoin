"""``python -m trio``: the same command line as the ``trio`` script."""

from trio.adapters.inbound.cli.main import main

if __name__ == "__main__":
    main()
