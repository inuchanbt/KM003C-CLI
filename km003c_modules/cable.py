"""SOP' cable discovery, usable by KM003C-CLI and the local PowerChecker bridge.

Field reference: https://github.com/coreboot/chrome-ec/blob/main/include/usb_pd_vdo.h
"""
import argparse
from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
import threading
import time

REQUEST = 'pd data=018F1001A000FF'
LEGACY_REQUEST = 'pd data=014F12018000FF'


def decode_identity(event):
    h, words = event.get('header'), event.get('objects')
    if (event.get('sop') != 1 or not isinstance(h, int) or h & 0x801f != 15
            or not isinstance(words, list) or len(words) != (h >> 12 & 7)
            or not words or not all(type(w) is int and 0 <= w <= 0xffffffff for w in words)
            or event.get('ok') is False or event.get('crc_error') or event.get('eop_error')):
        return None
    vdm = words[0]
    if vdm >> 16 != 0xff00 or not vdm & 0x8000 or vdm & 31 != 1 or vdm >> 8 & 7:
        return None
    response = vdm >> 6 & 3
    if not response:
        return None
    result = dict(sop="SOP'", status=['request', 'ack', 'nak', 'busy'][response],
                  raw_vdos=[f'0x{w:08x}' for w in words], timestamp_us=event.get('timestamp_us'))
    if response != 1:
        return result
    if len(words) < 5:
        return dict(result, status='incomplete')
    kind, cable, revision = words[1] >> 27 & 7, words[4], h >> 6 & 3
    version = cable >> 21 & 7
    result.update(product_type=kind, cable_type={3:'Passive', 4:'Active'}.get(kind, 'Unknown'),
                  vid=words[1] & 65535, pid=words[3] >> 16, bcd_device=words[3] & 65535,
                  xid=words[2], pd_revision=revision, vdo_version=None if revision == 1 else version,
                  hw_version=cable >> 28, fw_version=cable >> 24 & 15,
                  current_a=None, max_vbus_v=None, epr_capable=None, usb_speed=None)
    if kind not in (3, 4):
        return dict(result, status='unsupported')
    known = revision == 1 or revision == 2 and (version == 0 if kind == 3 else version in (0, 3, 4))
    if known:
        result['current_a'] = {1:3, 2:5}.get(cable >> 5 & 3)
        speeds = {0:'USB 2.0', 1:'USB 3.1 Gen1', 2:'USB 3.1 Gen2'} if revision == 1 else {
            0:'USB 2.0', 1:'USB 3.2 Gen1', 2:'USB 3.2 / USB4 Gen2', 3:'USB4 Gen3', 4:'USB4 Gen4'}
        result['usb_speed'] = speeds.get(cable & 7)
        if revision == 2:
            result['max_vbus_v'] = [20, 30, 40, 50][cable >> 9 & 3]
            if kind == 3:
                result['epr_capable'] = bool(cable & 0x20000)
    if kind == 4 and revision == 2 and version in (3, 4) and len(words) < 6:
        result['status'] = 'incomplete'
    return result


def latest_identity(events):
    rows = [r for e in events if (r := decode_identity(e))]
    acks = [r for r in rows if r['status'] == 'ack']
    return (acks or rows or [None])[-1]


def add_cable_options(parser, connection_options, positive_float):
    connection_options(parser, cdc_only=True)
    parser.add_argument('--wait', type=positive_float, default=1.0, help='ASCII response window (s)')
    parser.add_argument('--entry-timeout', type=positive_float, default=15.0)
    parser.add_argument('--observe', type=positive_float, default=3.0, help='USB observation window (s)')
    parser.add_argument('--json-file', help='save identity, PD events and raw USB frames as JSON')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='show commands; no hardware or files')
    parser.set_defaults(func=run_cable, keep_trigger=False)


def run_cable(args):
    if args.dry_run:
        print('\n'.join(['USB: GetData PD_PACKET (0x10)', 'pdm open',
                         'pdm set type=1,em=0,sink=1', 'entry pd', REQUEST,
                         'If no ACK: ' + LEGACY_REQUEST,
                         'Exit cleanup: reset -> pdm close']))
        return 0
    from km003c_modules.protocol import PD_PACKET, ClockUnwrapper, logical_packets, decode_pd
    from km003c_modules.transport import Meter, UsbTransport, SerialTransport, format_ascii_response, serial_ports
    from km003c_modules.sweep import release_trigger
    output = Path(args.json_file).expanduser() if args.json_file else None
    result = dict(events=[], samples=[], sources=["KM003C cable SOP' (USB + CDC)"], errors=[], raw_frames=[])
    stop, errors = threading.Event(), []
    failure = None
    ports = serial_ports(args.vid, args.pid)
    ports = [p for p in ports if (not args.port or p['port'].lower() == args.port.lower())
             and (not args.serial or p['serial'] == args.serial)]
    if len(ports) != 1 or not ports[0]['serial']:
        raise RuntimeError('Select one KM003C CDC port with a serial number using --port / --serial')
    # Match the USB logger to the selected CDC controller, including explicit --port.
    args.serial = ports[0]['serial']
    with ExitStack() as stack:
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(output.open('w' if args.force else 'x', encoding='utf-8'))
        serial = SerialTransport(args)
        stack.callback(serial.close)
        serial.open()
        meter = stack.enter_context(Meter(args, transport=UsbTransport(args)))
        clock = ClockUnwrapper()

        def pump():
            try:
                while not stop.is_set():
                    frame = meter.get_data(PD_PACKET)
                    result['raw_frames'].append(frame.hex(' '))
                    for packet in logical_packets(frame):
                        if packet.attribute == PD_PACKET:
                            _, events = decode_pd(packet.payload, clock)
                            result['events'].extend(asdict(e) for e in events)
                    if len(result['raw_frames']) > 2000:
                        raise RuntimeError('Cable capture exceeds size limit')
                    stop.wait(.04)
            except Exception as exc:
                errors.append(str(exc))

        def exchange(command, seconds=None):
            if errors:
                raise RuntimeError(errors[0])
            if not args.quiet:
                print('TX: ' + command, flush=True)
            serial.write(command.encode('ascii'))
            response = bytearray()
            deadline = time.monotonic() + (args.wait if seconds is None else seconds)
            while time.monotonic() < deadline:
                response.extend(serial.read(min(.05, max(0, deadline-time.monotonic()))))
                if len(response) > 1024*1024:
                    raise RuntimeError('ASCII response exceeds size limit')
                if command == 'entry pd' and re.search(rb'(?:^|[\r\n:>])\s*ready\b', response, re.I):
                    break
            text = format_ascii_response(bytes(response))
            if not args.quiet:
                print('RX: ' + (text or '(no reply)'), flush=True)
            if re.search(r'^\s*(?:error|fail(?:ed|ure)?|false|nak|reject(?:ed)?|invalid|unsupported)\b', text, re.I|re.M):
                raise RuntimeError('Device rejected ' + command)
            return text

        worker = threading.Thread(target=pump, daemon=True)
        worker.start()
        try:
            text = exchange('pdm open')
            if re.search(r'^\s*pdm busy\s*$', text, re.I|re.M):
                exchange('pdm close')
                if re.search(r'^\s*pdm busy\s*$', exchange('pdm open'), re.I|re.M):
                    raise RuntimeError('PDM remains busy')
            exchange('pdm set type=1,em=0,sink=1')
            result['events'].clear()
            if not re.search(r'(?:^|[\r\n:>])\s*ready\b', exchange('entry pd', args.entry_timeout), re.I):
                raise RuntimeError('PD ready was not observed')
            for request in (REQUEST, LEGACY_REQUEST):
                exchange(request)
                deadline = time.monotonic() + args.observe
                while time.monotonic() < deadline:
                    if errors:
                        raise RuntimeError(errors[0])
                    if (latest_identity(result['events']) or {}).get('status') == 'ack':
                        break
                    time.sleep(.04)
                if (latest_identity(result['events']) or {}).get('status') == 'ack':
                    break
        except BaseException as exc:
            failure = exc
            result['errors'].append(str(exc) or type(exc).__name__)
        finally:
            result['cleanup'] = release_trigger(serial, args)
            stop.set()
            worker.join(args.timeout+1)
            result['errors'].extend(errors + result['cleanup']['errors'])
            result['cable'] = latest_identity(result['events'])
            result['capture_status'] = 'interrupted' if isinstance(failure, KeyboardInterrupt) else 'failed' if failure or result['errors'] else 'completed'
            if output:
                json.dump({'schema':'powerchecker-km003c/v1', 'data':result, 'cable':result['cable']}, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
        print(json.dumps(result['cable'] or {'status':'no-response'}, ensure_ascii=False, indent=2))
        if not result['cable']:
            print("No SOP' identity ACK observed. Check cable orientation / VCONN / e-marker / firmware. This does not prove cable ratings.")
        if failure:
            raise failure
        if result['errors']:
            raise RuntimeError('; '.join(result['errors']))
    return 0
