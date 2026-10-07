"""Query source PDOs in one CDC session using the sweep initialization sequence."""
import argparse
from contextlib import ExitStack
from pathlib import Path

from .protocol import ProtocolError
from .transport import SerialTransport, format_ascii_response
from .sweep import add_trigger_options, initialization_commands, initialize_trigger, release_trigger


def add_pdo_options(parser, connection_options, positive_float, nonnegative_float):
    connection_options(parser, cdc_only=True)
    add_trigger_options(parser, positive_float, nonnegative_float, allow_no_initialize=False)
    parser.set_defaults(pps_sweep='')
    parser.add_argument('--epr', action=argparse.BooleanOptionalAction, default=True,
                        help='wait additionally for PDOs above 20 V; retain SPR PDOs on timeout (default: on)')
    parser.add_argument('--dry-run', action='store_true', help='print initialization/cleanup; no hardware or files')
    parser.add_argument('--quiet', action='store_true', help='show final PDO response only')
    parser.add_argument('--response-file', help='save the exact final valid PDO response bytes')
    parser.add_argument('--force', action='store_true', help='overwrite --response-file')


def run_pdo(args):
    if args.dry_run:
        for command in initialization_commands(args):
            print(command)
        print('PDO wait: valid response' + ('; also wait for EPR PDOs above 20 V' if args.epr else ''))
        print('Exit cleanup: ' + ('keep trigger' if args.keep_trigger else 'reset -> pdm close'))
        return 0
    path = Path(args.response_file).expanduser() if args.response_file else None
    if path and path.exists() and (path.is_dir() or not args.force):
        raise FileExistsError(f'{path} exists; choose another --response-file or use --force')
    with ExitStack() as stack:
        serial = SerialTransport(args)
        stack.callback(serial.close)
        serial.open()
        try:
            response = initialize_trigger(serial, args, [], require_pdo=True,
                                          wait_for_epr=args.epr and
                                          args.type in (None, 0, 2) and args.em in (None, 2))
        finally:
            cleanup = release_trigger(serial, args)
        # Show the final valid response even if a later EPR poll had no response.
        print('Source PDOs:')
        print(format_ascii_response(response))
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('wb' if args.force else 'xb') as handle:
                handle.write(response)
            print(f'PDO response: {path.resolve()}')
        if cleanup['status'] == 'failed':
            raise ProtocolError('Trigger cleanup failed: ' + '; '.join(cleanup['errors']))
    return 0
