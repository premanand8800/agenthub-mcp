import io
import unittest
from unittest import mock

from agenthub import cli


class _Stdin(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__("")
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class GatewayEntryPointTests(unittest.TestCase):
    def run_gateway(self, argv, tty):
        with mock.patch.object(cli, "main", return_value=0) as main, mock.patch("sys.stdin", _Stdin(tty)):
            cli.gateway_main(argv)
        return main.call_args.args[0]

    def test_no_args_from_mcp_client_starts_server(self):
        self.assertEqual(self.run_gateway([], tty=False), ["mcp"])

    def test_no_args_in_terminal_shows_help(self):
        self.assertEqual(self.run_gateway([], tty=True), [])

    def test_explicit_arguments_pass_through(self):
        self.assertEqual(self.run_gateway(["agents", "--json"], tty=False), ["agents", "--json"])

    def test_plain_agenthub_still_prints_help_without_args(self):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main([]), 2)
