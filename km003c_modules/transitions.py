"""KM003C adapter for CY4500-style AVS analysis, with disk-backed capture input."""
from dataclasses import asdict, replace
import csv
import json
import math
from pathlib import Path
import tempfile

from .transition_analysis import (SyncPDSample, SyncScopeSample, analyze_avs_transitions,
                                  decode_programmable_requests, RESET_MESSAGES)
from .transition_report import TRANSITION_OUTPUT_SUFFIXES, _write_transition_outputs, _print_transition_analysis
from .transition_plots import TRANSITION_PLOT_SUFFIXES, require_transition_plotting, write_transition_plots
from . import analysis_quality as quality


# Shared analysis thresholds follow CY4500; sample-dependent values suit 40 ms polling.
ANALYSIS_OPTIONS = (
    ('baseline-window-ms', 100.0, 'pre-request baseline window'),
    ('baseline-guard-ms', 5.0, 'exclude time immediately before the request'),
    ('movement-min-threshold-v', 0.05, 'minimum sustained VBUS deviation'),
    ('movement-mad-multiplier', 6.0, 'baseline noise MAD multiplier'),
    ('movement-sustain-samples', 2, 'consecutive measurements needed for movement'),
    ('target-band-percent', 1.0, 'requested-target band half-width'),
    ('settle-hold-ms', 80.0, 'minimum measured target-band hold'),
    ('settle-max-gap-ms', 100.0, 'maximum measurement gap during movement/settling'),
    ('plateau-lookback-ms', 400.0, 'stable measurement window for observed plateau'),
    ('plateau-min-samples', 6, 'minimum measured plateau sample count'),
    ('observed-band-percent', 0.5, 'observed-plateau band half-width'),
    ('observed-settle-hold-ms', 80.0, 'minimum measured observed-band hold'),
    ('plateau-stability-min-span-v', 0.10, 'minimum allowed plateau span limit'),
    ('plateau-stability-percent', 0.25, 'relative allowed plateau span'),
    ('plateau-target-guard-percent', 10.0, 'target plausibility guard for plateau'),
)


def add_analysis_options(parser, positive_float, nonnegative_float, positive_int):
    parser.add_argument('--transition-plots', action='store_true',
                        help='save waveform, timing and slew PNG plots (requires analysis and matplotlib)')
    for name, default, description in ANALYSIS_OPTIONS:
        kind = positive_int if isinstance(default, int) else (
            nonnegative_float if name in ('baseline-guard-ms', 'movement-mad-multiplier') else positive_float)
        parser.add_argument('--' + name, type=kind, default=default,
                            help=f'{description} (default: {default})'.replace('%', '%%'))


def analysis_settings(args):
    return {name.replace('-', '_'): getattr(args, name.replace('-', '_'))
            for name, _, _ in ANALYSIS_OPTIONS}


def analysis_output_suffixes(args):
    return TRANSITION_OUTPUT_SUFFIXES + ('.protocol_diagnostics.csv',) + (TRANSITION_PLOT_SUFFIXES if getattr(args, 'transition_plots', False) else ())


def run_analysis(pd, scope, args, *, capture_status='completed'):
    settings = analysis_settings(args)
    parameters = {}
    for name, value in settings.items():
        if name.endswith('_ms'):
            key = name[:-3] + '_us'
            key = {'settle_max_gap_us': 'settle_max_sample_gap_us'}.get(key, key)
            value = round(value * 1000)
        elif name.endswith('_percent'):
            key = name[:-8] + '_fraction'
            value /= 100
        else:
            key = {'movement_min_threshold_v': 'movement_min_threshold_V',
                   'plateau_stability_min_span_v': 'plateau_stability_min_span_V'}.get(name, name)
        parameters[key] = value
    analyses = analyze_avs_transitions(pd, scope, **parameters)
    _, skipped = decode_programmable_requests(pd)
    # Keep point-time and sampling limits visible in the compatible flags column.
    marked = []
    ordered = sorted(scope, key=lambda s: s.timestamp_us)
    for result in analyses:
        flags = list(result.flags) + ['km003c_point_timestamps', 'km003c_sampled_waveform']
        if capture_status != 'completed':
            flags.append('capture_' + capture_status)
        marked.append(replace(result, flags=tuple(dict.fromkeys(flags))))
    gaps = [b.timestamp_us - a.timestamp_us for a, b in zip(ordered, ordered[1:])]
    settings.update(device='KM003C', timestamp_resolution_us=1000,
                    timestamp_policy='device ms converted to us; PD start/end are observed point times',
                    waveform_policy='native samples only; no interpolation; latency and slew are estimates',
                    max_observed_sample_gap_us=max(gaps, default=None),
                    capture_status=capture_status, skipped_requests=skipped,
                    protocol_counts={kind: sum(a.supply_type == kind for a in marked)
                                     for kind in ('SPR_PPS', 'SPR_AVS', 'EPR_AVS')})
    prefix = Path(args.out_prefix).expanduser()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    wire = [dict(row_index=p.row_index, sno=p.sno, message=p.message, t=p.start_us,
                 role=('SRC' if p.header & 256 else 'SNK') if p.header is not None else None,
                 message_id=(p.header >> 9) & 7 if p.header is not None else None,
                 objects=[int.from_bytes(p.data[i:i+4], 'little') for i in range(0, len(p.data), 4)]) for p in pd]
    diagnostic_rows = quality.diagnostics(wire)
    settings.update(quality.resolution([dict(t=s.timestamp_us) for s in ordered]))
    settings['protocol_diagnostics_file'] = str(prefix.with_suffix('.protocol_diagnostics.csv'))
    settings['protocol_diagnostic_counts'] = dict(requests=sum(d['event']=='request' for d in diagnostic_rows),
        repeated_requests=sum(d.get('duplicate_classification','none')!='none' for d in diagnostic_rows),
        resets=sum(d['event'] in quality.RESETS for d in diagnostic_rows))
    keys = list(dict.fromkeys(k for r in diagnostic_rows for k in r)) or ['status']
    with prefix.with_suffix('.protocol_diagnostics.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(diagnostic_rows)
    if getattr(args, 'transition_plots', False):
        settings['plots'] = write_transition_plots(marked, pd, scope, prefix,
            max_gap_us=parameters['settle_max_sample_gap_us'])
    _write_transition_outputs(marked, csv_path=prefix.with_suffix('.transitions.csv'),
                              summary_path=prefix.with_suffix('.transitions.txt'), settings=settings,
                              human_csv_path=prefix.with_suffix('.transition_summary.csv'),
                              human_text_path=prefix.with_suffix('.transition_summary.txt'))
    messages = ['\nProtocol diagnostics (reset sender does not establish root cause):']
    for d in diagnostic_rows:
        if d['event'] != 'request' or d['duplicate_classification'] != 'none' or d['protocol_status'] == 'failed':
            messages.append(f"{d['event']} Sno={d['sno']} sender={d['sender']} ID={d.get('message_id')}; "
                f"{d.get('duplicate_classification','')}; GoodCRC={d.get('goodcrc_status')}; "
                f"previous ACK Sno={d.get('previous_goodcrc_sno')}; reset={d.get('reset_sender')}; "
                f"{d['cause_classification']}: {d['cause_evidence']}")
    for suffix in ('.transitions.txt', '.transition_summary.txt'):
        with prefix.with_suffix(suffix).open('a', encoding='utf-8') as handle:
            handle.write('\n'.join(messages)+'\n')
    return marked, settings


def print_analysis_outputs(analyses, prefix, settings=None):
    _print_transition_analysis(analyses)
    for suffix in TRANSITION_OUTPUT_SUFFIXES + ('.protocol_diagnostics.csv',):
        print(f'Transition analysis: {Path(prefix).with_suffix(suffix).resolve()}')
    if settings:
        for path in settings.get('plots', []):
            print(f'Transition plot: {path}')
        if settings.get('skipped_requests'):
            print(f'Skipped requests: {settings["skipped_requests"]}')


class TransitionSession:
    def __init__(self, stack):
        self.pd = stack.enter_context(tempfile.TemporaryFile(mode='w+t', encoding='utf-8'))
        self.scope = stack.enter_context(tempfile.TemporaryFile(mode='w+t', encoding='utf-8'))

    def measurement(self, sample):
        if sample.clock_source == 'device_ms' and sample.timestamp_us is not None:
            self.scope.write(json.dumps(asdict(SyncScopeSample(sample.timestamp_us, sample.voltage,
                              sample.current, sample.cc1_mV / 1000, sample.cc2_mV / 1000))) + '\n')

    def event(self, event, index):
        # SOP'/SOP'' responses concern cables and must not satisfy power-contract matching.
        if event.message in RESET_MESSAGES and event.header is None:
            self.pd.write(json.dumps(dict(row_index=index-1, sno=index, message=event.message,
                start_us=event.timestamp_us, end_us=event.timestamp_us, vbus_V=None, data='')) + '\n')
            return
        if event.header is None or event.sop != 0:
            return
        self.pd.write(json.dumps(dict(row_index=index - 1, sno=index, message=event.message,
                      header=event.header,
                      start_us=event.timestamp_us, end_us=event.timestamp_us,
                      vbus_V=event.vbus_mV / 1000,
                      data=bytes.fromhex(event.wire_hex)[2:2 + 4 * ((event.header >> 12) & 7)].hex())) + '\n')

    def finish(self, args, status):
        self.pd.seek(0)
        self.scope.seek(0)
        pd = []
        for line in self.pd:
            values = json.loads(line)
            values['data'] = bytes.fromhex(values['data'])
            pd.append(SyncPDSample(**values))
        scope = [SyncScopeSample(**json.loads(line)) for line in self.scope]
        return run_analysis(pd, scope, args, capture_status=status)


def load_analysis_csv(pd_path, scope_path):
    pd = []
    with Path(pd_path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'Sno', 'SOP', 'Message', 'Data', 'Start Time', 'End Time', 'Vbus(V)'} <= set(reader.fieldnames or []):
            raise ValueError('PD CSV is missing required Utility columns')
        for index, row in enumerate(reader):
            reset = row['Message'] in RESET_MESSAGES and row['SOP'] in ('SOP', '', '-')
            if not reset and row['SOP'] != 'SOP':
                continue
            try:
                words = (row['Data'] or '').split()
                header = int(words[0], 16) if words else None
                data = b'' if header is not None and header & 0x8000 else b''.join(int(word, 16).to_bytes(4, 'little') for word in words[1:])
                pd.append(SyncPDSample(index, int(row['Sno']), row['Message'], int(row['Start Time']),
                          int(row['End Time']), float(row['Vbus(V)']) / 1000 if row['Vbus(V)'] else None, data, header))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f'Invalid PD CSV row {index + 2}: {exc}') from exc
    scope = []
    with Path(scope_path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'Timestamp Raw', 'Timestamp(us)', 'Vbus(V)'} <= set(reader.fieldnames or []):
            raise ValueError('Scope CSV is missing device timestamp/voltage columns')
        for row in reader:
            if not row['Timestamp Raw']:
                raise ValueError('Scope CSV must have device timestamps; standalone host-timed scope cannot be correlated')
            timestamp, voltage = int(row['Timestamp(us)']), float(row['Vbus(V)'])
            if not math.isfinite(voltage):
                raise ValueError('Scope voltage must be finite')
            def optional_voltage(name):
                value = row.get(name, '')
                return float(value) if value not in ('', None) else None
            scope.append(SyncScopeSample(timestamp, voltage, optional_voltage('Ibus(A)'),
                                         optional_voltage('CC1(V)'), optional_voltage('CC2(V)')))
    return pd, scope
