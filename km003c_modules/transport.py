"""Lazy USB/HID/CDC backends; importing this module never opens hardware."""
from __future__ import annotations

import importlib
import sys
import time

from .protocol import VID, PID, GET_DATA, PUT_DATA, ProtocolError, control_packet, frame_length


def dependency(name: str, package: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise RuntimeError(f'{package} is required for this connection. Install with: '
                           f'"{sys.executable}" -m pip install {package}') from exc


class HidTransport:
    def __init__(self, args):
        self.args = args
        self.handle = None

    def open(self):
        hid = dependency('hid', 'hidapi')
        devices = hid.enumerate(self.args.vid, self.args.pid)
        if self.args.serial:
            devices = [d for d in devices if d.get('serial_number') == self.args.serial]
        if self.args.hid_path:
            devices = [d for d in devices if d['path'].decode('utf-8', 'surrogateescape') == self.args.hid_path]
        if len(devices) != 1:
            raise RuntimeError(f'Found {len(devices)} matching HID interfaces. '
                               'Use usb-info and --serial or --hid-path to select one.')
        self.handle = hid.device()
        try:
            self.handle.open_path(devices[0]['path'])
        except BaseException:
            self.handle.close()
            self.handle = None
            raise
        return self

    def write(self, data: bytes):
        if len(data) > 64:
            raise ValueError('HID payload exceeds 64 bytes')
        # HIDAPI requires the first byte to be the Report ID, even when it is zero.
        report = bytes([0]) + data.ljust(64, b'\0')
        count = self.handle.write(report)
        if count != len(report):
            raise OSError(f'Short HID write: {count}/{len(report)}')

    def read(self, timeout: float) -> bytes:
        data = bytes(self.handle.read(64, max(1, round(timeout * 1000))))
        return data

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class UsbTransport:
    def __init__(self, args):
        self.args = args
        self.context = self.handle = self.claim = None

    def open(self):
        usb1 = dependency('usb1', 'libusb1')
        self.usb1 = usb1
        self.context = usb1.USBContext()
        matches = []
        try:
            for device in self.context.getDeviceList(skip_on_error=True):
                if device.getVendorID() != self.args.vid or device.getProductID() != self.args.pid:
                    continue
                handle = device.open()
                try:
                    serial = handle.getSerialNumber() if self.args.serial else None
                except BaseException:
                    handle.close()
                    raise
                if self.args.serial and serial != self.args.serial:
                    handle.close()
                else:
                    matches.append(handle)
            if len(matches) != 1:
                for handle in matches:
                    handle.close()
                count = len(matches)
                matches.clear()
                raise RuntimeError(f'Found {count} matching USB devices; select with --serial.')
            self.handle = matches[0]
            matches.clear()
            if hasattr(self.handle, 'setAutoDetachKernelDriver'):
                try:
                    self.handle.setAutoDetachKernelDriver(True)
                except usb1.USBErrorNotSupported:
                    pass
            self.claim = self.handle.claimInterface(0)
            self.claim.__enter__()
        except usb1.USBError as exc:
            for handle in matches:
                handle.close()
            self.close()
            raise OSError(f'Cannot open/claim KM003C USB interface 0: {exc}') from exc
        except BaseException:
            for handle in matches:
                handle.close()
            self.close()
            raise
        return self

    def write(self, data: bytes):
        try:
            count = self.handle.bulkWrite(0x01, data, timeout=round(self.args.timeout * 1000))
        except self.usb1.USBError as exc:
            raise OSError(f'KM003C USB write failed: {exc}') from exc
        if count != len(data):
            raise OSError(f'Short USB write: {count}/{len(data)}')

    def read(self, timeout: float) -> bytes:
        try:
            return bytes(self.handle.bulkRead(0x81, 65536, timeout=max(1, round(timeout * 1000))))
        except self.usb1.USBErrorTimeout:
            return b''
        except self.usb1.USBError as exc:
            raise OSError(f'KM003C USB read failed: {exc}') from exc

    def close(self):
        if self.claim is not None:
            try:
                self.claim.__exit__(None, None, None)
            except Exception:
                pass
            self.claim = None
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        if self.context is not None:
            self.context.close()
            self.context = None


class SerialTransport:
    def __init__(self, args):
        self.args = args
        self.handle = None

    def open(self):
        serial = dependency('serial', 'pyserial')
        port = self.args.port
        if not port:
            ports = serial_ports(self.args.vid, self.args.pid)
            if self.args.serial:
                ports = [p for p in ports if p['serial'] == self.args.serial]
            if len(ports) != 1:
                raise RuntimeError(f'Found {len(ports)} matching serial ports; select with --port.')
            port = ports[0]['port']
        self.handle = serial.Serial(port, self.args.baud, timeout=0,
                                    write_timeout=self.args.timeout)
        return self

    def write(self, data: bytes):
        count = self.handle.write(data)
        if count != len(data):
            raise OSError(f'Short serial write: {count}/{len(data)}')

    def read(self, timeout: float) -> bytes:
        # Changing pyserial.timeout reconfigures the whole Windows COM port,
        # including SetCommState. Repeating that while PDM runs disrupts it.
        # Keep the port nonblocking and enforce the read window on the host.
        deadline = time.monotonic() + max(0, min(timeout, 0.05))
        while True:
            available = self.handle.in_waiting
            if available:
                return self.handle.read(min(65536, available))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b''
            time.sleep(min(0.005, remaining))

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def serial_ports(vid=VID, pid=PID):
    ports = dependency('serial.tools.list_ports', 'pyserial')
    return [dict(port=p.device, description=p.description, serial=p.serial_number,
                 vid=p.vid, pid=p.pid) for p in ports.comports()
            if p.vid == vid and p.pid == pid]


def enumerate_devices(args) -> dict:
    result = {'vid': f'0x{args.vid:04X}', 'pid': f'0x{args.pid:04X}',
              'hid': [], 'cdc': [], 'usb': [], 'notes': []}
    try:
        hid = dependency('hid', 'hidapi')
        for d in hid.enumerate(args.vid, args.pid):
            result['hid'].append({k: (v.decode('utf-8', 'surrogateescape') if isinstance(v, bytes) else v)
                                  for k, v in d.items()})
    except (RuntimeError, OSError) as exc:
        result['notes'].append(str(exc))
    try:
        result['cdc'] = serial_ports(args.vid, args.pid)
    except (RuntimeError, OSError) as exc:
        result['notes'].append(str(exc))
    try:
        usb1 = dependency('usb1', 'libusb1')
        with usb1.USBContext() as context:
            for d in context.getDeviceList(skip_on_error=True):
                if d.getVendorID() == args.vid and d.getProductID() == args.pid:
                    result['usb'].append({'bus': d.getBusNumber(), 'address': d.getDeviceAddress(),
                                           'usb_release_bcd': f'0x{d.getbcdDevice():04X}'})
    except (RuntimeError, OSError) as exc:
        result['notes'].append(str(exc))
    return result


def selected_transport(args):
    mode = args.transport
    if mode == 'auto':
        mode = 'cdc' if args.port else ('usb' if args.command == 'capture' else 'hid')
    return {'hid': HidTransport, 'usb': UsbTransport, 'cdc': SerialTransport}[mode](args)


class Meter:
    def __init__(self, args, transport=None, on_transfer=None):
        self.args = args
        self.transport = transport if transport is not None else selected_transport(args)
        self.on_transfer = on_transfer
        self.transaction = 0
        self.buffer = bytearray()

    def __enter__(self):
        self.transport.open()
        return self

    def __exit__(self, *exc):
        self.transport.close()

    def read_frame(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            length = frame_length(self.buffer)
            if length is not None:
                raw = bytes(self.buffer[:length])
                del self.buffer[:length]
                # HID reports are zero-padded, while serial/USB may concatenate frames.
                if self.buffer and not any(self.buffer):
                    self.buffer.clear()
                return raw
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                partial = bytes(self.buffer)
                self.buffer.clear()
                raise TimeoutError(f'Timed out reading a KM003C frame ({len(partial)} partial bytes)')
            data = self.transport.read(min(remaining, 0.1))
            if data:
                if self.on_transfer:
                    self.on_transfer(data)
                self.buffer.extend(data)
                if len(self.buffer) > 65536:
                    self.buffer.clear()
                    raise ProtocolError('Receive buffer exceeds size limit')

    def get_data(self, attribute: int) -> bytes:
        transaction = self.transaction
        self.transaction = (transaction + 1) & 255
        self.transport.write(control_packet(GET_DATA, attribute, transaction))
        deadline = time.monotonic() + self.args.timeout
        while True:
            raw = self.read_frame(max(0, deadline - time.monotonic()))
            kind = raw[0] & 127
            if kind == PUT_DATA:
                # The supplied vendor example treats response ID as a random number.
                # Polling is strictly one transaction at a time; preserve actual IDs.
                return raw
            if kind in (6, 11):
                raise ProtocolError(f'Device rejected the data request: {raw.hex(" ")}')
            if time.monotonic() >= deadline:
                raise TimeoutError('No PutData response before timeout')


class CdcStream:
    """Vendor's new ADC-only CDC stream. Keep CRC and unknown fields untouched."""
    def __init__(self, args, transport=None, on_transfer=None):
        self.args = args
        self.transport = transport if transport is not None else SerialTransport(args)
        self.on_transfer = on_transfer
        self.buffer = bytearray()
        self.next_keepalive = 0

    def __enter__(self):
        self.transport.open()
        try:
            if self.args.rate is None:
                self.transport.write(b'\x02')
            else:
                code = {4: 0, 10: 1, 50: 2, 1000: 3}[self.args.rate]
                self.transport.write(bytes([2, code]))
            self.next_keepalive = time.monotonic() + 1800
        except BaseException:
            self.transport.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.transport.write(b'\x03')
        finally:
            self.transport.close()

    def read_frame(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            if len(self.buffer) >= 4:
                if self.buffer[0] != 2:
                    raise ProtocolError('Unexpected new CDC stream header')
                size = 4 + self.buffer[2] * 4
                if size <= 4:
                    raise ProtocolError('Empty new CDC ADC frame')
                if len(self.buffer) >= size:
                    raw = bytes(self.buffer[:size])
                    del self.buffer[:size]
                    return raw
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Timed out waiting for new CDC ADC data')
            if time.monotonic() >= self.next_keepalive:
                self.transport.write(b'\x01')
                self.next_keepalive = time.monotonic() + 1800
            data = self.transport.read(min(remaining, 0.1))
            if data:
                if self.on_transfer:
                    self.on_transfer(data)
                self.buffer.extend(data)


def encode_ascii_command(command: str) -> bytes:
    """One command per CDC write, without CR/LF (vendor's SSCOM framing)."""
    if any(c in command for c in ('\r', '\n', '\0')):
        raise ValueError('A command must contain only one line')
    return command.encode('ascii')


def format_ascii_response(response: bytes) -> str:
    """PDM replies mix ASCII lines with binary PDOs; escape binary for consoles."""
    text = response.decode('ascii', errors='backslashreplace')
    return ''.join(c if c.isprintable() or c in '\r\n\t' else f'\\x{ord(c):02x}'
                   for c in text).strip()


def ascii_command(args, command: str, transport=None) -> bytes:
    """Send exactly one documented ASCII command and retain its response bytes."""
    payload = encode_ascii_command(command)
    transport = transport if transport is not None else SerialTransport(args)
    transport.open()
    try:
        transport.write(payload)
        deadline = time.monotonic() + args.wait
        response = bytearray()
        while time.monotonic() < deadline:
            chunk = transport.read(min(0.05, deadline - time.monotonic()))
            response.extend(chunk)
            if len(response) > 1024 * 1024:
                raise ProtocolError('ASCII reply exceeds size limit')
        return bytes(response)
    finally:
        transport.close()
