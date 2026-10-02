"""KM003C framing, calibrated ADC values, and observed PD event decoding.

Sources and firmware limitations are documented in README.md.
No CY4500 ADC scaling or binary record layout is used here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import struct

VID, PID = 0x5FC9, 0x0063
GET_DATA, PUT_DATA = 0x0C, 0x41
ADC, PD_PACKET = 0x0001, 0x0010

# Column names/order match cy4500_cli.py and its Analyzer Utility exporter.
PD_COLUMNS = ('Sno,Ok,SOP,Message,Msg Id,Data Role,Power Role,Obj Count,Rev,'
              'Duration,Delta,Vbus(V),Data,Start Time,End Time').split(',')
SCOPE_COLUMNS = [
    'Transfer', 'Packet', 'Sample', 'Timestamp Raw', 'Timestamp(us)',
    'Vbus Raw', 'Vbus(mV)', 'Vbus(V)', 'CC1 Raw', 'CC1(mV)', 'CC1(V)',
    'CC2 Raw', 'CC2(mV)', 'CC2(V)', 'Ibus Raw', 'Ibus(mA)', 'Ibus(A)',
    'Power(W)', 'Padding',
]
LIVE_COLUMNS = [
    'sample', 'monotonic_s', 'raw_hex', 'raw0_vbus', 'raw1_ibus',
    'raw2_cc1', 'raw3_cc2', 'vbus_mV', 'vbus_V', 'ibus_mA', 'ibus_A',
    'ibus_median_A', 'cc1_mV', 'cc1_V', 'cc2_mV', 'cc2_V',
    'power_instant_W', 'power_median_W',
]

CONTROL_NAMES = {
    1: 'GOODCRC', 2: 'GOTO_MIN', 3: 'ACCEPT', 4: 'REJECT', 5: 'PING',
    6: 'PS_RDY', 7: 'GET_SOURCE_CAP', 8: 'GET_SINK_CAP', 9: 'DR_SWAP',
    10: 'PR_SWAP', 11: 'VCONN_SWAP', 12: 'WAIT', 13: 'SOFT_RESET',
    14: 'DATA_RESET', 15: 'DATA_RESET_COMPLETE', 16: 'NOT_SUPPORTED',
    17: 'GET_SOURCE_CAP_EXTENDED', 18: 'GET_STATUS', 19: 'FR_SWAP',
    20: 'GET_PPS_STATUS', 21: 'GET_COUNTRY_CODES', 22: 'GET_SINK_CAP_EXTENDED',
    23: 'GET_SOURCE_INFO', 24: 'GET_REVISION',
}
DATA_NAMES = {
    1: 'SOURCE_CAPABILITIES', 2: 'REQUEST', 3: 'BIST', 4: 'SINK_CAPABILITIES',
    5: 'BATTERY_STATUS', 6: 'ALERT', 7: 'GET_COUNTRY_INFO',
    8: 'ENTER_USB', 9: 'EPR_REQUEST', 10: 'EPR_MODE',
    11: 'SOURCE_INFO', 12: 'REVISION', 15: 'VENDOR_DEFINED',
}
EXTENDED_NAMES = {
    1: 'SOURCE_CAPABILITIES_EXTENDED', 2: 'STATUS', 3: 'GET_BATTERY_CAP',
    4: 'GET_BATTERY_STATUS', 5: 'BATTERY_CAPABILITIES',
    6: 'GET_MANUFACTURER_INFO', 7: 'MANUFACTURER_INFO',
    8: 'SECURITY_REQUEST', 9: 'SECURITY_RESPONSE',
    10: 'FIRMWARE_UPDATE_REQUEST', 11: 'FIRMWARE_UPDATE_RESPONSE', 12: 'PPS_STATUS',
    13: 'COUNTRY_INFO', 14: 'COUNTRY_CODES', 15: 'SINK_CAPABILITIES_EXTENDED',
    16: 'EXTENDED_CONTROL_MSG', 17: 'EPR_SOURCE_CAPABILITIES',
    18: 'EPR_SINK_CAPABILITIES',
}


class ProtocolError(ValueError):
    """A received frame is incomplete or has an unknown layout."""


def control_packet(kind: int, attribute: int = 0, transaction: int = 0) -> bytes:
    if not 0 <= kind < 128 or not 0 <= attribute <= 0x7FFF or not 0 <= transaction <= 255:
        raise ValueError('Control header field outside its bit width')
    return struct.pack('<I', kind | (transaction << 8) | (attribute << 17))


@dataclass
class LogicalPacket:
    attribute: int
    chunk: int
    payload: bytes


def frame_length(data: bytes | bytearray) -> int | None:
    """Return framed length, or None while a fragmented frame is incomplete."""
    if len(data) < 4:
        return None
    if data[0] & 0x7F != PUT_DATA:
        return 4
    # Some devices return an empty PutData header without an extended header.
    if struct.unpack_from('<I', data)[0] >> 22 == 0 and (len(data) == 4 or not any(data[4:])):
        return 4
    offset = 4
    for _ in range(128):
        if len(data) < offset + 4:
            return None
        header = struct.unpack_from('<I', data, offset)[0]
        size = header >> 22
        offset += 4 + size
        if offset > 65536:
            raise ProtocolError('Frame exceeds size limit')
        if len(data) < offset:
            return None
        if not header & 0x8000:
            return offset
    raise ProtocolError('Too many chained logical packets')


def logical_packets(raw: bytes) -> list[LogicalPacket]:
    if len(raw) < 4 or raw[0] & 0x7F != PUT_DATA:
        raise ProtocolError('Expected a PutData response')
    if len(raw) == 4:
        return []
    result = []
    offset = 4
    for _ in range(128):
        if len(raw) < offset + 4:
            raise ProtocolError('Truncated logical header')
        word = struct.unpack_from('<I', raw, offset)[0]
        size = word >> 22
        offset += 4
        if len(raw) < offset + size:
            raise ProtocolError('Truncated logical payload')
        result.append(LogicalPacket(word & 0x7FFF, (word >> 16) & 63,
                                    raw[offset:offset + size]))
        offset += size
        if not word & 0x8000:
            if any(raw[offset:]):
                raise ProtocolError('Nonzero bytes after the final logical packet')
            return result
    raise ProtocolError('Too many chained logical packets')


@dataclass
class Measurement:
    vbus_uV: int
    ibus_uA: int
    cc1_raw: int
    cc2_raw: int
    aux_scale_mV: float
    dp_raw: int | None = None
    dm_raw: int | None = None
    temp_raw: int | None = None
    vdd_raw: int | None = None
    timestamp_raw: int | None = None
    timestamp_us: int | None = None
    clock_source: str = 'host_monotonic'
    raw_hex: str = ''
    extra: dict = field(default_factory=dict)

    @property
    def voltage(self):
        return self.vbus_uV / 1_000_000

    @property
    def current(self):
        return self.ibus_uA / 1_000_000

    @property
    def power(self):
        return self.voltage * self.current

    @property
    def cc1_mV(self):
        return self.cc1_raw * self.aux_scale_mV

    @property
    def cc2_mV(self):
        return self.cc2_raw * self.aux_scale_mV

    def scope_row(self, transfer: int, packet: int, sample: int):
        return [transfer, packet, sample,
                '' if self.timestamp_raw is None else self.timestamp_raw,
                self.timestamp_us, self.vbus_uV, self.vbus_uV / 1000,
                f'{self.voltage:.9f}', self.cc1_raw, self.cc1_mV,
                f'{self.cc1_mV / 1000:.9f}', self.cc2_raw, self.cc2_mV,
                f'{self.cc2_mV / 1000:.9f}', self.ibus_uA,
                self.ibus_uA / 1000, f'{self.current:.9f}', f'{self.power:.9f}', '']


def decode_adc(payload: bytes, *, average: bool = True) -> Measurement:
    if len(payload) < 40:
        raise ProtocolError(f'ADC requires at least 40 bytes, received {len(payload)}')
    v, i, va, ia, vo, io, temp, cc1, cc2, dp, dm, vdd = struct.unpack_from('<6ih5H', payload)
    return Measurement(va if average else v, ia if average else i, cc1, cc2, 0.1,
                       dp, dm, temp, vdd, raw_hex=payload.hex(' '),
                       extra={'instant_vbus_uV': v, 'instant_ibus_uA': i,
                              'uncalibrated_vbus_uV': vo, 'uncalibrated_ibus_uA': io,
                              'rate_index': payload[36] & 3,
                              'tail_hex': payload[37:].hex(' ')})


class ClockUnwrapper:
    """Unwrap device milliseconds without interpreting small backwards moves as rollover."""
    def __init__(self, bits: int = 32):
        self.modulus = 1 << bits
        self.highwater = None

    def unwrap(self, raw: int) -> int:
        if self.highwater is None:
            self.highwater = raw
            return raw
        base = self.highwater // self.modulus * self.modulus
        candidate = base + raw
        if candidate - self.highwater > self.modulus // 2:
            candidate -= self.modulus
        elif self.highwater - candidate > self.modulus // 2:
            candidate += self.modulus
        self.highwater = max(self.highwater, candidate)
        return candidate


def nearest_timestamp(raw: int, reference: int, bits: int) -> int:
    modulus = 1 << bits
    base = reference // modulus * modulus + raw
    return min((base - modulus, base, base + modulus), key=lambda t: abs(t - reference))


@dataclass
class PdEvent:
    kind: str
    timestamp_raw: int
    timestamp_us: int
    vbus_mV: int
    ibus_mA: int
    raw_hex: str
    sop: int | None = None
    wire_hex: str = ''
    header: int | None = None
    message: str = ''
    code: int | None = None
    objects: list[int] = field(default_factory=list)
    trailer_hex: str = ''

    def csv_row(self, index: int):
        if self.header is None:
            return [index, '', '', self.message, '', '', '', '', '', '', '',
                    self.vbus_mV, '', self.timestamp_us, self.timestamp_us]
        h = self.header
        count = (h >> 12) & 7
        # CRC/EOP and wire duration are not exposed. The one device observation
        # timestamp is represented as a point (Start Time == End Time), so the
        # CY4500 CSV reader can consume it without inventing a wire duration.
        data = f'0x{h:X}'
        wire = bytes.fromhex(self.wire_hex)
        if h & 0x8000:
            if len(wire) >= 4:
                data += f" 0x{int.from_bytes(wire[2:4], 'little'):X}"
                data += ''.join(f' 0x{b:02X}' for b in wire[4:2 + 4 * count])
        else:
            data += ''.join(f' 0x{x:X}' for x in self.objects)
        return [index, '', {0: 'SOP', 1: "SOP'", 2: 'SOP"'}.get(self.sop, f'SOP_{self.sop}'),
                self.message, (h >> 9) & 7, 'DFP' if h & 0x20 else 'UFP',
                'SRC' if h & 0x100 else 'SNK', count,
                {0: '1.0', 1: '2.0', 2: '3.0'}.get((h >> 6) & 3, 'Reserved'),
                '', '', self.vbus_mV, data, self.timestamp_us, self.timestamp_us]


def decode_pd(payload: bytes, clock: ClockUnwrapper) -> tuple[Measurement, list[PdEvent]]:
    if len(payload) < 12:
        raise ProtocolError('PD packet is missing its 12-byte measurement preamble')
    ts, v, i, cc1, cc2 = struct.unpack_from('<IHhHH', payload)
    reference = clock.unwrap(ts)
    measurement = Measurement(v * 1000, i * 1000, cc1, cc2, 1,
                              timestamp_raw=ts, timestamp_us=reference * 1000,
                              clock_source='device_ms', raw_hex=payload[:12].hex(' '))
    offset = 12
    events = []
    while offset < len(payload):
        if len(payload) - offset < 6:
            raise ProtocolError('Truncated PD event wrapper')
        flag = payload[offset]
        if flag == 0x45:
            raw = payload[offset:offset + 6]
            event_ts = int.from_bytes(raw[1:4], 'little')
            code = raw[5]
            events.append(PdEvent('connection', event_ts,
                nearest_timestamp(event_ts, reference, 24) * 1000, v, i,
                raw.hex(' '), message={0x21: 'CONNECT', 0x22: 'DISCONNECT'}.get(code, f'EVENT_0x{code:02X}'), code=code))
            offset += 6
            continue
        if not flag & 0x80:
            raise ProtocolError(f'Unknown PD event flag 0x{flag:02X}')
        size = (flag & 0x3F) - 5
        if size < 2 or offset + 6 + size > len(payload):
            raise ProtocolError('Truncated or invalid wrapped PD message')
        raw = payload[offset:offset + 6 + size]
        event_ts = int.from_bytes(raw[1:5], 'little')
        wire = raw[6:]
        h = int.from_bytes(wire[:2], 'little')
        count, msg_type = (h >> 12) & 7, h & 31
        expected = 2 + count * 4
        if len(wire) < expected:
            raise ProtocolError('PD header object count exceeds available data')
        if h & 0x8000:
            names, label = EXTENDED_NAMES, 'E'
        elif count:
            names, label = DATA_NAMES, 'D'
        else:
            names, label = CONTROL_NAMES, 'C'
        objects = [int.from_bytes(wire[n:n + 4], 'little') for n in range(2, expected, 4)]
        events.append(PdEvent('pd', event_ts,
            nearest_timestamp(event_ts, reference, 32) * 1000, v, i, raw.hex(' '),
            sop=raw[5], wire_hex=wire.hex(' '), header=h,
            message=names.get(msg_type, f'{label}_RSVD{msg_type}'), objects=objects,
            trailer_hex=wire[expected:].hex(' ')))
        offset += 6 + size
    return measurement, events


def decode_cdc_adc(frame: bytes, clock: ClockUnwrapper) -> list[Measurement]:
    """Decode the supplied new CDC format; CRC algorithm/time unit remain undocumented."""
    if len(frame) < 4 or frame[0] != 2 or len(frame) != 4 + frame[2] * 4:
        raise ProtocolError('Invalid new CDC ADC frame length')
    payload = frame[4:]
    if len(payload) % 20:
        raise ProtocolError('New CDC ADC payload is not a multiple of 20 bytes')
    samples = []
    for offset in range(0, len(payload), 20):
        raw = payload[offset:offset + 20]
        ts, v, i, cc1, cc2, dp, dm = struct.unpack('<Iii4H', raw)
        # The vendor document names Time but does not define its unit. Preserve it.
        samples.append(Measurement(v, i, cc1, cc2, 1, dp, dm,
                                  timestamp_raw=ts, clock_source='host_monotonic',
                                  raw_hex=raw.hex(' '),
                                  extra={'cdc_header_crc_raw': frame[3],
                                         'crc_verified': None, 'cdc_attribute': frame[1]}))
    return samples


def measurement_dict(sample: Measurement) -> dict:
    result = asdict(sample)
    result.update(vbus_V=sample.voltage, ibus_A=sample.current, power_W=sample.power,
                  cc1_V=sample.cc1_mV / 1000, cc2_V=sample.cc2_mV / 1000)
    return result
