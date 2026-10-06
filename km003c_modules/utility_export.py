"""Analyzer Utility 4.2 ccgx3 export (ZIP containing Java serialization).

The serialization schema follows the MIT-licensed sibling CY4500/TI CLI writers.
No Java runtime or proprietary libraries are required. The writer deliberately
supports only the value types in the captured Utility schema; it never loads
Java objects or executes content from input files.
"""
import csv
import shutil
import struct
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from .protocol import PD_COLUMNS


COLUMNS = PD_COLUMNS

def packet_for_event(event, index):
    """Adapt an observed PD message; never synthesize a PD header for status events."""
    if event.header is None or event.sop not in (0, 1, 2):
        return None
    count = (event.header >> 12) & 7
    wire = bytes.fromhex(event.wire_hex)
    if len(wire) < 2 + count * 4:
        raise ValueError('PD header exceeds available wire bytes')
    row = list(map(str, event.csv_row(index)))
    row[2] = ('SOP', 'SOP_PRIME', 'SOP_DPRIME')[event.sop]
    row[8] = {0: 'v1', 1: 'v2', 2: 'v3'}.get((event.header >> 6) & 3, 'RSVD')
    # pktData is an adapter for the GUI decoder, not a native CY4500 capture.
    # No OK/CRC/EOP bits are asserted: KM003C does not expose those results.
    word = event.header | (event.sop << 16)
    vbus_raw = max(0, round(event.vbus_mV * 4096 / (4048 * 12)))
    timestamp = event.timestamp_us & 0xFFFFFFFF
    raw = struct.pack('<IIIII', index & 0xFFFFFFFF, vbus_raw,
                      timestamp, timestamp, word) + wire[2:2 + count * 4]
    return raw, row


def packet_for_vbus_event(event, index):
    """Explicit software voltage-event adapter, using the Utility VOLT_PKT flag."""
    timestamp = event['timestamp_us']
    mv = event['vbus_mV']
    code = 2 if event['event'] == 'VBUS_UP' else 1
    vbus_raw = max(0, round(mv * 4096 / (4048 * 12)))
    raw = struct.pack('<IIIII', code, vbus_raw, timestamp & 0xFFFFFFFF,
                      timestamp & 0xFFFFFFFF, 1 << 26)
    # Reserved control header is the GUI's voltage-event representation, not PD.
    row = list(map(str, (index, event['event'], 'SOP', 'C_RSVD0', 0, 'UFP', 'SNK',
                         0, 'v1', 0, '', mv, '0x0', timestamp, timestamp)))
    return raw, row


def _utf(s):
    b = s.encode('ascii')  # All schema names and exported values are ASCII.
    return struct.pack('>H', len(b)) + b

def _string(s):
    return b'\x70' if s is None else b'\x74' + _utf(s)

def _desc(name, uid, fields, flags=2):
    out = b'\x72' + _utf(name) + struct.pack('>QBH', uid, flags, len(fields))
    for kind, field, signature in fields:
        out += kind.encode() + _utf(field)
        if kind in 'L[':
            out += _string(signature)
    return out + b'\x78\x70'

LIST = _desc('java.util.ArrayList', 0x7881d21d99c7619d, [('I', 'size', None)], 3)
BYTE_ARRAY = _desc('[B', 0xacf317f8060854e0, [])
UUID = _desc('java.util.UUID', 0xbc9903f7986d852f, [('J', 'leastSigBits', None), ('J', 'mostSigBits', None)])
STR = 'Ljava/lang/String;'
ARR = 'Ljava/util/ArrayList;'
PACKET_FIELDS = [('Z', 'isMarked', None), ('I', 'markerCount', None)] + [
    ('L', n, sig) if n != 'pktData' else ('[', n, sig) for n, sig in [
    ('bg', 'Lcom/cypress/ezpdanalyzer/ui/util/BGColor;'), ('count', STR), ('dRole', STR),
    ('data', STR), ('delta', STR), ('duration', STR), ('eTime', STR), ('id', STR),
    ('msg', STR), ('ok', STR), ('pRole', STR), ('packetDetails', ARR), ('payloads', ARR),
    ('pktData', '[B'), ('rev', STR), ('sTime', STR), ('sno', STR), ('sop', STR),
    ('subPackets', ARR), ('uniqueId', 'Ljava/util/UUID;'), ('vbus', STR)]]
PACKET = _desc('com.cypress.ezpdanalyzer.ui.model.USBPacketData', 1, PACKET_FIELDS)
GRAPH = _desc('com.cypress.ezpdanalyzer.ui.model.GraphData', 1,
    [('S', 'amp', None), ('S', 'cc1', None), ('S', 'cc2', None), ('J', 'timeStamp', None), ('S', 'volt', None)])

def _list_start(count):
    return b'\x73' + LIST + struct.pack('>i', count) + b'\x77\x04' + struct.pack('>i', count)

EMPTY_LIST = _list_start(0) + b'\x78'

def _packet(raw, row):
    values = dict(zip(('sno','ok','sop','msg','id','dRole','pRole','count','rev','duration','delta','vbus','data','sTime','eTime'), row))
    # Native objects store hardware Sno; CSV exports sequential row numbers.
    values['sno'] = str(int.from_bytes(raw[:4], 'little'))
    raw = raw[:20 + 4 * ((int.from_bytes(raw[16:20], 'little') >> 12) & 7)]
    out = b'\x73' + PACKET + b'\x00' * 5
    for _, name, _ in PACKET_FIELDS[2:]:
        if name == 'bg': out += b'\x70'
        elif name in ('packetDetails', 'payloads', 'subPackets'): out += EMPTY_LIST
        elif name == 'pktData': out += b'\x75' + BYTE_ARRAY + struct.pack('>i', len(raw)) + raw
        elif name == 'uniqueId':
            u = uuid.uuid4().int
            out += b'\x73' + UUID + struct.pack('>QQ', u & ((1<<64)-1), u >> 64)
        else: out += _string(values[name])
    return out

class UtilityExport:
    """Stream into disk-backed temporary files, then assemble the ZIP on close."""
    def __init__(self, csv_path=None, ccgx3_path=None):
        self.packet_count = 0
        self.csv_fp = None
        self.packets = None
        self.scope = None
        self.ccgx3_path = ccgx3_path
        self.scope_count = 0
        self.graph_clipped = {}
        self.closed = False
        try:
            if csv_path is not None:
                self.csv_fp = open(csv_path, 'w', encoding='utf-8', newline='')
                self.csv = csv.writer(self.csv_fp, lineterminator='\n')
                self.csv.writerow(COLUMNS)
            if ccgx3_path is not None:
                self.packets = tempfile.TemporaryFile()
                self.scope = tempfile.TemporaryFile()
        except BaseException:
            self._cleanup()
            raise

    def write_packet(self, raw, row, csv_row=None):
        if self.csv_fp is None and self.packets is None:
            return
        self.packet_count += 1
        if self.csv_fp is not None: self.csv.writerow(row if csv_row is None else csv_row)
        if self.packets is not None: self.packets.write(_packet(raw, row))

    def write_scope(self, sample):
        if self.scope is not None:
            # GraphData stores already converted mV/mA, unlike native CY ADC
            # records. Both stock 4.2 and the user's EPR GUI plot these directly.
            # The EPR GUI interprets volt as unsigned, supporting up to 65.535 V.
            values = {
                'IBUS': round(sample.current * 1000),
                'CC1': round(sample.cc1_mV),
                'CC2': round(sample.cc2_mV),
                'VBUS': round(sample.voltage * 1000),
            }
            for key, value in values.items():
                lower, upper = (0, 65535) if key == 'VBUS' else (-32768, 32767)
                bounded = max(lower, min(upper, value))
                if value != bounded:
                    self.graph_clipped[key] = self.graph_clipped.get(key, 0) + 1
                values[key] = bounded
            self.scope.write(b'\x73' + GRAPH + struct.pack('>hhhqH', values['IBUS'],
                values['CC1'], values['CC2'], sample.timestamp_us, values['VBUS']))
            self.scope_count += 1

    def _cleanup(self):
        for fp in (self.csv_fp, self.packets, self.scope):
            if fp is not None: fp.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.ccgx3_path is not None:
                folder = datetime.now().strftime('%Y_%m_%d_%H_%M_%S') + '/'
                # Assemble on the same filesystem and replace only a complete ZIP.
                target = Path(self.ccgx3_path)
                temporary = tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp', delete=False)
                temporary.close()
                pending = Path(temporary.name)
                try:
                    self._write_archive(pending, folder)
                    pending.replace(target)
                finally:
                    pending.unlink(missing_ok=True)
        finally:
            self._cleanup()

    def _write_archive(self, path, folder):
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr(folder, b'')
            for name, fp, count in [('ezpd_last.part', self.packets, self.packet_count),
                                    ('ezpd_scope.scope', self.scope, self.scope_count)]:
                with z.open(folder + name, 'w', force_zip64=True) as dest:
                    dest.write(b'\xac\xed\x00\x05' + _list_start(count))
                    fp.seek(0)
                    shutil.copyfileobj(fp, dest)
                    dest.write(b'\x78')
