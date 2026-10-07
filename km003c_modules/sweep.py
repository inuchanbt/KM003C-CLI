"""ASD-style PPS/AVS voltage sweeps using KM003C's ASCII PD trigger.

Expression current is a PD request, not an electronic-load setting. Source PDO
selection is explicit; command delivery does not prove protocol acceptance.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import argparse
import csv
import json
from pathlib import Path
import re
import signal
import sys
import threading
import time

from .protocol import ADC, ProtocolError, decode_adc, logical_packets
from .transport import Meter, SerialTransport, encode_ascii_command, format_ascii_response


SWEEP_COLUMNS = [
    'timestamp', 'controller_version', 'run_started_at', 'source_name', 'cable_name',
    'test_note', 'mode', 'sweep_leg', 'sweep_pass', 'sweep_direction', 'phase_elapsed_s',
    'target_voltage_v', 'target_load_current_a', 'request_current_a',
    'actual_voltage_v', 'actual_current_a', 'actual_power_w', 'pdo_kind',
    'pdo_object_number', 'raw_measure_response', 'command', 'raw_command_response',
    'request_status', 'measurement_index', 'error', 'request_sent_at',
]


@dataclass(frozen=True)
class SweepPoint:
    voltage_mv: int
    current_ma: int
    leg: str
    sweep_pass: int
    direction: str

    def command(self, pdo_index):
        return f'pd req={pdo_index},volt={self.voltage_mv},cur={self.current_ma}'


def add_sweep_options(parser, connection_options, positive_float, nonnegative_float,
                      positive_int, nonnegative_int):
    connection_options(parser, cdc_only=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--sweep', help='AVS range in V/A: start:end:step[:current]')
    source.add_argument('--pps-sweep', default='', help='PPS range in V/A: start:end:step[:current]')
    parser.add_argument('--mode', choices=['auto', 'avs', 'var'], default='auto',
                        help='ASD mode; --sweep uses AVS, --pps-sweep uses PPS')
    parser.add_argument('--pdo-index', type=positive_int, required=True,
                        help='source APDO ObjectPosition (check with pd --pdo)')
    parser.add_argument('--request-current', type=nonnegative_float,
                        help='PD request current in A; overrides expression current')
    parser.add_argument('--round-trip-sweep', action='store_true')
    parser.add_argument('--continuous-sweep', action='store_true',
                        help='keep the PDO/current request; measure each target')
    parser.add_argument('--continuous-settle', type=nonnegative_float, default=0.5)
    parser.add_argument('--apdo-voltage-hold', type=nonnegative_float, default=0.0,
                        help='minimum seconds per target, including replies/measurements')
    parser.add_argument('--measure', action='store_true', help='one ADC read per target')
    parser.add_argument('--measure-loop', type=nonnegative_int, default=0)
    parser.add_argument('--delay', type=nonnegative_float, default=0.5)
    parser.add_argument('--measurement-transport', choices=['hid', 'usb'], default='hid')
    parser.add_argument('--wait', type=positive_float, default=1.0,
                        help='ASCII reply read window in seconds (included in hold; excludes entry pd)')
    parser.add_argument('--entry-timeout', type=positive_float, default=10.0,
                        help='maximum seconds to wait for entry pd ready (default: 10)')
    parser.add_argument('--pdm-startup-wait', type=nonnegative_float, default=2.0,
                        help='minimum PDM startup reply window in seconds (default: 2)')
    parser.add_argument('--type', type=int, choices=[0,1,2,3],
                        help='PDM protocol: default PD3.1 for AVS, PD3.0 for PPS')
    parser.add_argument('--em', type=int, choices=[0,1,2],
                        help='e-marker simulation: 0 off, 1 20V5A, 2 EPR; default 2 AVS / 1 PPS')
    parser.add_argument('--sink', type=int, choices=[0,1], default=1,
                        help='Sink capabilities: 0 3A PPS, 1 5A PPS (default)')
    parser.add_argument('--initialize', action=argparse.BooleanOptionalAction, default=True,
                        help='open/configure PDM, enter PD and query PDOs; restart PDM if already busy')
    parser.add_argument('--keep-trigger', action=argparse.BooleanOptionalAction, default=False,
                        help='keep the last PD request on exit; default resets and closes PDM')
    parser.add_argument('--dry-run', action='store_true', help='print plan; no hardware or files')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--csv', help='default: unique sweep filename in captures/')
    output.add_argument('--no-csv', action='store_true')
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument('--csv-overwrite', dest='csv_mode', action='store_const', const='overwrite')
    policy.add_argument('--csv-append', dest='csv_mode', action='store_const', const='append')
    parser.set_defaults(csv_mode='error')
    parser.add_argument('--quiet', action='store_true')
    for name in ('source-name', 'cable-name', 'test-note'):
        parser.add_argument('--' + name, default='')


def _decimal(value, name):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be numeric') from exc
    if not number.is_finite():
        raise ValueError(f'{name} must be finite')
    return number


def _milli(value, name):
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + 3)
        milli = value * 1000
    if milli != milli.to_integral_value():
        raise ValueError(f'{name} must have whole mV/mA precision')
    return int(milli)


def build_plan(args):
    expression = args.pps_sweep or args.sweep
    parts = expression.split(':')
    if len(parts) not in (3, 4):
        raise ValueError('Sweep must be start:end:step[:current] in V/A')
    start, end, step = [_decimal(value, 'Sweep voltage/step') for value in parts[:3]]
    if step == 0 or (start < end and step < 0) or (start > end and step > 0):
        raise ValueError('Sweep step must be nonzero and point toward the end voltage')
    explicit_current = _decimal(parts[3], 'Sweep current') if len(parts) == 4 else None
    if explicit_current is not None and explicit_current < 0:
        raise ValueError('Sweep current must be non-negative')
    current = (_decimal(args.request_current, 'Request current') if args.request_current is not None
               else explicit_current if explicit_current is not None
               else Decimal('1') if args.pps_sweep else None)
    if current is None:
        raise ValueError('AVS sweep requires :current or --request-current (A)')
    if not 0 <= current <= Decimal('6.35'):
        raise ValueError('PPS/AVS request current must be 0..6.35 A')
    current_ma = _milli(current, 'Request current')
    if not 0 <= current_ma <= 6350:
        raise ValueError('PPS/AVS request current must be 0..6.35 A')
    if not 1 <= args.pdo_index <= 15:
        raise ValueError('--pdo-index must be 1..15')
    if not (0 < start <= Decimal('65.535') and 0 < end <= Decimal('65.535')):
        raise ValueError('Voltage must be greater than 0 and at most 65.535 V')
    if step.copy_abs() > Decimal('65.535'):
        raise ValueError('Sweep step must not exceed 65.535 V')
    first, last, stride = [_milli(v, 'Sweep voltage/step') for v in (start, end, step)]
    if not (0 < first <= 65535 and 0 < last <= 65535):
        raise ValueError('Voltage must be greater than 0 and at most 65.535 V')
    count = abs(last - first) // abs(stride) + 1
    if count * (2 if args.round_trip_sweep else 1) > 100000:
        raise ValueError('Sweep exceeds 100000 points; increase the step')
    outward = [first + i * stride for i in range(count)]
    direction = f'{float(start):g}V->{outward[-1] / 1000:g}V'
    points = [SweepPoint(v, current_ma, 'outbound', 1, direction) for v in outward]
    if args.round_trip_sweep and len(outward) > 1:
        backward = outward[-2::-1]
        direction = f'{backward[0] / 1000:g}V->{first / 1000:g}V'
        points.extend(SweepPoint(v, current_ma, 'return', 2, direction) for v in backward)
    return points


def validate_sweep(args):
    build_plan(args)
    if args.csv_mode != 'error' and not args.csv:
        raise ValueError('--csv-overwrite/--csv-append requires --csv')


def _read_reply(transport, seconds, response, *, stop_when=None):
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        response.extend(transport.read(min(.05, remaining)))
        if len(response) > 1024 * 1024:
            raise ProtocolError('ASCII reply exceeds size limit')
        if stop_when is not None and stop_when(response):
            break


def _rejects(response):
    return bool(re.search(r'^\s*(?:error|fail(?:ed|ure)?|false|nak|reject(?:ed)?|invalid|unsupported)\b',
                          response.decode('utf-8', errors='replace'), re.I | re.M))


def _ready(response):
    return bool(re.search(rb'(?:^|[\r\n:>])\s*ready\b', response, re.I))


def initialization_commands(args):
    if not args.initialize:
        return []
    protocol_type = args.type if args.type is not None else (1 if args.pps_sweep else 2)
    em = args.em if args.em is not None else (1 if args.pps_sweep else 2)
    return ['pdm open', f'pdm set type={protocol_type},em={em},sink={args.sink}',
            'entry pd', 'pd pdo']


def initialize_trigger(serial, args, setup_replies):
    """Configure the trigger explicitly and retain each setup exchange."""
    def exchange(command, seconds=None):
        response = bytearray()
        entry = {'command': command}
        setup_replies.append(entry)
        if not args.quiet:
            print(command, flush=True)
        try:
            serial.write(encode_ascii_command(command))
            if command == 'entry pd':
                _read_reply(serial, args.entry_timeout, response,
                            stop_when=lambda data: _rejects(data) or _ready(data))
            else:
                window = seconds if seconds is not None else (
                    max(args.wait, args.pdm_startup_wait) if command == 'pdm open' else args.wait)
                _read_reply(serial, window, response)
        finally:
            entry['response_hex'] = response.hex(' ')
        if not args.quiet:
            print(format_ascii_response(response) or '(no reply)')
        if _rejects(response):
            raise ProtocolError(f'Device rejected {command}')
        return response

    def busy(response):
        return bool(re.search(rb'^\s*pdm busy\s*$', response, re.I | re.M))

    points = build_plan(args)
    voltage_min = min(p.voltage_mv for p in points)
    voltage_max = max(p.voltage_mv for p in points)
    needs_epr_avs = not args.pps_sweep and voltage_max > 20000

    def epr_avs_ready(response):
        # The first ready can precede EPR entry. The textual pd pdo reply
        # exposes ranges while omitting reserved PDO slots; do not infer indices.
        ranges = re.findall(rb'^AVS:\s*(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)V',
                            response, re.M | re.I)
        return any(Decimal(low.decode('ascii')) * 1000 <= voltage_min and
                   Decimal(high.decode('ascii')) * 1000 >= voltage_max
                   for low, high in ranges)

    for command in initialization_commands(args):
        response = exchange(command)
        if command == 'pdm open' and busy(response):
            # --initialize requests a fresh negotiation. A running trigger must
            # be closed before reopening; preserve it with --no-initialize.
            exchange('pdm close')
            response = exchange('pdm open')
            if busy(response):
                raise ProtocolError('PDM remained busy after close/open; check the device trigger state')
        if command == 'pd pdo' and needs_epr_avs:
            deadline = time.monotonic() + args.entry_timeout
            while not epr_avs_ready(response):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError(
                        'EPR AVS capabilities covering the sweep did not become ready; '
                        'check source capabilities and --type/--em settings')
                response = exchange('pd pdo', min(args.wait, remaining))
        if command == 'entry pd' and not _ready(response):
            raise ProtocolError(
                f'entry pd did not reply ready within {args.entry_timeout:g}s; '
                'check source/CC connection and PDM settings, or increase --entry-timeout')


CLEANUP_REPLY_SECONDS = 2.0
CLEANUP_LOW_VOLTAGE_LIMIT = 5.5


def release_trigger(serial, args, adc=None):
    """Try both release commands, preserving failures independently of the sweep."""
    result = dict(policy='keep_trigger' if args.keep_trigger else 'reset_then_close',
                  status='skipped' if args.keep_trigger else 'pending', commands=[], errors=[],
                  voltage_verified=False, external_load_controlled=False)
    if args.keep_trigger:
        return result
    def display(message, *, warning=False):
        # A closed/redirected console must not prevent releasing the trigger.
        if args.quiet and not warning:
            return
        try:
            print(message, file=sys.stderr if warning else sys.stdout, flush=True)
        except (OSError, ValueError):
            pass

    # A second Ctrl+C must not skip the close command. Preserve failures for
    # metadata; bound both reply windows and restore the signal handler afterward.
    main_thread = threading.current_thread() is threading.main_thread()
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN) if main_thread else None
    try:
        for command in ('reset', 'pdm close'):
            response = bytearray()
            entry = dict(command=command, sent=False, status='write_failed')
            result['commands'].append(entry)
            try:
                display(f'Cleanup: {command}')
                serial.write(encode_ascii_command(command))
                entry.update(sent=True, status='read_failed')
                _read_reply(serial, CLEANUP_REPLY_SECONDS, response)
                if _rejects(response):
                    entry['status'] = 'device_rejected'
                    raise ProtocolError(f'Device rejected cleanup {command}')
                if not re.search(rb'^\s*ok\s*$', response, re.I | re.M):
                    entry['status'] = 'unacknowledged'
                    raise ProtocolError(f'Cleanup {command} did not reply ok')
                entry['status'] = 'acknowledged'
            except (Exception, KeyboardInterrupt) as exc:
                entry['error'] = 'Ctrl+C during cleanup' if isinstance(exc, KeyboardInterrupt) else str(exc)
                result['errors'].append(entry['error'])
            finally:
                entry['response_hex'] = response.hex(' ')
            if response:
                display(format_ascii_response(response))
        if adc is not None:
            response = bytearray()
            try:
                response.extend(adc.get_data(ADC))
                sample = next((decode_adc(p.payload) for p in logical_packets(response)
                               if p.attribute == ADC), None)
                if sample is None:
                    raise ProtocolError('No ADC payload in cleanup verification')
                result.update(actual_voltage_V=sample.voltage, actual_current_A=sample.current,
                              actual_power_W=sample.power, low_voltage_limit_V=CLEANUP_LOW_VOLTAGE_LIMIT)
                if abs(sample.voltage) > CLEANUP_LOW_VOLTAGE_LIMIT:
                    raise ProtocolError(f'Output remains at {sample.voltage:g} V after cleanup')
                result['voltage_verified'] = True
                display(f'After cleanup: {sample.voltage:.4f} V  {sample.current:.4f} A')
            except (Exception, KeyboardInterrupt) as exc:
                result['errors'].append('Ctrl+C during cleanup verification' if isinstance(exc, KeyboardInterrupt)
                                        else f'Cleanup verification: {exc}')
            finally:
                result['raw_measure_response'] = response.hex(' ')
        result['status'] = 'failed' if result['errors'] else 'acknowledged'
        if result['errors']:
            display('Warning: trigger cleanup failed: ' + '; '.join(result['errors']), warning=True)
    finally:
        if main_thread:
            signal.signal(signal.SIGINT, previous_handler)
    return result


def _paths(args, kind, started):
    if args.no_csv:
        return None, None
    path = Path(args.csv).expanduser() if args.csv else (
        Path('captures') / f'km003c_{kind}_sweep_{started.strftime("%Y%m%d_%H%M%S_%f")}.csv')
    if path.exists():
        if path.is_dir() or args.csv_mode == 'error':
            raise FileExistsError(f'{path} exists; use another --csv or --csv-overwrite/--csv-append')
        if args.csv_mode == 'append':
            with path.open(encoding='utf-8-sig', newline='') as handle:
                if next(csv.reader(handle), None) != SWEEP_COLUMNS:
                    raise ValueError('Existing sweep CSV header does not match')
    if args.csv_mode == 'append':
        meta = Path(str(path) + f'.run_{started.strftime("%Y%m%d_%H%M%S_%f")}.metadata.json')
    else:
        meta = Path(str(path) + '.metadata.json')
    if meta.resolve() == path.resolve() or (meta.exists() and
            (meta.is_dir() or args.csv_mode != 'overwrite')):
        raise FileExistsError(f'Cannot replace metadata: {meta}')
    return path, meta


def run_sweep(args, *, version='unknown'):
    validate_sweep(args)
    points = build_plan(args)
    kind = 'pps' if args.pps_sweep else 'avs'
    setup = initialization_commands(args)
    measurements = args.measure_loop or int(args.measure or args.continuous_sweep)
    print(f'{kind.upper()} sweep: {len(points)} requests, PDO {args.pdo_index}, '
          f'{points[0].current_ma / 1000:g} A requested; electronic load is external')
    if args.dry_run:
        for command in setup:
            print(command)
        for index, point in enumerate(points, 1):
            print(f'{index:5d} [{point.leg}] {point.command(args.pdo_index)}')
        print('Exit cleanup: ' + ('keep last request' if args.keep_trigger else 'reset -> pdm close'))
        print(f'ADC reads per target: {measurements}; minimum hold: {args.apdo_voltage_hold:g}s; '
              f'ASCII reply window: {args.wait:g}s')
        return 0
    started = datetime.now().astimezone()
    path, meta_path = _paths(args, kind, started)
    status, error = 'completed', None
    setup_replies = []
    cleanup = None
    sent, completed, rows, last_voltage = 0, 0, 0, None
    origin = time.monotonic()
    with ExitStack() as stack:
        serial = SerialTransport(args)
        stack.callback(serial.close)
        serial.open()
        writer = handle = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.exists()
            mode = {'error': 'x', 'overwrite': 'w', 'append': 'a'}[args.csv_mode]
            handle = stack.enter_context(path.open(mode, encoding='utf-8-sig', newline=''))
            writer = csv.DictWriter(handle, SWEEP_COLUMNS)
            if args.csv_mode != 'append' or not existing:
                writer.writeheader()
            elif path.stat().st_size:
                with path.open('rb') as check:
                    check.seek(-1, 2)
                    if check.read(1) != b'\n':
                        handle.write('\n')
            handle.flush()

        def log(row):
            nonlocal rows
            rows += 1
            if writer:
                writer.writerow(row)
                handle.flush()

        adc = None
        try:
            initialize_trigger(serial, args, setup_replies)
            if measurements:
                adc_args = argparse.Namespace(**vars(args))
                adc_args.command, adc_args.transport, adc_args.port = 'adc', args.measurement_transport, None
                adc_args.hid_path = None
                adc = stack.enter_context(Meter(adc_args))

            for index, point in enumerate(points, 1):
                point_origin = time.monotonic()
                row = dict(timestamp=datetime.now().astimezone().isoformat(timespec='milliseconds'),
                           controller_version=version, run_started_at=started.isoformat(),
                           source_name=args.source_name, cable_name=args.cable_name, test_note=args.test_note,
                           mode=kind + ('-continuous' if args.continuous_sweep else '-sweep'),
                           sweep_leg=point.leg, sweep_pass=point.sweep_pass, sweep_direction=point.direction,
                           target_voltage_v=point.voltage_mv / 1000, request_current_a=point.current_ma / 1000,
                           pdo_kind=kind, pdo_object_number=args.pdo_index, command=point.command(args.pdo_index),
                           request_status='send_failed')
                row['request_sent_at'] = row['timestamp']
                response = bytearray()
                try:
                    if not args.quiet:
                        print(f'{index:5d}/{len(points)} [{point.leg}] {row["command"]}')
                    serial.write(encode_ascii_command(row['command']))
                    sent += 1
                    last_voltage = point.voltage_mv / 1000
                    row['request_status'] = 'sent_response_read_failed'
                    _read_reply(serial, args.wait, response)
                    row['raw_command_response'] = response.hex(' ')
                    if _rejects(response):
                        row['request_status'] = 'device_rejected'
                        raise ProtocolError(f'Device rejected target {last_voltage:g} V')
                    row['request_status'] = 'sent_unverified'
                    if args.continuous_sweep and args.continuous_settle:
                        time.sleep(args.continuous_settle)
                    if measurements:
                        for sample_index in range(measurements):
                            if args.delay and (not args.continuous_sweep or sample_index):
                                time.sleep(args.delay)
                            for key in ('actual_voltage_v', 'actual_current_a', 'actual_power_w', 'raw_measure_response'):
                                row.pop(key, None)
                            row['measurement_index'] = sample_index + 1
                            raw = adc.get_data(ADC)
                            row['raw_measure_response'] = raw.hex(' ')
                            sample = next((decode_adc(p.payload) for p in logical_packets(raw)
                                           if p.attribute == ADC), None)
                            if sample is None:
                                raise ProtocolError('No ADC payload in sweep measurement')
                            row.update(timestamp=datetime.now().astimezone().isoformat(timespec='milliseconds'),
                                       actual_voltage_v=sample.voltage, actual_current_a=sample.current,
                                       actual_power_w=sample.power, measurement_index=sample_index + 1,
                                       phase_elapsed_s=time.monotonic() - origin)
                            log(row)
                            if not args.quiet:
                                print(f'       ADC: {sample.voltage:.4f} V  {sample.current:.4f} A')
                    else:
                        row['phase_elapsed_s'] = time.monotonic() - origin
                        log(row)
                    remaining = args.apdo_voltage_hold - (time.monotonic() - point_origin)
                    if remaining > 0:
                        time.sleep(remaining)
                    completed += 1
                except BaseException as exc:
                    row.update(raw_command_response=response.hex(' '),
                               error='Ctrl+C' if isinstance(exc, KeyboardInterrupt) else str(exc),
                               phase_elapsed_s=time.monotonic() - origin)
                    log(row)
                    raise
        except KeyboardInterrupt:
            status, error = 'interrupted', 'Ctrl+C'
        except BaseException as exc:
            status, error = 'failed', str(exc)
            raise
        finally:
            # Cleanup runs before files or host connections close, on success,
            # Ctrl+C and failures (including partially written PD requests).
            cleanup = release_trigger(serial, args, adc)
            if cleanup['status'] == 'failed' and status == 'completed':
                status, error = 'failed', 'Trigger cleanup failed: ' + '; '.join(cleanup['errors'])
            if meta_path:
                info = dict(device='KM003C', cli_version=version, status=status, error=error,
                            started_at=started.isoformat(), elapsed_s=time.monotonic() - origin,
                            arguments={k: v for k, v in vars(args).items() if k != 'func'},
                            source_capability_policy='explicit PDO index; EPR AVS range readiness checked, PDO indices/current not auto-validated',
                            current_policy='PD request only; external load not controlled',
                            negotiation_policy='sent_unverified; ASCII delivery/ADC values do not verify PD acceptance',
                            measurement_clock='host timestamps; independent ADC observations, not PD waveform timing',
                            planned_points=len(points), sent_requests=sent, completed_points=completed,
                            rows=rows, last_requested_voltage_V=last_voltage, setup=setup_replies,
                            stop_policy='keep last request' if args.keep_trigger else 'reset then pdm close; external load unchanged',
                            cleanup=cleanup)
                meta_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Sweep {status}: {completed}/{len(points)} points; {sent} requests sent')
    if path:
        print(f'Sweep CSV: {path.resolve()}')
        print(f'Metadata: {meta_path.resolve()}')
    if status == 'failed':
        raise ProtocolError(error)
    return 130 if status == 'interrupted' else 0
