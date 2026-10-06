#!/usr/bin/env python3
"""POWER-Z KM003C CLI with CY4500-style capture/status CSV and options."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime
import csv
import json
import math
from pathlib import Path
import statistics
import struct
import sys
import time

from km003c_modules.protocol import (
    VID, PID, ADC, PD_PACKET, PD_COLUMNS, SCOPE_COLUMNS, LIVE_COLUMNS,
    ClockUnwrapper, ProtocolError, decode_adc, decode_pd, decode_cdc_adc,
    logical_packets, measurement_dict,
)
from km003c_modules.transport import Meter, CdcStream, ascii_command, enumerate_devices
from km003c_modules.utility_export import UtilityExport, packet_for_event, packet_for_vbus_event
from km003c_modules.vbus_events import VbusEventDetector

VERSION = '0.3.0'


def positive_float(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be a finite number greater than zero')
    return value


def nonnegative_float(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError('must be a finite non-negative number')
    return value


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return value


def auto_int(text):
    return int(text, 0)


def nonnegative_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError('must be non-negative')
    return value


def pd_raw(text):
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be hexadecimal SOP + header + data bytes') from exc
    if len(raw) < 3 or raw[0] not in (0, 1, 2):
        raise argparse.ArgumentTypeError('must start with SOP 00/01/02 and a two-byte PD header')
    header = int.from_bytes(raw[1:3], 'little')
    if len(raw) != 3 + ((header >> 12) & 7) * 4:
        raise argparse.ArgumentTypeError('PD header object count does not match the supplied bytes (omit CRC)')
    return raw.hex().upper()


def connection_options(parser, *, cdc_only=False):
    if not cdc_only:
        parser.add_argument('--transport', choices=['auto', 'hid', 'usb', 'cdc'], default='auto',
                            help='auto: USB for capture, HID for ADC, CDC with --port')
    else:
        parser.set_defaults(transport='cdc')
    parser.add_argument('--port', '-p', '--com', help='virtual serial port, e.g. COM3')
    parser.add_argument('--serial', help='select the USB device serial number')
    parser.add_argument('--vid', type=auto_int, default=VID)
    parser.add_argument('--pid', type=auto_int, default=PID)
    parser.add_argument('--timeout', type=positive_float, default=2.0, help='I/O timeout in seconds')
    parser.add_argument('--baud', type=positive_int, default=115200)
    if not cdc_only:
        parser.add_argument('--hid-path', help='select a path printed by usb-info')


def duration_options(parser, seconds):
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument('--seconds', type=positive_float, default=seconds)
    duration.add_argument('--until-ctrl-c', action='store_true')


def ascii_options(parser):
    connection_options(parser, cdc_only=True)
    parser.add_argument('--wait', type=positive_float, default=1.0, help='response read window in seconds')
    parser.add_argument('--dry-run', action='store_true', help='print the command without opening hardware')
    parser.add_argument('--response-file', help='save the exact response bytes')


def gui_output_options(parser):
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--scope', dest='scope', action='store_true', help='include measurements (default)')
    scope.add_argument('--no-scope', dest='scope', action='store_false', help='omit scope CSV and GUI waveform')
    parser.set_defaults(scope=True)
    gui = parser.add_mutually_exclusive_group()
    gui.add_argument('--ccgx3', dest='ccgx3', action='store_true', default=None,
                     help='enable EZ-PD 4.2 session export (default)')
    gui.add_argument('--no-ccgx3', dest='ccgx3', action='store_false', help='disable session export')
    parser.add_argument('--formats', nargs='+', choices=['all', 'original', 'csv', 'ccgx3'],
                        help='TI-style output selection; default: all; --[no-]ccgx3 overrides this selection')
    parser.add_argument('--force', action='store_true', help='overwrite existing outputs (default: refuse collisions)')
    parser.add_argument('--gui-csv', action='store_true', help='compatibility alias; Utility CSV is the default')
    parser.add_argument('--infer-vbus-events', action='store_true',
                        help='infer software VBUS_UP/DN from measurements (default: off); '
                             '4.0V/0.8V thresholds, no initial event or inference across gaps >100ms; '
                             'requires scope; saves GUI rows and .vbus_events.jsonl')


def build_arg_parser():
    parser = argparse.ArgumentParser(description='POWER-Z KM003C measurement, PD capture and fast-charge control')
    parser.add_argument('--version', action='version', version=f'%(prog)s {VERSION}')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('usb-info', help='enumerate KM003C USB/HID/virtual COM interfaces')
    connection_options(p)
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=run_usb_info)

    p = sub.add_parser('live-status', aliases=['volt-amp'], help='read VBUS/IBUS/CC1/CC2')
    connection_options(p)
    p.add_argument('--count', type=positive_int, default=None)
    p.add_argument('--interval', type=nonnegative_float, default=0.1)
    p.add_argument('--median', type=positive_int, default=5)
    p.add_argument('--csv')
    p.add_argument('--instant', action='store_true', help='use instantaneous ADC values instead of device averages')
    p.set_defaults(func=run_live)

    p = sub.add_parser('adc', help='read detailed KM003C ADC, including D+/D-/VDD and raw temperature')
    connection_options(p)
    p.add_argument('--count', type=positive_int, default=1)
    p.add_argument('--interval', type=nonnegative_float, default=0.1)
    p.add_argument('--csv')
    p.add_argument('--jsonl', help='save complete decoded ADC samples')
    p.add_argument('--instant', action='store_true')
    p.set_defaults(func=run_adc)

    p = sub.add_parser('scope', help='record VBUS/IBUS/CC1/CC2 to CY4500-compatible scope CSV')
    connection_options(p)
    duration_options(p, 5.0)
    p.add_argument('--csv', default='km003c_scope.csv')
    p.add_argument('--raw', help='save length-prefixed native transfers')
    p.add_argument('--quiet', action='store_true')
    p.add_argument('--interval', type=nonnegative_float, default=0.1)
    p.add_argument('--instant', action='store_true')
    p.add_argument('--stream', action='store_true', help='use the new CDC ADC streaming command')
    p.add_argument('--rate', type=int, choices=[4, 10, 50, 1000], help='new CDC stream samples/second')
    p.set_defaults(func=run_scope)

    p = sub.add_parser('capture', help='record PD events, native frames and optional scope CSV')
    connection_options(p)
    duration_options(p, 8.0)
    p.add_argument('--out-prefix', default='km003c_capture')
    gui_output_options(p)
    p.add_argument('--scope-raw', action='store_true')
    p.add_argument('--quiet', action='store_true')
    p.add_argument('--allow-framing-errors', action='store_true')
    goodcrc = p.add_mutually_exclusive_group()
    goodcrc.add_argument('--hide-goodcrc', dest='hide_goodcrc', action='store_true',
                         help='hide decoded GOODCRC console lines (default); all saved data is preserved; '
                              'KM003C does not expose CRC validity')
    goodcrc.add_argument('--show-goodcrc', dest='hide_goodcrc', action='store_false',
                         help='show GOODCRC console lines')
    p.set_defaults(hide_goodcrc=True)
    p.add_argument('--interval', type=nonnegative_float, default=0.04, help='PD polling interval in seconds')
    p.set_defaults(func=run_capture)

    p = sub.add_parser('export-gui', aliases=['decode', 'convert'], help='convert saved native records to Utility CSV/ccgx3 offline')
    p.add_argument('--records', '--input', required=True, help='KM003C .records.bin (length-prefixed native frames)')
    p.add_argument('--out-prefix', required=True)
    gui_output_options(p)
    p.add_argument('--allow-framing-errors', action='store_true')
    p.set_defaults(func=run_export)

    p = sub.add_parser('pdm', help='open/close/configure the fast-charge trigger module')
    p.add_argument('action', choices=['open', 'close', 'set'])
    p.add_argument('--type', type=int, choices=[0, 1, 2, 3], help='0:auto, 1:PD3.0, 2:PD3.1, 3:private PPS')
    p.add_argument('--em', type=int, choices=[0, 1, 2], help='0:off, 1:20V5A, 2:EPR50V5A')
    p.add_argument('--sink', type=int, choices=[0, 1])
    ascii_options(p)
    p.set_defaults(func=run_ascii)

    p = sub.add_parser('entry', help='enter or discover a charging protocol')
    p.add_argument('protocol', choices=['pd', 'ufcs', 'qc', 'fcp', 'scp', 'afc', 'vfcp',
                                       'sfcp', 'bc', 'apple', 'list', 'list+'])
    ascii_options(p)
    p.set_defaults(func=run_ascii)

    p = sub.add_parser('reset', help='reset the fast-charge trigger to its initial state')
    ascii_options(p)
    p.set_defaults(func=run_ascii)

    for name in ('pd', 'ufcs'):
        p = sub.add_parser(name, help=f'{name.upper()} capabilities, requests and control messages')
        action = p.add_mutually_exclusive_group(required=True)
        action.add_argument('--pdo', action='store_true')
        action.add_argument('--req', type=positive_int, help='ObjectPosition / request index')
        action.add_argument('--cmd', type=int, choices=range(1, 32), metavar='1..31')
        if name == 'pd':
            action.add_argument('--data', type=pd_raw, help='hex SOP + PD header + objects; no CRC')
            action.add_argument('--drp', action='store_true')
        p.add_argument('--volt', type=positive_int, help='vendor volt parameter (mV)')
        p.add_argument('--cur', type=nonnegative_int, help='vendor cur parameter (mA)')
        ascii_options(p)
        p.set_defaults(func=run_ascii)

    for name, voltages in [('qc', [5, 9, 12, 20]), ('fcp', [5, 9, 12]),
                           ('afc', [5, 9, 12]), ('sfcp', [5, 9, 12])]:
        p = sub.add_parser(name, help=f'{name.upper()} fixed-voltage trigger')
        p.add_argument('--voltage', required=True, type=int, choices=voltages, help='voltage in V')
        ascii_options(p)
        p.set_defaults(func=run_ascii)

    p = sub.add_parser('qc3', help='QC3.0 voltage setting / increase / decrease')
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument('--volt', type=positive_int, help='voltage in mV, 3600..20000')
    action.add_argument('--inc', type=positive_int)
    action.add_argument('--dec', type=positive_int)
    ascii_options(p)
    p.set_defaults(func=run_ascii)

    for name in ('scp', 'vfcp'):
        p = sub.add_parser(name, help=f'{name.upper()} voltage/current trigger')
        p.add_argument('--volt', required=True, type=positive_int, help='voltage in mV')
        p.add_argument('--cur', required=True, type=positive_int, help='current in mA')
        ascii_options(p)
        p.set_defaults(func=run_ascii)
    return parser


def build_ascii_command(args):
    name = args.command
    if name == 'pdm':
        options = [(key, getattr(args, key)) for key in ('type', 'em', 'sink') if getattr(args, key) is not None]
        if args.action == 'set':
            if not options:
                raise ValueError('pdm set requires --type, --em or --sink')
            return 'pdm set ' + ','.join(f'{key}={value}' for key, value in options)
        if options:
            raise ValueError('pdm --type/--em/--sink are only used with set')
        return 'pdm ' + args.action
    if name == 'entry':
        return 'entry ' + args.protocol
    if name == 'reset':
        return 'reset'
    if name in ('pd', 'ufcs'):
        if args.req is None and (args.volt is not None or args.cur is not None):
            raise ValueError('--volt and --cur require --req')
        if name == 'ufcs' and args.req is not None and (args.volt is None or args.cur is None):
            raise ValueError('ufcs --req requires --volt and --cur')
        if args.req is not None:
            if args.req > (15 if name == 'pd' else 255):
                raise ValueError('request index is outside the protocol field range')
            fields = [f'req={args.req}']
            fields.extend(f'{key}={getattr(args, key)}' for key in ('volt', 'cur') if getattr(args, key) is not None)
            return name + ' ' + ','.join(fields)
        if args.pdo:
            return name + ' pdo'
        if args.cmd is not None:
            return f'{name} cmd={args.cmd}'
        if getattr(args, 'data', None):
            return 'pd data=' + args.data
        return 'pd drp'
    if name in ('qc', 'fcp', 'afc', 'sfcp'):
        return f'{name} {args.voltage}V'
    if name == 'qc3':
        if args.volt is not None and (not 3600 <= args.volt <= 20000 or args.volt % 200):
            raise ValueError('QC3 voltage must be 3600..20000 mV in 200 mV steps')
        key = next(key for key in ('volt', 'inc', 'dec') if getattr(args, key) is not None)
        return f'qc3 {key}={getattr(args, key)}'
    if name == 'vfcp' and not 7000 <= args.volt <= 20000:
        raise ValueError('VFCP voltage must be 7000..20000 mV')
    return f'{name} volt={args.volt},cur={args.cur}'


def open_output(stack, path, *, binary=False, bom=True):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        return stack.enter_context(path.open('wb'))
    return stack.enter_context(path.open('w', encoding='utf-8-sig' if bom else 'utf-8', newline=''))


def json_line(handle, data):
    handle.write(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    handle.flush()


def write_native(handle, raw):
    handle.write(struct.pack('<I', len(raw)))
    handle.write(raw)
    handle.flush()


def read_native(path):
    with Path(path).open('rb') as handle:
        while prefix := handle.read(4):
            if len(prefix) != 4:
                raise ProtocolError('Truncated native record length')
            size = struct.unpack('<I', prefix)[0]
            if not 4 <= size <= 65536:
                raise ProtocolError('Invalid native record length; this is not a KM003C records file')
            raw = handle.read(size)
            if len(raw) != size:
                raise ProtocolError('Truncated native record')
            yield raw


def metadata(args, **fields):
    return {'device': 'KM003C', 'cli_version': VERSION,
            'created_at': datetime.now().astimezone().isoformat(timespec='milliseconds'),
            'arguments': {k: v for k, v in vars(args).items() if k != 'func'},
            'raw_format': 'u32le-length + native KM003C bytes, repeated',
            'current_direction': 'vendor signed current; polarity is not inverted',
            **fields}


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def wait_interval(start, interval):
    remaining = start + interval - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def first_adc(raw, args):
    for packet in logical_packets(raw):
        if packet.attribute == ADC:
            return decode_adc(packet.payload, average=not args.instant)
    raise ProtocolError('No ADC payload in the response')


def run_usb_info(args):
    info = enumerate_devices(args)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def run_ascii(args):
    command = build_ascii_command(args)
    print(command)
    if args.dry_run:
        return 0
    raw = ascii_command(args, command)
    if args.response_file:
        with ExitStack() as stack:
            open_output(stack, args.response_file, binary=True).write(raw)
    if raw:
        print(raw.decode('utf-8', errors='replace'), end='' if raw.endswith(b'\n') else '\n')
    else:
        print('(no reply; command delivery does not confirm negotiation success)')
    return 0


def print_sample(index, sample, median=None):
    current = sample.current if median is None else median
    print(f'{index:6d}  VBUS={sample.voltage:9.6f} V  IBUS={current:9.6f} A  '
          f'CC1={sample.cc1_mV / 1000:.4f} V  CC2={sample.cc2_mV / 1000:.4f} V  '
          f'P={sample.power:.6f} W')


def run_live(args):
    history = deque(maxlen=args.median)
    with ExitStack() as stack:
        meter = stack.enter_context(Meter(args))
        handle = open_output(stack, args.csv) if args.csv else None
        writer = csv.writer(handle) if handle else None
        if writer:
            writer.writerow(LIVE_COLUMNS)
        index = 0
        try:
            while args.count is None or index < args.count:
                started = time.monotonic()
                raw = meter.get_data(ADC)
                sample = first_adc(raw, args)
                index += 1
                history.append(sample.current)
                median = statistics.median(history)
                print_sample(index, sample, median)
                if writer:
                    writer.writerow([index, f'{time.monotonic():.9f}', raw.hex(' '),
                        sample.vbus_uV, sample.ibus_uA, sample.cc1_raw, sample.cc2_raw,
                        sample.vbus_uV / 1000, sample.voltage, sample.ibus_uA / 1000,
                        sample.current, median, sample.cc1_mV, sample.cc1_mV / 1000,
                        sample.cc2_mV, sample.cc2_mV / 1000, sample.power,
                        sample.voltage * median])
                    handle.flush()
                if args.count is None or index < args.count:
                    wait_interval(started, args.interval)
        except KeyboardInterrupt:
            pass
    return 0


ADC_COLUMNS = ['sample', 'monotonic_s', 'vbus_V', 'ibus_A', 'power_W',
               'cc1_V', 'cc2_V', 'dp_V', 'dm_V', 'vdd_V', 'temp_raw', 'raw_hex']


def run_adc(args):
    with ExitStack() as stack:
        meter = stack.enter_context(Meter(args))
        handle = open_output(stack, args.csv) if args.csv else None
        writer = csv.writer(handle) if handle else None
        jsonl = open_output(stack, args.jsonl, bom=False) if args.jsonl else None
        if writer:
            writer.writerow(ADC_COLUMNS)
        try:
            for index in range(1, args.count + 1):
                started = time.monotonic()
                raw = meter.get_data(ADC)
                sample = first_adc(raw, args)
                record = {'sample': index, 'monotonic_s': time.monotonic(),
                          **measurement_dict(sample), 'native_frame_hex': raw.hex(' ')}
                print(json.dumps(record, ensure_ascii=False))
                if jsonl:
                    json_line(jsonl, record)
                if writer:
                    writer.writerow([index, record['monotonic_s'], sample.voltage, sample.current,
                        sample.power, sample.cc1_mV / 1000, sample.cc2_mV / 1000,
                        sample.dp_raw / 10000, sample.dm_raw / 10000, sample.vdd_raw / 10000,
                        sample.temp_raw, raw.hex(' ')])
                    handle.flush()
                if index < args.count:
                    wait_interval(started, args.interval)
        except KeyboardInterrupt:
            pass
    return 0


def run_scope(args):
    clock = ClockUnwrapper()
    samples = 0
    origin = time.monotonic()
    status = 'completed'
    error = None
    with ExitStack() as stack:
        reader = stack.enter_context(CdcStream(args) if args.stream else Meter(args))
        raw_file = open_output(stack, args.raw, binary=True) if args.raw else None
        reader.on_transfer = (lambda raw: write_native(raw_file, raw)) if raw_file else None
        handle = open_output(stack, args.csv)
        writer = csv.writer(handle)
        writer.writerow(SCOPE_COLUMNS)
        origin = time.monotonic()
        deadline = math.inf if args.until_ctrl_c else origin + args.seconds
        transfer = 0
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                transfer += 1
                if args.stream:
                    try:
                        raw = reader.read_frame(min(args.timeout, max(0.001, deadline - started)))
                    except TimeoutError:
                        if time.monotonic() >= deadline:
                            break
                        raise
                    decoded = decode_cdc_adc(raw, clock)
                else:
                    raw = reader.get_data(ADC)
                    decoded = [first_adc(raw, args)]
                received_us = round((time.monotonic() - origin) * 1_000_000)
                for index, sample in enumerate(decoded):
                    sample.timestamp_us = received_us
                    samples += 1
                    writer.writerow(sample.scope_row(transfer, transfer, index))
                    if not args.quiet and (samples == 1 or time.monotonic() - started >= 1 or not args.stream):
                        print_sample(samples, sample)
                handle.flush()
                if not args.stream:
                    wait_interval(started, args.interval)
        except KeyboardInterrupt:
            status = 'interrupted'
        except BaseException as exc:
            status = 'failed'
            error = str(exc)
            raise
        finally:
            save_json(str(args.csv) + '.metadata.json', metadata(args, status=status, error=error,
                samples=samples, elapsed_s=time.monotonic() - origin,
                clock_source='host_monotonic', timestamp_unit='microseconds since this scope run',
                cdc_time_unit='unknown; Timestamp Raw is preserved' if args.stream else None,
                cdc_crc_verified=None if args.stream else 'not applicable',
                raw_units={'VBUS': 'uV', 'IBUS': 'uA', 'CC': 'mV (CDC stream) or 0.1mV (ADC)'},
                batch_timestamps='All samples in a CDC batch share their host receipt time' if args.stream else None))
    print(f'Scope CSV: {Path(args.csv).resolve()} ({samples} samples)')
    return 0


def output_formats(args):
    formats = set(args.formats or ['all'])
    if 'all' in formats:
        formats = {'original', 'csv', 'ccgx3'}
    if args.ccgx3 is not None:
        if args.ccgx3:
            formats.add('ccgx3')
        else:
            formats.discard('ccgx3')
    if not formats:
        raise ValueError('Select at least one output format')
    return formats


def capture_output_paths(args, *, live):
    formats = output_formats(args)
    suffixes = ['.metadata.json', '.summary.txt']
    if args.infer_vbus_events:
        suffixes.append('.vbus_events.jsonl')
    if 'csv' in formats:
        suffixes.append('.csv')
    if 'ccgx3' in formats:
        suffixes.append('.ccgx3')
    if 'original' in formats:
        suffixes.append('.records.jsonl')
        if args.scope:
            suffixes.append('.scope.csv')
        if live:
            suffixes.extend(['.records.bin', '.records.hex.txt', '.xfers.bin'])
            if args.scope and args.scope_raw:
                suffixes.append('.scope.xfers.bin')
    prefix = Path(args.out_prefix).expanduser()
    return [prefix.with_suffix(suffix) for suffix in suffixes]


def check_capture_outputs(args, *, live, source=None):
    for path in capture_output_paths(args, live=live):
        if source is not None and path.resolve() == source:
            raise ValueError('Output would overwrite the input records')
        if path.exists() and (not args.force or path.is_dir()):
            raise FileExistsError(f'{path} exists; choose another prefix or use --force')


def print_capture_outputs(export):
    inferred = f', {export.gui_inferred_events} inferred VBUS events' if export.detector else ''
    if 'csv' in export.formats:
        print(f'PD CSV: {export.prefix.with_suffix(".csv").resolve()} ({export.gui_events} PD messages{inferred})')
    if 'ccgx3' in export.formats:
        print(f'CCGX3: {export.prefix.with_suffix(".ccgx3").resolve()} '
              f'({export.gui_events} PD messages{inferred}, {export.utility.scope_count} waveform samples)')
    if export.scope_file:
        print(f'Scope CSV: {export.prefix.with_suffix(".scope.csv").resolve()} ({export.samples} samples)')
    if export.vbus_file:
        print(f'Inferred VBUS events: {export.prefix.with_suffix(".vbus_events.jsonl").resolve()} '
              f'({sum(export.detector.counts.values())} estimates)')


class CaptureExport:
    def __init__(self, stack, args, *, live):
        self.args = args
        self.prefix = Path(args.out_prefix).expanduser()
        self.formats = output_formats(args)
        check_capture_outputs(args, live=live)
        original = 'original' in self.formats
        self.csv_file = open_output(stack, self.prefix.with_suffix('.csv'), bom=False) if 'csv' in self.formats else None
        self.pd_writer = csv.writer(self.csv_file, lineterminator='\n') if self.csv_file else None
        if self.pd_writer:
            self.pd_writer.writerow(PD_COLUMNS)
        self.jsonl = open_output(stack, self.prefix.with_suffix('.records.jsonl'), bom=False) if original else None
        self.detector = VbusEventDetector() if args.infer_vbus_events else None
        self.vbus_file = (open_output(stack, self.prefix.with_suffix('.vbus_events.jsonl'), bom=False)
                          if self.detector else None)
        self.scope_file = open_output(stack, self.prefix.with_suffix('.scope.csv')) if args.scope and original else None
        self.scope_writer = csv.writer(self.scope_file) if self.scope_file else None
        if self.scope_writer:
            self.scope_writer.writerow(SCOPE_COLUMNS)
        self.records = open_output(stack, self.prefix.with_suffix('.records.bin'), binary=True) if live and original else None
        self.hex_file = open_output(stack, self.prefix.with_suffix('.records.hex.txt'), bom=False) if live and original else None
        self.transfers = open_output(stack, self.prefix.with_suffix('.xfers.bin'), binary=True) if live and original else None
        self.scope_raw = (open_output(stack, self.prefix.with_suffix('.scope.xfers.bin'), binary=True)
                          if live and original and args.scope and args.scope_raw else None)
        self.prefix.parent.mkdir(parents=True, exist_ok=True)
        self.utility = UtilityExport(ccgx3_path=self.prefix.with_suffix('.ccgx3') if 'ccgx3' in self.formats else None)
        stack.callback(self.utility.close)
        self.clock = ClockUnwrapper()
        self.frames = self.events = self.samples = self.errors = self.unknown_events = 0
        self.gui_events = self.gui_rows = self.gui_inferred_events = 0
        self.gui_omitted = Counter()
        self.messages = Counter()

    def write_gui_packet(self, adapted):
        if not self.formats & {'csv', 'ccgx3'}:
            return False
        raw_packet, row = adapted
        self.gui_rows += 1
        if self.pd_writer:
            self.pd_writer.writerow(row)
        self.utility.write_packet(raw_packet, row)
        return True

    def infer_vbus(self, sample):
        if self.detector is None:
            return
        event = self.detector.observe(sample)
        if event is None:
            return
        exported = self.write_gui_packet(packet_for_vbus_event(event, self.gui_rows + 1))
        if exported:
            self.gui_inferred_events += 1
        json_line(self.vbus_file, {**event, 'frame_index': self.frames,
                                  'gui_exported': exported,
                                  'gui_row_index': self.gui_rows if exported else None})
        if not getattr(self.args, 'quiet', True):
            print(f'[VBUS inferred] {event["timestamp_us"]:12d} us  '
                  f'{event["event"]} {event["vbus_mV"]} mV')

    def transfer(self, raw):
        if self.transfers:
            write_native(self.transfers, raw)
        if self.scope_raw:
            write_native(self.scope_raw, raw)

    def frame(self, raw):
        self.frames += 1
        if self.records:
            write_native(self.records, raw)
            self.hex_file.write(f'{self.frames - 1:04d}  {raw.hex(" ")}\n')
            self.hex_file.flush()
        record = {'index': self.frames, 'raw': raw.hex(' '),
                  'host_monotonic_s': time.monotonic(), 'events': [], 'measurements': []}
        try:
            packets = logical_packets(raw)
            record['attributes'] = [p.attribute for p in packets]
            # Decode the complete response before emitting any derived rows.
            decoded = []
            for packet_index, packet in enumerate(packets):
                if packet.attribute == PD_PACKET:
                    sample, events = decode_pd(packet.payload, self.clock)
                    decoded.append((packet_index, sample, events))
                elif packet.attribute == ADC:
                    sample = decode_adc(packet.payload)
                    decoded.append((packet_index, sample, []))
                else:
                    record.setdefault('unknown_payloads', []).append({
                        'attribute': packet.attribute, 'payload': packet.payload.hex(' ')})
            for packet_index, sample, events in decoded:
                record['measurements'].append(measurement_dict(sample))
                if self.args.scope and sample.timestamp_us is not None:
                    self.samples += 1
                    if self.scope_writer:
                        self.scope_writer.writerow(sample.scope_row(self.frames, packet_index, 0))
                    self.utility.write_scope(sample)
                    self.infer_vbus(sample)
                for event in events:
                    self.events += 1
                    if event.kind == 'unknown':
                        self.unknown_events += 1
                    self.messages[event.message] += 1
                    adapted = packet_for_event(event, self.gui_rows + 1)
                    exported = adapted is not None and self.write_gui_packet(adapted)
                    if exported:
                        self.gui_events += 1
                    elif adapted is None:
                        self.gui_omitted[event.message] += 1
                    record['events'].append({**asdict(event), 'gui_exported': exported})
                    if not getattr(self.args, 'quiet', True) and not (
                            getattr(self.args, 'hide_goodcrc', True) and event.message == 'GOODCRC'):
                        print(f'{self.events:6d} {event.timestamp_us:12d} us  {event.message}')
        except ProtocolError as exc:
            self.errors += 1
            record['decode_error'] = str(exc)
            if not self.args.allow_framing_errors:
                if self.jsonl:
                    json_line(self.jsonl, record)
                raise
        if self.jsonl:
            json_line(self.jsonl, record)
        if self.csv_file:
            self.csv_file.flush()
        if self.scope_file:
            self.scope_file.flush()

    def finalize(self, status, error=None):
        archive_error = None
        try:
            self.utility.close()
        except (OSError, ValueError) as exc:
            archive_error = exc
            status, error = 'failed', str(exc)
        info = metadata(self.args, status=status, error=error, frames=self.frames,
                        events=self.events, scope_samples=self.samples,
                        framing_errors=self.errors, message_counts=dict(self.messages),
                        unknown_pd_events=self.unknown_events,
                        output_formats=sorted(self.formats), gui_pd_messages=self.gui_events,
                        gui_rows=self.gui_rows, gui_inferred_vbus_events=self.gui_inferred_events,
                        vbus_event_inference=self.detector.summary() if self.detector else {'enabled': False},
                        goodcrc_console_policy='hide decoded GOODCRC; CRC validity not exposed' if
                        getattr(self.args, 'hide_goodcrc', True) else 'show GOODCRC',
                        gui_omitted_events=dict(self.gui_omitted),
                        ccgx3_waveform_samples=self.utility.scope_count,
                        ccgx3_graph_clipped=self.utility.graph_clipped,
                        ccgx3_packet_data='synthetic GUI adapter; not native CY4500 records; no OK/CRC/EOP bits asserted; inferred VBUS uses VOLT_PKT',
                        ccgx3_graph_scaling='GraphData physical mV/mA rounded to integers; signed IBUS; EPR GUI unsigned VBUS',
                        gui_omitted_policy='status/unknown events have no PD header; retained in original-format JSONL/native records',
                        unknown_pd_policy='preserve the remaining logical payload; resume at the next logical packet/response',
                        unknown_pd_timestamp='PD status preamble observation time; unknown event timestamp is not decoded',
                        clock_source='device_ms', timestamp_unit='us converted from device ms',
                        timestamp_resolution_us=1000, crc_eop_status='not exposed',
                        duration_delta='wire duration and inter-packet gap are not exposed; cells are empty',
                        start_end_time='same observed event timestamp; point event, not measured wire start/end',
                        pd_csv_vbus_unit='integer mV under CY4500 Utility heading Vbus(V)',
                        scope_raw_content='same native PD transfers; measurement preamble is included')
        save_json(self.prefix.with_suffix('.metadata.json'), info)
        summary = [f'KM003C CLI {VERSION}', f'Status: {status}', f'Native frames: {self.frames}',
                   f'PD/status events: {self.events}', f'Scope samples: {self.samples}',
                   f'Framing errors: {self.errors}', 'Clock: device milliseconds converted to microseconds',
                   f'Unknown PD event payloads: {self.unknown_events}',
                   f'GUI PD messages: {self.gui_events}', f'GUI omitted events: {dict(self.gui_omitted)}',
                   f'GUI inferred VBUS events: {self.gui_inferred_events}',
                   f'VBUS inference: {self.detector.summary() if self.detector else {"enabled": False}}',
                   f'CCGX3 waveform samples: {self.utility.scope_count}',
                   'CRC/EOP, wire duration and delta: not exposed',
                   'Start Time == End Time: one observed event timestamp, not physical wire start/end',
                   'Native binary: u32le length followed by native KM003C response, repeated',
                   '', 'Message counts:']
        summary.extend(f'  {key}: {value}' for key, value in self.messages.items())
        if error:
            summary.append(f'Error: {error}')
        self.prefix.with_suffix('.summary.txt').write_text('\n'.join(summary) + '\n', encoding='utf-8')
        if archive_error is not None:
            raise archive_error


def run_capture(args):
    status, error = 'completed', None
    check_capture_outputs(args, live=True)
    with ExitStack() as stack:
        # Own the connection before replacing output files.
        meter = stack.enter_context(Meter(args))
        export = CaptureExport(stack, args, live=True)
        meter.on_transfer = export.transfer
        origin = time.monotonic()
        deadline = math.inf if args.until_ctrl_c else origin + args.seconds
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                export.frame(meter.get_data(PD_PACKET))
                wait_interval(started, args.interval)
        except KeyboardInterrupt:
            status = 'interrupted'
        except BaseException as exc:
            status, error = 'failed', str(exc)
            raise
        finally:
            export.finalize(status, error)
    print_capture_outputs(export)
    return 0


def run_export(args):
    source = Path(args.records).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    check_capture_outputs(args, live=False, source=source)
    status, error = 'completed', None
    with ExitStack() as stack:
        export = CaptureExport(stack, args, live=False)
        try:
            for raw in read_native(source):
                export.frame(raw)
        except BaseException as exc:
            status, error = 'failed', str(exc)
            raise
        finally:
            export.finalize(status, error)
    print_capture_outputs(export)
    return 0


def validate_args(args, parser):
    if hasattr(args, 'vid') and not (0 <= args.vid <= 65535 and 0 <= args.pid <= 65535):
        parser.error('VID/PID must fit 16 bits')
    if hasattr(args, 'transport') and args.transport in ('hid', 'usb') and args.port:
        parser.error('--port is used with --transport cdc or auto')
    if args.command == 'scope':
        if args.rate is not None and not args.stream:
            parser.error('--rate requires --stream')
        if args.stream and args.transport in ('hid', 'usb'):
            parser.error('--stream uses the CDC port')
    if args.command == 'capture' and args.scope_raw and not args.scope:
        parser.error('--scope-raw requires --scope')
    if getattr(args, 'infer_vbus_events', False) and not args.scope:
        parser.error('--infer-vbus-events requires --scope')
    if args.func is run_ascii:
        try:
            build_ascii_command(args)
        except ValueError as exc:
            parser.error(str(exc))
    # Avoid collisions between independently written user-selected outputs.
    paths = [getattr(args, key, None) for key in ('csv', 'raw', 'jsonl')]
    if args.command == 'scope':
        paths.append(str(args.csv) + '.metadata.json')
    resolved = [Path(p).expanduser().resolve() for p in paths if p]
    if len(resolved) != len(set(resolved)):
        parser.error('CSV, raw and JSONL output paths must be different')


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
